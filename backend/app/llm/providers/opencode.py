"""OpenCode CLI (`opencode run`). Uses the logins OpenCode already has on this machine (GitHub Copilot, OpenCode Zen,
9router, ...), no API key here.

Each request runs one `opencode run --format json -m <provider>/<model> <message>` process in an empty temp directory
(so no project files, AGENTS.md or git repo are picked up). `--auto` is not passed, so tool permissions are not
granted. The system prompt and the history are folded into the one message (the CLI takes a single prompt).

Model ids are OpenCode's own `<opencode provider>/<model>` (`opencode models` lists them), so a spec looks like
``opencode:github-copilot/claude-sonnet-5.5`` or ``opencode:9router/kc/minimax/minimax-m3:free`` (the model keeps
everything after the first colon of the spec).

Settings (app/llm/config.py, section "opencode"): model, thinking, isolated, bin, timeout, rotation, accounts.
- model: request model > providers.opencode.model (default opencode/beam-free), as opencode writes it.
- thinking: extra["effort"] > extra["reasoning"] (the OpenRouter option) > providers.opencode.thinking. It is the
  model's `#variant`: the CLI gets `-m <model>#<thinking>` unless thinking is "" / "off" or the model already has
  a `#variant`.
- isolated (default true): the run loads none of this machine's opencode plugins, MCP servers, skills or AGENTS.md:
  `--standalone` (a private server, not the background service that already loaded them), an empty
  XDG_CONFIG_HOME and OPENCODE_DISABLE_CLAUDE_CODE / _PROJECT_CONFIG / _AUTOUPDATE / _FILEWATCHER. (OpenCode 2.3.2
  has no single "pure" flag.) The logins stay (they live in XDG_DATA_HOME). `isolated: false` uses the machine's
  own config (needed for models of custom providers defined in it). The run also happens in an empty temp cwd.
- accounts: several logins, each its own XDG_DATA_HOME (where opencode keeps auth.json) and/or XDG_CONFIG_HOME
  (see accounts.py); the next one takes over when one hits a usage / rate limit or its login is refused.
- bin: providers.opencode.bin, else `opencode` on PATH, else ~/.bun/bin/opencode (a server's PATH often lacks it).
- timeout: providers.opencode.timeout (default 180s). A timeout is not retried; 429 / 5xx answers are.
- temperature / max_tokens are not supported by the CLI; images are saved to temp files and attached with -f
  (the chosen model must support vision).

JSON events (one object per line on stdout, exit code 0 even when the run failed):
    {"type":"step_start","sessionID":...,"part":{...}}
    {"type":"text","part":{"type":"text","text":"Hi!"}}                 the answer (several parts are joined)
    {"type":"reasoning","part":{"type":"reasoning","text":"..."}}       thinking, when the model emits it
    {"type":"step_finish","part":{"reason":"stop","cost":0.003,"tokens":{"input":3,"output":23,"reasoning":0,
                                                                          "cache":{"read":0,"write":14737}}}}
    {"type":"error","error":{"type":"provider.no-route","message":"Model unavailable: ...","status":402?}}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..accounts import AccountRunner
from ..config import Settings, text
from ..schema import BadInput, ChatRequest, ChatResult, LLMError
from . import register
from .base import LLMProvider

log = logging.getLogger("app.llm")

ACCOUNT_FIELDS = {"data_home": "XDG_DATA_HOME", "config_home": "XDG_CONFIG_HOME"}  # accounts[].<field> -> env var
DEFAULT_MODEL = "opencode/beam-free"
DEFAULT_TIMEOUT = 180.0
MODELS_TTL = 600.0  # seconds the `opencode models` list is reused
MODELS_TIMEOUT = 30.0
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}
_MODEL_ID = re.compile(r"^[A-Za-z0-9_.-]+/\S+$")  # <provider>/<model>
_STATUS = re.compile(r"\b(4\d\d|5\d\d)\b")
_ERROR_STATUS = {"provider.quota": 402, "provider.auth": 401, "provider.rate-limit": 429, "provider.no-route": 404}

# `opencode models` lists, by binary: (fetched at, ids)
_models_cache: dict[str, tuple[float, list[str]]] = {}
_models_lock = threading.Lock()


def clear_models_cache() -> None:
    with _models_lock:
        _models_cache.clear()


@register("opencode")
class OpenCodeProvider(LLMProvider):
    name = "opencode"
    label = "OpenCode"

    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.section(self.name)
        self.bin = text(cfg.get("bin")) or "opencode"
        self.model = text(cfg.get("model")) or DEFAULT_MODEL
        self.thinking = text(cfg.get("thinking")).lower()
        self.isolated = cfg.get("isolated") is not False
        try:
            self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        self.accounts = AccountRunner.from_section(self.name, self.label, cfg, ACCOUNT_FIELDS)

    # ---- public API -------------------------------------------------------

    def chat(self, req: ChatRequest) -> ChatResult:
        with tempfile.TemporaryDirectory(dir=self._workdir(), prefix="img-") as tmp:
            args, env = self.build_command(req, image_dir=tmp)
            return self.accounts.run(lambda extra: self._chat_once(req, args, {**env, **extra}))

    async def achat(self, req: ChatRequest) -> ChatResult:
        with tempfile.TemporaryDirectory(dir=self._workdir(), prefix="img-") as tmp:
            args, env = self.build_command(req, image_dir=tmp)
            return await self.accounts.arun(lambda extra: self._achat_once(req, args, {**env, **extra}))

    def _chat_once(self, req: ChatRequest, args: list[str], env: dict[str, str]) -> ChatResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, env=env, cwd=self._workdir(),
                    stdin=subprocess.DEVNULL, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                raise LLMError(f"OpenCode timed out after {self.timeout:.0f}s", provider=self.name) from None
            except OSError as e:
                raise LLMError(f"cannot run OpenCode ({args[0]}): {e}", provider=self.name) from e
            try:
                return self.parse_output(proc.stdout, proc.stderr, proc.returncode, model=req.model or self.model)
            except LLMError as e:
                if attempt == attempts - 1 or e.status_code not in RETRY_STATUS:
                    raise
                time.sleep(self._retry_wait(attempt, e))
        raise AssertionError("unreachable")

    async def _achat_once(self, req: ChatRequest, args: list[str], env: dict[str, str]) -> ChatResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env, cwd=self._workdir(),
                )
            except OSError as e:
                raise LLMError(f"cannot run OpenCode ({args[0]}): {e}", provider=self.name) from e
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise LLMError(f"OpenCode timed out after {self.timeout:.0f}s", provider=self.name) from None
            try:
                return self.parse_output(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode,
                                         model=req.model or self.model)
            except LLMError as e:
                if attempt == attempts - 1 or e.status_code not in RETRY_STATUS:
                    raise
                await asyncio.sleep(self._retry_wait(attempt, e))
        raise AssertionError("unreachable")

    def problem(self) -> str | None:
        if self._binary() is None:
            return (f"OpenCode CLI '{self.bin}' not found (install it, e.g. `bun add -g opencode-ai`, "
                    "or set providers.opencode.bin in backend/llm.yaml / OPENCODE_BIN in backend/.env)")
        return self.accounts.problem()

    def list_models(self) -> list[dict[str, Any]]:
        """`opencode models` as [{id, name, free}], cached for ~10 minutes; [] when the CLI cannot be run."""
        binary = self._binary()
        if binary is None:
            return []
        now = time.monotonic()
        with _models_lock:
            hit = _models_cache.get(binary)
            if hit and now - hit[0] < MODELS_TTL:
                ids = hit[1]
            else:
                ids = None
        if ids is None:
            try:
                proc = subprocess.run(
                    [binary, "models", *(["--standalone"] if self.isolated else [])], capture_output=True, text=True,
                    env=self._env(self._workdir()), cwd=self._workdir(),
                    stdin=subprocess.DEVNULL, timeout=MODELS_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                log.warning("opencode models failed: %s", e)
                return []
            if proc.returncode != 0:
                log.warning("opencode models exited %s: %s", proc.returncode, " ".join((proc.stderr or "").split())[:200])
                return []
            ids = parse_models(proc.stdout)
            if ids:
                with _models_lock:
                    _models_cache[binary] = (now, ids)
        return [{"id": i, "name": i, "free": _is_free(i)} for i in ids]

    def suggested_models(self) -> list[dict[str, Any]]:
        out = [dict(m, vision=False) for m in self.list_models()]
        if self.model and all(m["id"] != self.model for m in out):
            out.insert(0, {"id": self.model, "name": self.model, "vision": False, "free": _is_free(self.model)})
        return out

    def thinking_choices(self) -> list[str] | None:
        return None  # the model's own `#variant` names (low, high, max, ...): any simple word

    def thinking_extra(self, level: str | None) -> dict[str, Any]:
        level = (level or "").strip().lower()
        if level and not re.fullmatch(r"[a-z0-9_.-]{1,32}", level):
            raise BadInput(f"bad thinking level {level!r}")
        return {"effort": level} if level else {}

    def info(self) -> dict[str, Any]:
        out = super().info()
        out["bin"] = self._binary() or self.bin
        out.update(self.accounts.info())
        return out

    # ---- request building --------------------------------------------------

    def build_command(self, req: ChatRequest, image_dir: str | None = None) -> tuple[list[str], dict[str, str]]:
        """(argv, env) of one `opencode run`; the prompt is the last argument.

        Images (data URLs) are written into ``image_dir`` and attached with ``-f``; the model must support vision.
        """
        images = [img for m in req.messages for img in m.images]
        if images and not image_dir:
            raise BadInput("the OpenCode provider needs an image_dir to attach images")
        files = [self._write_image(img, image_dir, i) for i, img in enumerate(images)] if images and image_dir else []
        message = self.build_message(req)
        model = self.model_arg(req)
        if not model:
            raise BadInput("no OpenCode model set, use opencode:<provider>/<model>")
        args = [self._binary() or self.bin, "run", *(["--standalone"] if self.isolated else []),
                "--format", "json", "-m", model, *[a for f in files for a in ("-f", f)], "--", message]
        return args, self._env(self._workdir())

    @staticmethod
    def _write_image(image: Any, folder: str, index: int) -> str:
        """Save a ``data:image/<type>;base64,...`` URL (or raw bytes) as a file and return its path."""
        if isinstance(image, (bytes, bytearray)):
            data, ext = bytes(image), "png"
        else:
            m = re.match(r"data:image/(png|jpe?g|gif|webp);base64,(.*)$", str(image), re.S)
            if not m:
                raise BadInput("OpenCode images must be png/jpeg/gif/webp data URLs")
            ext = "jpg" if m.group(1).startswith("jp") else m.group(1)
            try:
                data = base64.b64decode(m.group(2), validate=False)
            except ValueError as e:
                raise BadInput(f"bad image data: {e}") from e
        path = Path(folder) / f"image-{index + 1}.{ext}"
        path.write_bytes(data)
        return str(path)

    def model_arg(self, req: ChatRequest) -> str:
        """The model with its thinking level as `#variant` (a model that has one keeps it)."""
        model = req.model or self.model
        variant = self.variant(req.extra)
        if model and variant and "#" not in model:
            model = f"{model}#{variant}"
        return model

    def variant(self, extra: dict[str, Any]) -> str:
        value = extra.get("effort")
        reasoning = extra.get("reasoning")
        if value is None and isinstance(reasoning, dict):
            value = reasoning.get("effort") or ("low" if reasoning.get("enabled") is False else None)
        value = str(value or self.thinking or "").strip().lower()
        return "" if value == "off" else value

    def _env(self, cwd: str) -> dict[str, str]:
        """Environment of a run: the machine's, or (isolated) with none of its opencode config."""
        env = dict(os.environ)
        if self.isolated:
            config = Path(cwd) / ".config"
            try:
                config.mkdir(exist_ok=True)
            except OSError:
                pass
            env.update(
                XDG_CONFIG_HOME=str(config),  # no global opencode.jsonc: no plugins, MCP servers, skills, AGENTS.md
                OPENCODE_DISABLE_CLAUDE_CODE="1", OPENCODE_DISABLE_PROJECT_CONFIG="1",
                OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_FILEWATCHER="1",
            )
        return env

    @staticmethod
    def build_message(req: ChatRequest) -> str:
        """System prompt + history + last user message as one text."""
        system = "\n\n".join(m.text for m in req.messages if m.role == "system" and m.text)
        turns = [m for m in req.messages if m.role != "system" and m.text]
        if not turns:
            raise BadInput("send a prompt")
        if not system and len(turns) == 1:
            return turns[0].text
        parts: list[str] = []
        if system:
            parts.append(f"[system instructions]\n{system}")
        for m in turns[:-1]:
            parts.append(f"[earlier {m.role} message]\n{m.text}")
        parts.append(f"[user message]\n{turns[-1].text}" if len(parts) else turns[-1].text)
        return "\n\n".join(parts)

    # ---- output parsing ----------------------------------------------------

    def parse_output(self, stdout: str, stderr: str = "", returncode: int | None = 0, model: str = "") -> ChatResult:
        answer: list[str] = []
        thinking: list[str] = []
        error: dict[str, Any] | None = None
        session: str | None = None
        finish: str | None = None
        cost = 0.0
        has_cost = False
        tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        seen_tokens = False
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            session = event.get("sessionID") or session
            part = event.get("part")
            if not isinstance(part, dict):
                part = {}
            kind = event.get("type")
            if kind == "text" and part.get("text"):
                answer.append(str(part["text"]))
            elif kind == "reasoning" and part.get("text"):
                thinking.append(str(part["text"]))
            elif kind == "step_finish":
                finish = part.get("reason") or finish
                if part.get("cost") is not None:
                    cost += float(part.get("cost") or 0)
                    has_cost = True
                t = part.get("tokens")
                if isinstance(t, dict):
                    seen_tokens = True
                    cache = t.get("cache")
                    if not isinstance(cache, dict):
                        cache = {}
                    tokens["input"] += int(t.get("input") or 0)
                    tokens["output"] += int(t.get("output") or 0)
                    tokens["reasoning"] += int(t.get("reasoning") or 0)
                    tokens["cache_read"] += int(cache.get("read") or 0)
                    tokens["cache_write"] += int(cache.get("write") or 0)
            elif kind == "error" and isinstance(event.get("error"), dict):
                error = event["error"]

        if error is not None:
            raise self._error(error)
        if not answer:
            detail = " ".join((stderr or stdout or "").split())[:300] or f"exit code {returncode}"
            raise LLMError(f"OpenCode gave no answer: {detail}", provider=self.name)

        usage: dict[str, Any] = {}
        if seen_tokens:
            prompt = tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
            usage = {
                "input_tokens": tokens["input"], "output_tokens": tokens["output"],
                "reasoning_tokens": tokens["reasoning"],
                "cache_read_input_tokens": tokens["cache_read"], "cache_creation_input_tokens": tokens["cache_write"],
                "prompt_tokens": prompt, "completion_tokens": tokens["output"],
                "total_tokens": prompt + tokens["output"],
            }
            if has_cost:
                usage["cost_usd"] = cost
        return ChatResult(
            text="".join(answer).strip(),
            model=model or self.model,  # the events do not name the model
            provider=self.name,
            finish_reason=finish,
            usage=usage,
            id=session,
            reasoning="\n".join(thinking) or None,
            raw={"events": "json", "sessionID": session},
        )

    def _error(self, error: dict[str, Any]) -> LLMError:
        kind = str(error.get("type") or error.get("name") or "")
        data = error.get("data")
        if not isinstance(data, dict):
            data = {}
        message = " ".join(str(error.get("message") or data.get("message") or kind or "error").split())[:300]
        response = error.get("response")
        if not isinstance(response, dict):
            response = {}
        status = error.get("status") or data.get("statusCode") or response.get("status")
        if not isinstance(status, int):
            status = _ERROR_STATUS.get(kind)
        if not isinstance(status, int):
            m = _STATUS.search(message)
            status = int(m.group(1)) if m else None
        label = f"OpenCode {status}" if status else "OpenCode"
        return LLMError(f"{label}: {message}", status_code=status, provider=self.name)

    # ---- helpers -----------------------------------------------------------

    def _binary(self) -> str | None:
        found = shutil.which(self.bin)
        if found is None and self.bin == "opencode":
            # Default path of `bun add -g`, often missing from the PATH of a server / service
            local = Path.home() / ".bun" / "bin" / "opencode"
            found = str(local) if local.is_file() else None
        return found

    @staticmethod
    def _workdir() -> str:
        """An empty directory to run in, so OpenCode loads no project files."""
        path = Path(tempfile.gettempdir()) / "fxnews-opencode"
        try:
            path.mkdir(exist_ok=True)
            return str(path)
        except OSError:
            return tempfile.gettempdir()

    def _retry_wait(self, attempt: int, exc: LLMError) -> float:
        wait = min(2.0**attempt, 10.0)
        log.warning("OpenCode %s, retry %d in %.1fs", exc.status_code, attempt + 1, wait)
        return wait


def parse_models(output: str) -> list[str]:
    """`opencode models` output -> ids (`provider/model`, one per line), in order, without repeats."""
    seen: set[str] = set()
    out: list[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if _MODEL_ID.match(line) and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _is_free(model_id: str) -> bool:
    low = model_id.lower()
    return low.endswith(":free") or low.endswith("-free")
