"""Several logins for a CLI provider (claude_code, opencode), used in turn like the API keys of keys.py.

A CLI has no API key: it uses the login stored in a config directory. One *account* is one such directory,
passed to the subprocess as an environment variable, so each account has its own login:

    providers:
      claude_code:
        rotation: failover            # or round_robin
        accounts:
          - {name: main}              # no directory = the machine's default login
          - {name: work, config_dir: ~/.claude-work}      # CLAUDE_CONFIG_DIR=~/.claude-work
      opencode:
        accounts:
          - {name: main}
          - {name: second, data_home: ~/.opencode-b}      # XDG_DATA_HOME=~/.opencode-b (holds auth.json)

Without ``accounts`` nothing changes: one run with the inherited environment and no rotation. With accounts the
pool works like KeyPool: failover keeps using the first working account, round_robin spreads the requests, and an
account whose run fails with a rate-limit / usage-limit / auth error rests for a while while the next one answers
the same request. (Log in to an account once: ``CLAUDE_CONFIG_DIR=~/.claude-work claude`` then /login,
``XDG_DATA_HOME=~/.opencode-b opencode auth login``.)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, TypeVar

from .keys import ROTATIONS, KeyPool, NoKeyAvailable, describe_wait, shared_pool
from .schema import BadInput, LLMError

log = logging.getLogger("app.llm")

T = TypeVar("T")

AUTH_REST = 3600.0    # seconds an account with a dead login rests
LIMIT_REST = 1800.0   # a usage limit without a reset time
RATE_REST = 60.0      # a short rate limit

_AUTH_WORDS = ("not logged in", "please run /login", "/login", "invalid api key", "invalid x-api-key",
               "authentication", "unauthorized", "oauth", "token expired", "expired token", "credentials",
               "login required", "not authenticated")
_LIMIT_WORDS = ("usage limit", "limit reached", "quota", "out of credits", "insufficient", "credit balance",
                "billing", "exceeded your")
_RATE_WORDS = ("rate limit", "rate_limit", "too many requests", "overloaded")
_RESET_EPOCH = re.compile(r"\|\s*(\d{10})\b")  # Claude Code: "Claude AI usage limit reached|1760000000"


def account_problem(exc: LLMError, now: Optional[float] = None) -> Optional[tuple[str, float, bool]]:
    """(reason, rest seconds, hard) when a CLI failure means "this account cannot be used", else None.

    hard: the account rests even when it is the last one that works; soft limits only move the request
    to another account. Timeouts, bad input and "no answer" errors are not the account's fault.
    """
    if isinstance(exc, BadInput):
        return None
    now = time.time() if now is None else now
    status = exc.status_code
    message = str(exc)
    low = message.lower()
    short = " ".join(message.split())[:160]
    if "timed out" in low or "cannot run" in low:
        return None
    if status in (401, 403) or any(w in low for w in _AUTH_WORDS):
        return f"login refused: {short}", AUTH_REST, True
    if status == 402 or any(w in low for w in _LIMIT_WORDS):
        match = _RESET_EPOCH.search(message)
        wait = float(match.group(1)) - now if match else LIMIT_REST
        return f"usage limit: {short}", max(60.0, wait), True
    if status == 429 or any(w in low for w in _RATE_WORDS):
        return f"rate limit: {short}", RATE_REST, False
    return None


def parse_accounts(raw: Any, fields: Mapping[str, str]) -> list[dict[str, Any]]:
    """[{name, <field>: value...}] from the YAML value, without repeats. ``fields`` is {yaml field: ENV VAR}.

    An entry may have no directory (the machine's default login). Unknown entries are skipped; config.py
    reports them before this runs, so this only normalises.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, Mapping):
            continue
        values = {f: str(item.get(f) or "").strip() for f in fields}
        signature = tuple(values[f] for f in fields)
        if signature in seen:
            continue
        seen.add(signature)
        name = str(item.get("name") or "").strip() or f"account {index}"
        out.append({"name": name, **values})
    return out


@dataclass
class AccountRunner:
    """Runs a CLI request on the accounts of one provider (``pool is None``: no accounts, run as before)."""

    provider: str
    label: str
    pool: Optional[KeyPool]
    envs: list[dict[str, str]]  # extra environment of each pool key, same order

    @classmethod
    def from_section(cls, provider: str, label: str, cfg: Mapping[str, Any], fields: Mapping[str, str]) -> "AccountRunner":
        accounts = parse_accounts(cfg.get("accounts"), fields)
        if not accounts:
            return cls(provider, label, None, [])
        rotation = str(cfg.get("rotation") or "failover").strip().lower()
        rotation = rotation if rotation in ROTATIONS else "failover"
        envs = [{env: a[f] for f, env in fields.items() if a[f]} for a in accounts]
        entries = [(a["name"], ";".join(f"{k}={v}" for k, v in env.items())) for a, env in zip(accounts, envs)]
        pool = shared_pool(provider, entries, rotation, noun="account", public=True)
        return cls(provider, label, pool, envs)

    # ---- display ----------------------------------------------------------

    def problem(self) -> Optional[str]:
        """Why no request can be sent now (every account rests), or None."""
        reason = self.pool.problem() if self.pool is not None else None
        return f"{self.label}: {reason}" if reason else None

    def info(self) -> dict[str, Any]:
        if self.pool is None:
            return {}
        return {"rotation": self.pool.rotation, "accounts": self.pool.status()}

    # ---- running ----------------------------------------------------------

    def _pick(self):
        assert self.pool is not None
        try:
            key = self.pool.pick()
        except NoKeyAvailable as e:
            raise LLMError(f"{self.label}: {e}", status_code=e.status_code, provider=self.provider) from None
        index = next(i for i, k in enumerate(self.pool.keys) if k is key)  # identity: ApiKey equality is by value
        return key, self.envs[index]

    def _failed(self, key, exc: LLMError) -> bool:
        """Handle a failed run; True = try the next account (the failed one now rests)."""
        pool = self.pool
        assert pool is not None
        problem = account_problem(exc, pool.now())
        if problem is None:
            pool.note_error(key, " ".join(str(exc).split())[:160])
            return False
        reason, seconds, hard = problem
        if not (hard or pool.has_other(key)):
            pool.note_error(key, reason)
            return False
        pool.rest(key, reason, seconds, exc.status_code, floor=5.0)
        log.warning("%s account %s %s; resting %s", self.label, key.name, reason, describe_wait(seconds))
        return pool.problem() is None  # another account is ready

    def run(self, fn: Callable[[dict[str, str]], T]) -> T:
        """fn(extra_env) on the next account, moving to the next one while accounts fail for their own reasons."""
        if self.pool is None:
            return fn({})
        while True:
            key, env = self._pick()
            try:
                result = fn(env)
            except LLMError as exc:
                if self._failed(key, exc):
                    continue
                raise
            self.pool.succeeded(key)
            return result

    async def arun(self, fn: Callable[[dict[str, str]], Awaitable[T]]) -> T:
        if self.pool is None:
            return await fn({})
        while True:
            key, env = self._pick()
            try:
                result = await fn(env)
            except LLMError as exc:
                if self._failed(key, exc):
                    continue
                raise
            self.pool.succeeded(key)
            return result
