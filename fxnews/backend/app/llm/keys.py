"""Several API keys for one provider, used in turn so one dead key does not stop the requests.

    providers:                      # llm.yaml (app/llm/config.py); env keys are appended to the list
      openrouter:
        rotation: failover          # or round_robin
        api_keys:
          - name: main
            key: sk-or-v1-...
          - sk-or-v1-...            # a plain string works too (named "key 2")

- failover: every request uses the first key that works. When the provider refuses it (invalid
  or expired key, no credits, daily limit) the key rests and the next one takes over in the same
  request. A rested key is tried again once its rest is over, so the first key comes back by itself.
- round_robin: requests take the working keys in turn (spreads per-minute limits over the keys).

Pools are shared by every client with the same keys, so a key found dead by one client is
skipped by the others too. The provider decides which answers mean "this key" (see
providers/openrouter.py and hive.py key_problem); other errors are retried as usual.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

ROTATIONS = ("failover", "round_robin")


def parse_keys(raw: Any, keep_placeholders: bool = False) -> list[tuple[str, str]]:
    """[(name, key)] from the YAML value: a string, or a list of strings / {name, key}.

    Empty keys and template placeholders ("sk-or-v1-...") are left out, as are repeats.
    """
    if raw is None or raw is False:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        if isinstance(item, dict):
            secret = str(item.get("key") or item.get("api_key") or "").strip()
            name = str(item.get("name") or "").strip() or f"key {index}"
        else:
            secret = str(item or "").strip()
            name = f"key {index}"
        if not secret or secret in seen:
            continue
        if not keep_placeholders and is_placeholder(secret):
            continue
        seen.add(secret)
        out.append((name, secret))
    return out


def is_placeholder(secret: str) -> bool:
    return "..." in secret or secret.lower() in ("changeme", "your-key", "your_api_key")


def mask(secret: str) -> str:
    """sk-or-v1-...41ad: enough to tell keys apart, never enough to use one."""
    secret = str(secret or "")
    if len(secret) <= 12:
        return "..." + secret[-2:] if secret else ""
    return f"{secret[:9]}...{secret[-4:]}"


def describe_wait(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60}h{minutes % 60:02d}"


@dataclass
class ApiKey:
    name: str
    secret: str
    resting_until: float = 0.0   # not used before this time (epoch seconds)
    reason: str = ""             # why it rests
    uses: int = 0                # answered requests
    failures: int = 0            # times it was refused
    last_used: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0

    public: bool = False         # the "secret" is not one (a CLI account's directory): shown as it is

    @property
    def masked(self) -> str:
        if self.public:
            return self.secret or "(machine default)"
        return mask(self.secret)


class NoKeyAvailable(Exception):
    """Every key is resting (or there is none). The message says why and until when."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class KeyPool:
    def __init__(self, entries: Iterable[tuple[str, str]], rotation: str = "failover",
                 clock: Callable[[], float] = time.time, noun: str = "key", public: bool = False):
        self.noun = noun  # "key" for API keys, "account" for CLI logins (only used in messages)
        self.keys = [ApiKey(name, secret, public=public) for name, secret in entries]
        self.rotation = rotation if rotation in ROTATIONS else "failover"
        self._clock = clock
        self._lock = threading.Lock()
        self._turn = 0
        self._last_status: Optional[int] = None

    def __len__(self) -> int:
        return len(self.keys)

    def now(self) -> float:
        return self._clock()

    def pick(self) -> ApiKey:
        """The key for the next request. Raises NoKeyAvailable when every key rests."""
        with self._lock:
            now = self._clock()
            ready = [k for k in self.keys if k.resting_until <= now]
            if not ready:
                raise NoKeyAvailable(self._why_none(now), self._last_status)
            if self.rotation == "round_robin":
                key = ready[self._turn % len(ready)]
                self._turn += 1
            else:
                key = ready[0]
            key.last_used = now
            return key

    def has_other(self, key: ApiKey) -> bool:
        """Another key could take this one's place right now."""
        with self._lock:
            now = self._clock()
            return any(k is not key and k.resting_until <= now for k in self.keys)

    def succeeded(self, key: ApiKey) -> None:
        with self._lock:
            key.uses += 1
            key.reason = ""
            key.resting_until = 0.0

    def rest(self, key: ApiKey, reason: str, seconds: float, status: Optional[int] = None,
             floor: float = 30.0) -> None:
        """Do not use `key` for `seconds` (at least `floor`)."""
        with self._lock:
            now = self._clock()
            key.failures += 1
            key.reason = reason
            key.last_error = reason
            key.last_error_at = now
            key.resting_until = now + max(floor, float(seconds))
            self._last_status = status

    def note_error(self, key: ApiKey, message: str) -> None:
        """An error that is not the key's fault (the key stays in use)."""
        with self._lock:
            key.last_error = message
            key.last_error_at = self._clock()

    def problem(self) -> Optional[str]:
        """Why no request can be sent now, or None."""
        if not self.keys:
            return "no API key" if self.noun == "key" else f"no {self.noun}"
        with self._lock:
            now = self._clock()
            if any(k.resting_until <= now for k in self.keys):
                return None
            return self._why_none(now)

    def status(self) -> list[dict[str, Any]]:
        """Each key for display (never the key itself)."""
        with self._lock:
            now = self._clock()
            ready = [k for k in self.keys if k.resting_until <= now]
            active = ready[0] if ready and self.rotation == "failover" else None
            return [
                {
                    "name": k.name,
                    "masked": k.masked,
                    "state": "resting" if k.resting_until > now else ("active" if k is active or self.rotation == "round_robin" else "standby"),
                    "reason": k.reason if k.resting_until > now else "",
                    "restingUntil": k.resting_until if k.resting_until > now else None,
                    "uses": k.uses,
                    "failures": k.failures,
                    "lastUsed": k.last_used or None,
                    "lastError": k.last_error,
                    "lastErrorAt": k.last_error_at or None,
                }
                for k in self.keys
            ]

    def _why_none(self, now: float) -> str:
        if not self.keys:
            return "no API key" if self.noun == "key" else f"no {self.noun}"
        parts = [f"{k.name} ({k.reason or 'resting'}, {describe_wait(k.resting_until - now)} left)" for k in self.keys]
        count = f"the only {self.noun}" if len(self.keys) == 1 else f"all {len(self.keys)} {self.noun}s"
        return f"{count} unavailable: " + "; ".join(parts)


# ---- pools shared by clients with the same keys ----------------------------

_POOLS: dict[tuple, KeyPool] = {}
_POOLS_LOCK = threading.Lock()


def shared_pool(provider: str, entries: list[tuple[str, str]], rotation: str = "failover",
                noun: str = "key", public: bool = False) -> KeyPool:
    """The pool of these keys (made once): rest times survive a settings reload."""
    signature = (provider, tuple(secret for _, secret in entries))
    with _POOLS_LOCK:
        pool = _POOLS.get(signature)
        if pool is None:
            pool = _POOLS[signature] = KeyPool(entries, rotation, noun=noun, public=public)
        else:
            pool.rotation = rotation if rotation in ROTATIONS else "failover"
            for key, (name, _) in zip(pool.keys, entries):
                key.name = name  # renamed in the config
        return pool


def reset_shared_pools() -> None:
    """Forget every pool (tests)."""
    with _POOLS_LOCK:
        _POOLS.clear()
