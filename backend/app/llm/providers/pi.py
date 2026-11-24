"""pi CLI (`pi --print`). Uses the logins pi already has on this machine (`pi auth login`), no API key.

Each request runs one headless `pi --print --mode json --no-session` process in an empty temp directory.
The toggles of the example command `pi -ne -ns -np -nc --no-mcp --no-tools --no-themes --system-prompt ""`
are the `flags` section of the config (all default to true); `--no-session` is always on (one-shot answers,
nothing to resume; sessions would otherwise pile up on disk).

Settings (app/llm/config.py, section "pi"): model, thinking, bin, timeout, system_prompt, flags, rotation.
- model: request model > providers.pi.model ("" = pi's own default model). It is pi's --model pattern
  (see `pi --list-models`), e.g. `google/gemini-3-pro` or `anthropic/claude-opus-5-5`.
- thinking: extra["effort"] > extra["reasoning"] (the OpenRouter option) > providers.pi.thinking.
  It is `--thinking`: off, minimal, low, medium, high, xhigh, max.
- system_prompt: the fallback `--system-prompt` when the request has no system message
  ("" = the request's system text is used and only a real prompt is ever sent).
- flags (all default true, set false to turn one off): no_extensions (-ne), no_skills (-ns),
  no_prompt_templates (-np), no_context_files (-nc), no_mcp, no_tools, no_themes.
- bin: providers.pi.bin, else `pi` on PATH, else ~/.pi/... is not probed: set bin explicitly when needed.
- temperature / max_tokens are not supported by the CLI; images are written to temp files and attached
  as @file arguments (the chosen model must support vision).

JSON events (one object per line on stdout; the interesting ones):
    {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Hi!"}],
      "model":"...","stopReason":"stop","usage":{"input":3,"output":23,"cacheRead":0,"cacheWrite":0,
      "totalTokens":26,"cost":{"total":0.003}}}}
    ... stopReason "error" carries errorMessage (auth expired, rate limit, ...).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import Settings, text
from ..schema import BadInput, ChatRequest, ChatResult, LLMError
from . import register
from .base import LLMProvider

log = logging.getLogger("app.llm")

DEFAULT_TIMEOUT = 180.0
THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}
_STATUS = re.compile(r"\b(4\d\d|5\d\d)\b")
_DATA_URL = re.compile(r"data:image/(png|jpe?g|gif|webp);base64,(.*)$", re.S)
# flags section name -> CLI argument (order fixed so commands are stable)
FLAGS: tuple[tuple[str, str], ...] = (
    ("no_extensions", "--no-extensions"),   # -ne
    ("no_skills", "--no-skills"),           # -ns
    ("no_prompt_templates", "--no-prompt-templates"),  # -np
    ("no_context_files", "--no-context-files"),        # -nc
    ("no_mcp", "--no-mcp"),
    ("no_tools", "--no-tools"),
    ("no_themes", "--no-themes"),
)
DEFAULT_SYSTEM = "You are a helpful assistant."


@register("pi")
class PiProvider(LLMProvider):
    name = "pi"
    label = "pi agent"
    thinking_levels = THINKING

    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.section(self.name)
        self.bin = text(cfg.get("bin")) or "pi"
        self.model = text(cfg.get("model"))
        self.thinking = text(cfg.get("thinking")).lower()
        self.system_prompt = text(cfg.get("system_prompt"))
        flags = cfg.get("flags") or {}
        if not isinstance(flags, dict):
            raise BadInput("providers.pi.flags must be a mapping of flag names to true/false")
        self.flags = {name: flags.get(name, True) for name, _ in FLAGS}
        try:
            self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT

    # ---- public API -------------------------------------------------------

    def chat(self, req: ChatRequest) -> ChatResult:
        with tempfile.TemporaryDirectory(prefix="fxnews-pi-img-") as tmp:
            args, env = self.build_command(req, image_dir=tmp)
            return self._run(lambda: subprocess.run(
                args, capture_output=True, text=True, env=env, cwd=self._workdir(),
                stdin=subprocess.DEVNULL, timeout=self.timeout))

    async def achat(self, req: ChatRequest) -> ChatResult:
        with tempfile.TemporaryDirectory(prefix="fxnews-pi-img-") as tmp:
            args, env = self.build_command(req, image_dir=tmp)
            return await self._arun(args, env)

    # ---- retries (a timeout is not retried; 429 / 5xx answers are) ---------

    def _run(self, fn) -> ChatResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                proc = fn()
            except subprocess.TimeoutExpired:
                raise LLMError(f"pi timed out after {self.timeout:.0f}s", provider=self.name) from None
            except OSError as e:
                raise LLMError(f"cannot run pi ({self.bin}): {e}", provider=self.name) from e
            try:
                return self.parse_output(proc.stdout, proc.stderr, proc.returncode)
            except LLMError as e:
                if attempt == attempts - 1 or e.status_code not in RETRY_STATUS:
                    raise
                wait = min(2.0**attempt, 10.0)
                log.warning("pi %s, retry %d in %.1fs", e.status_code, attempt + 1, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    async def _arun(self, args: list[str], env: dict[str, str]) -> ChatResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env, cwd=self._workdir())
            except OSError as e:
                raise LLMError(f"cannot run pi ({self.bin}): {e}", provider=self.name) from e
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise LLMError(f"pi timed out after {self.timeout:.0f}s", provider=self.name) from None
            try:
                return self.parse_output(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode)
            except LLMError as e:
                if attempt == attempts - 1 or e.status_code not in RETRY_STATUS:
                    raise
                wait = min(2.0**attempt, 10.0)
                log.warning("pi %s, retry %d in %.1fs", e.status_code, attempt + 1, wait)
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    # ---- request building --------------------------------------------------

    def build_command(self, req: ChatRequest, image_dir: str | None = None) -> tuple[list[str], dict[str, str]]:
        """(argv, env) of one `pi --print` run; the prompt is the last argument."""
        message, files = self.build_message(req), []
        if not message:
            raise BadInput("send a prompt, an image, or both")
        for i, image in enumerate(img for m in req.messages for img in m.images):
            files.append(self._write_image(image, image_dir, i))
        args = [self._binary() or self.bin, "--print", "--mode", "json", "--no-session",
                *(arg for name, arg in FLAGS if self.flags.get(name, True))]
        if self.model_arg(req):
            args += ["--model", self.model_arg(req)]
        effort = self.effort(req.extra)
        if effort:
            args += ["--thinking", effort]
        args += ["--system-prompt", self.system_arg(req), "--", message]
        args += [f"@{f}" for f in files]
        return args, dict(os.environ)

    def system_arg(self, req: ChatRequest) -> str:
        """The request's system text, else the configured one, else a plain assistant prompt."""
        system = "\n\n".join(m.text for m in req.messages if m.role == "system" and m.text)
        return system or self.system_prompt or DEFAULT_SYSTEM

    def model_arg(self, req: ChatRequest) -> str:
        return (req.model or self.model or "").strip()

    def effort(self, extra: dict[str, Any]) -> str:
        value = extra.get("effort")
        reasoning = extra.get("reasoning")
        if value is None and isinstance(reasoning, dict):
            value = reasoning.get("effort") or ("off" if reasoning.get("enabled") is False else None)
        value = str(value or self.thinking or "").strip().lower()
        if value and value not in THINKING:
            raise BadInput(f"unknown thinking '{value}', use one of {', '.join(THINKING)}")
        return value

    @staticmethod
    def build_message(req: ChatRequest) -> str:
        """History + last user message as one text (the CLI takes a single prompt)."""
        turns = [m for m in req.messages if m.role != "system" and (m.text or m.images)]
        if not turns:
            return ""
        if len(turns) == 1:
            return turns[0].text
        parts = [f"[earlier {m.role} message]\n{m.text}" for m in turns[:-1] if m.text]
        parts.append(f"[user message]\n{turns[-1].text}" if turns[-1].text else turns[-1].text)
        return "\n\n".join(parts)

    @staticmethod
    def _write_image(image: Any, folder: str | None, index: int) -> str:
        """Save a ``data:image/<type>;base64,...`` URL (or raw bytes) as a file and return its path."""
        if folder is None:
            raise BadInput("the pi provider needs an image_dir to attach images")
        if isinstance(image, (bytes, bytearray)):
            data, ext = bytes(image), "png"
        else:
            m = _DATA_URL.match(str(image))
            if not m:
                raise BadInput("pi images must be png/jpeg/gif/webp data URLs")
            ext = "jpg" if m.group(1).startswith("jp") else m.group(1)
            import base64
            try:
                data = base64.b64decode(m.group(2), validate=False)
            except ValueError as e:
                raise BadInput(f"bad image data: {e}") from e
        path = Path(folder) / f"image-{index + 1}.{ext}"
        path.write_bytes(data)
        return str(path)

    # ---- output parsing ----------------------------------------------------

    def parse_output(self, stdout: str, stderr: str = "", returncode: int | None = 0) -> ChatResult:
        answer: list[str] = []
        thinking: list[str] = []
        model = ""
        finish: str | None = None
        error: str | None = None
        usage: dict[str, Any] = {}
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "message_end":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            model = message.get("model") or model
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    answer.append(str(block["text"]))
                elif block.get("type") == "thinking":
                    thought = block.get("thinking") or block.get("content")
                    if thought:
                        thinking.append(str(thought))
            if message.get("stopReason"):
                finish = str(message["stopReason"])
                if finish == "error":
                    error = " ".join(str(message.get("errorMessage") or "error").split())[:300]
            usage = self._usage(message.get("usage")) or usage

        if error is not None:
            status = _STATUS.search(error)
            raise LLMError(f"pi: {error}", status_code=int(status.group(1)) if status else None, provider=self.name)
        if not answer:
            detail = " ".join((stderr or stdout or "").split())[:300] or f"exit code {returncode}"
            raise LLMError(f"pi gave no answer: {detail}", provider=self.name)
        return ChatResult(
            text="".join(answer).strip(),
            model=model or self.model_arg(ChatRequest([])),
            provider=self.name,
            finish_reason=finish,
            usage=usage,
            reasoning="\n".join(thinking) or None,
            raw={"events": "json"},
        )

    @staticmethod
    def _usage(raw: Any) -> dict[str, Any]:
        """pi's usage plus the OpenAI-style totals."""
        if not isinstance(raw, dict):
            return {}
        out = {k: raw[k] for k in ("input", "output", "cacheRead", "cacheWrite") if raw.get(k) is not None}
        prompt = int(raw.get("input") or 0) + int(raw.get("cacheRead") or 0) + int(raw.get("cacheWrite") or 0)
        completion = int(raw.get("output") or 0)
        out.update(prompt_tokens=prompt, completion_tokens=completion, total_tokens=int(raw.get("totalTokens") or prompt + completion))
        cost = raw.get("cost")
        if isinstance(cost, dict) and cost.get("total") is not None:
            out["cost_usd"] = cost["total"]
        return out

    # ---- helpers -----------------------------------------------------------

    def problem(self) -> str | None:
        if self._binary() is None:
            return (f"pi CLI '{self.bin}' not found (install it, run `pi auth login` once to log in, "
                    "or set providers.pi.bin in backend/llm.yaml)")
        if self.thinking and self.thinking not in THINKING:
            return f"pi thinking {self.thinking!r} is not one of {', '.join(THINKING)}"
        return None

    def suggested_models(self) -> list[dict[str, Any]]:
        """`pi --list-models` (the ids pi accepts for --model), empty when the CLI cannot be run."""
        binary = self._binary()
        if binary is None:
            return []
        try:
            proc = subprocess.run([binary, "--list-models"], capture_output=True, text=True,
                                  cwd=self._workdir(), stdin=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("pi --list-models failed: %s", e)
            return []
        out: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            m = re.match(r"\s*([\w.-]+/[\w./:-]+)", line)
            if m and not re.match(r"^(Commands|Options|Usage|Extension)", line):
                low = m.group(1).lower()
                out.append({"id": m.group(1), "name": m.group(1), "vision": False,
                            "free": low.endswith((":free", "-free"))})
        return out

    def info(self) -> dict[str, Any]:
        out = super().info()
        out["bin"] = self._binary() or self.bin
        out["flags"] = dict(self.flags)
        return out

    def _binary(self) -> str | None:
        return shutil.which(self.bin)

    @staticmethod
    def _workdir() -> str:
        """An empty directory to run in, so pi loads no project files or git repo."""
        path = Path(tempfile.gettempdir()) / "fxnews-pi"
        try:
            path.mkdir(exist_ok=True)
            return str(path)
        except OSError:
            return tempfile.gettempdir()
