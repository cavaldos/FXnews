"""Claude Code CLI (`claude -p`). Uses the Claude login of this machine (`claude auth status`), no API key.

Each request runs one headless `claude -p` process with no tools, no MCP servers and no
CLAUDE.md / hooks / skills (--safe-mode), so the model sees only the system prompt and the
message. Images go in as base64 content blocks through `--input-format stream-json`.

Settings (app/llm/config.py, section "claude_code"): model, thinking (the --effort level), bin, rotation, accounts.
- accounts: several logins, each a CLAUDE_CONFIG_DIR (see accounts.py); the next one takes over when one hits a
  usage / rate limit or its login is refused.
- model: request model > providers.claude_code.model (default claude-haiku-5-5).
- effort: extra["effort"] > extra["reasoning"] (the OpenRouter option: {"effort": "low"}, or
  {"enabled": False} -> "low", since thinking cannot be fully turned off here) >
  providers.claude_code.thinking ("off" also means "low").
- max_tokens is passed as CLAUDE_CODE_MAX_OUTPUT_TOKENS; temperature is ignored (the CLI has no option).
- History turns are sent as labelled text in the one user message (the CLI takes no assistant turns).
- A timeout is not retried (a late answer is useless to the bot); 429 / 5xx answers are.
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

from ..accounts import AccountRunner
from ..config import Settings, text
from ..schema import BadInput, ChatRequest, ChatResult, LLMError, Message
from . import register
from .base import LLMProvider

log = logging.getLogger("app.llm")

ACCOUNT_FIELDS = {"config_dir": "CLAUDE_CONFIG_DIR"}  # accounts[].<field> -> environment variable of the CLI
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-haiku-5-5"
# Offered on the web (Claude Code takes any model name or alias its login can use)
MODELS = (
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5 (fastest)"},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5"},
    {"id": "claude-fable-5-2", "name": "Claude Fable 5.2 (most capable, slowest)"},
    {"id": "opus", "name": "Opus (alias, latest Opus)"},
    {"id": "sonnet", "name": "Sonnet (alias, latest Sonnet)"},
    {"id": "haiku", "name": "Haiku (alias, latest Haiku)"},
)
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}
DEFAULT_SYSTEM = "You are a helpful assistant."  # an empty --system-prompt would bring back Claude Code's own
_DATA_URL = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$", re.DOTALL)


@register("claude_code")
class ClaudeCodeProvider(LLMProvider):
    name = "claude_code"
    label = "Claude Code"
    thinking_levels = EFFORTS

    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.section(self.name)
        self.bin = text(cfg.get("bin")) or "claude"
        self.model = text(cfg.get("model")) or DEFAULT_MODEL
        self.thinking = text(cfg.get("thinking")).lower()
        self.accounts = AccountRunner.from_section(self.name, self.label, cfg, ACCOUNT_FIELDS)

    # ---- public API -------------------------------------------------------

    def chat(self, req: ChatRequest) -> ChatResult:
        args, stdin, env = self.build_command(req)
        return self.accounts.run(lambda extra: self._chat_once(args, stdin, {**env, **extra}))

    async def achat(self, req: ChatRequest) -> ChatResult:
        args, stdin, env = self.build_command(req)
        return await self.accounts.arun(lambda extra: self._achat_once(args, stdin, {**env, **extra}))

    def _chat_once(self, args: list[str], stdin: str, env: dict[str, str]) -> ChatResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                proc = subprocess.run(
                    args, input=stdin, capture_output=True, text=True, env=env,
                    cwd=tempfile.gettempdir(), timeout=self.settings.timeout,
                )
            except subprocess.TimeoutExpired:
                raise LLMError(f"Claude Code timed out after {self.settings.timeout:.0f}s", provider=self.name) from None
            except OSError as e:
                raise LLMError(f"cannot run Claude Code ({args[0]}): {e}", provider=self.name) from e
            try:
                return self.parse_output(proc.stdout, proc.stderr, proc.returncode)
            except LLMError as e:
                if attempt == attempts - 1 or e.status_code not in RETRY_STATUS:
                    raise
                time.sleep(self._retry_wait(attempt, e))
        raise AssertionError("unreachable")

    async def _achat_once(self, args: list[str], stdin: str, env: dict[str, str]) -> ChatResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env, cwd=tempfile.gettempdir(),
                )
            except OSError as e:
                raise LLMError(f"cannot run Claude Code ({args[0]}): {e}", provider=self.name) from e
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), self.settings.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise LLMError(f"Claude Code timed out after {self.settings.timeout:.0f}s", provider=self.name) from None
            try:
                return self.parse_output(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode)
            except LLMError as e:
                if attempt == attempts - 1 or e.status_code not in RETRY_STATUS:
                    raise
                await asyncio.sleep(self._retry_wait(attempt, e))
        raise AssertionError("unreachable")

    def problem(self) -> str | None:
        if self._binary() is None:
            return f"Claude Code CLI '{self.bin}' not found (install it, run `claude` once to log in, or set providers.claude_code.bin in backend/llm.yaml or CLAUDE_CODE_BIN in backend/.env)"
        if self.thinking and self.thinking != "off" and self.thinking not in EFFORTS:
            return f"claude_code thinking {self.thinking!r} is not one of {', '.join(EFFORTS)}"
        return self.accounts.problem()

    def thinking_choices(self) -> list[str] | None:
        return ["off", *EFFORTS]  # "off" is accepted and means "low" (see effort())

    def suggested_models(self) -> list[dict[str, Any]]:
        out = [dict(m, vision=True, free=False) for m in MODELS]
        if self.model and all(m["id"] != self.model for m in out):
            out.insert(0, {"id": self.model, "name": self.model, "vision": True, "free": False})
        return out

    def info(self) -> dict[str, Any]:
        out = super().info()
        out["bin"] = self._binary() or self.bin
        out.update(self.accounts.info())
        return out

    # ---- request building --------------------------------------------------

    def build_command(self, req: ChatRequest) -> tuple[list[str], str, dict[str, str]]:
        """(argv, stdin line, env) of one `claude -p` run."""
        system = "\n\n".join(m.text for m in req.messages if m.role == "system" and m.text)
        turns = [m for m in req.messages if m.role != "system"]
        if not turns:
            raise BadInput("send a prompt, an image, or both")

        model = req.model or self.model
        effort = self.effort(req.extra)
        args = [
            self._binary() or self.bin, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--tools", "", "--strict-mcp-config", "--safe-mode", "--no-session-persistence",
            "--system-prompt", system or DEFAULT_SYSTEM,
        ]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]

        env = dict(os.environ)
        if req.max_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(req.max_tokens))
        line = {"type": "user", "message": {"role": "user", "content": self.build_content(turns)}}
        return args, json.dumps(line) + "\n", env

    @staticmethod
    def build_content(turns: list[Message]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for m in turns[:-1]:
            blocks += [_image_block(u) for u in m.images]
            blocks.append({"type": "text", "text": f"[earlier {m.role} message]\n{m.text}"})
        last = turns[-1]
        blocks += [_image_block(u) for u in last.images]
        if last.text:
            blocks.append({"type": "text", "text": last.text})
        return blocks

    def effort(self, extra: dict[str, Any]) -> str:
        value = extra.get("effort")
        reasoning = extra.get("reasoning")
        if value is None and isinstance(reasoning, dict):
            value = reasoning.get("effort") or ("low" if reasoning.get("enabled") is False else None)
        value = str(value or self.thinking or "").strip().lower()
        if value == "off":
            value = "low"  # thinking cannot be turned off through the CLI
        if value and value not in EFFORTS:
            raise BadInput(f"unknown effort '{value}', use one of {', '.join(EFFORTS)}")
        return value

    # ---- output parsing ----------------------------------------------------

    def parse_output(self, stdout: str, stderr: str = "", returncode: int | None = 0) -> ChatResult:
        result: dict[str, Any] | None = None
        model = ""
        thinking: list[str] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                model = event.get("model") or model
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking"):
                        thinking.append(block["thinking"])
            elif kind == "result":
                result = event

        if result is None:
            detail = " ".join((stderr or stdout or "").split())[:300] or f"exit code {returncode}"
            raise LLMError(f"Claude Code gave no answer: {detail}", provider=self.name)
        if result.get("is_error"):
            status = result.get("api_error_status")
            status = status if isinstance(status, int) else None
            msg = " ".join(str(result.get("result") or result.get("subtype") or "error").split())[:300]
            raise LLMError(f"Claude Code{f' {status}' if status else ''}: {msg}", status_code=status, provider=self.name)

        model = model or next(iter(result.get("modelUsage") or {}), "")
        return ChatResult(
            text=str(result.get("result") or ""),
            model=model,
            provider=self.name,
            finish_reason=result.get("stop_reason"),
            usage=_usage(result),
            id=result.get("session_id"),
            reasoning="\n".join(thinking) or None,
            raw=result,
        )

    # ---- helpers -----------------------------------------------------------

    def _binary(self) -> str | None:
        found = shutil.which(self.bin)
        if found is None and self.bin == "claude":
            # Default path of the native installer, often missing from PATH under tmux / services
            local = Path.home() / ".local" / "bin" / "claude"
            found = str(local) if local.is_file() else None
        return found

    def _retry_wait(self, attempt: int, exc: LLMError) -> float:
        wait = min(2.0**attempt, 10.0)
        log.warning("Claude Code %s, retry %d in %.1fs", exc.status_code, attempt + 1, wait)
        return wait


def _image_block(url: str) -> dict[str, Any]:
    m = _DATA_URL.match(url)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group("mime"), "data": m.group("data")}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _usage(result: dict[str, Any]) -> dict[str, Any]:
    """The CLI's usage plus OpenAI-style totals (prompt = new + cached input tokens)."""
    usage = dict(result.get("usage") or {})
    prompt = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    completion = int(usage.get("output_tokens") or 0)
    usage.update(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)
    if result.get("total_cost_usd") is not None:
        usage["cost_usd"] = result["total_cost_usd"]
    if result.get("duration_ms") is not None:
        usage["duration_ms"] = result["duration_ms"]
    return usage
