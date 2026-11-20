"""Settings of the LLM layer: backend/llm.yaml (template: backend/llm.ex.yaml) plus the backend's env settings.

Ported from Bot_Trade/llm_service, where this was a YAML file only. The providers read their own section with
``settings.section("<provider>")`` and know nothing else:

    openrouter   model, thinking, rotation, api_keys, base_url, app_name, site_url
    groq         model, thinking, rotation, api_keys, base_url
    hive         model, rotation, api_keys, base_url
    claude_code  model, thinking, bin, rotation, accounts[{name, config_dir}]   (Claude Code CLI login, no key)
    opencode     model, thinking (the #variant), isolated, timeout, bin, rotation, accounts[{name, data_home, config_home}]

Sources, lowest to highest priority:

1. the defaults of app/config.py;
2. ``llm.yaml`` (path: ``LLM_CONFIG``, default ``backend/llm.yaml``; a relative path is from ``backend/``);
3. env / ``backend/.env`` (OPENROUTER_MODEL, GROQ_THINKING, LLM_TIMEOUT_SECONDS, ...): only a *non-empty, explicitly
   set* variable replaces the YAML value, so a default never hides the file.

Keys do not replace each other: a non-empty ``OPENROUTER_API_KEY`` / ``OPENROUTER_API_KEYS`` (and the Groq / Hive
ones) is appended after the keys of the YAML list, without repeats. Placeholder keys ("sk-or-v1-...") are ignored.

A bad YAML file raises ``ConfigError`` naming the file and the field. Unknown providers (and unknown fields) are
skipped with a warning. The file is read when the shared client is created (restart the backend after editing it).

A model is addressed as ``<provider>:<model>`` (e.g. ``openrouter:google/gemini-2.5-flash``), see client.parse_model_spec.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from app.config import BACKEND_DIR
from app.config import Settings as AppSettings
from app.config import get_settings

from .keys import ROTATIONS, is_placeholder

log = logging.getLogger("app.llm")

CONFIG_FILE = BACKEND_DIR / "llm.yaml"
EXAMPLE_FILE = BACKEND_DIR / "llm.ex.yaml"
CONFIG_ENV = "LLM_CONFIG"

TOP_LEVEL = ("default_provider", "timeout", "max_retries", "max_image_mb")

# Fields each provider reads from its section (see providers/*.py); other fields are reported and dropped.
_TEXT = ("model", "thinking", "base_url", "bin", "app_name", "site_url")
_SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "openrouter": ("model", "thinking", "rotation", "api_keys", "api_key", "base_url", "app_name", "site_url"),
    "groq": ("model", "thinking", "rotation", "api_keys", "api_key", "base_url"),
    "hive": ("model", "rotation", "api_keys", "api_key", "base_url"),
    "claude_code": ("model", "thinking", "bin", "rotation", "accounts"),
    "opencode": ("model", "thinking", "isolated", "timeout", "bin", "rotation", "accounts"),
}
# accounts[].<field> of the CLI providers (the same names as the ACCOUNT_FIELDS of providers/claude_code.py, opencode.py)
_ACCOUNT_FIELDS = {"claude_code": ("config_dir",), "opencode": ("data_home", "config_home")}


class ConfigError(ValueError):
    """llm.yaml cannot be read, or a value in it is wrong. The message names the file and the field."""


def text(value: Any) -> str:
    """A setting as stripped text (``False`` means "off": an unquoted ``off`` in YAML is a boolean)."""
    if value is None:
        return ""
    if value is False:
        return "off"
    if value is True:
        return "on"
    return str(value).strip()


def config_path(path: str | Path | None = None, app: AppSettings | None = None) -> Path:
    """The YAML file used: `path`, else LLM_CONFIG (env / backend/.env), else backend/llm.yaml."""
    raw = str(path or "").strip()
    if not raw:
        raw = (getattr(app, "llm_config", "") or os.environ.get(CONFIG_ENV, "")).strip()
    if not raw:
        return CONFIG_FILE
    file = Path(raw).expanduser()
    return file if file.is_absolute() else BACKEND_DIR / file


# ==================== reading and validating llm.yaml ====================

def read_yaml(file: Path) -> dict[str, Any]:
    """The mapping in `file`, checked (see parse_config). Raises ConfigError."""
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = " ".join(str(getattr(e, "problem", None) or e).split())[:200]
        raise ConfigError(f"{file}: not valid YAML{where}: {problem}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {file}: {e}") from None
    return parse_config(raw, file)


def parse_config(raw: Any, file: str | Path = "llm.yaml") -> dict[str, Any]:
    """Validate the loaded YAML and return ``{<top level fields>, "providers": {name: section}}``.

    Missing fields stay missing (the caller falls back to app/config.py). Raises ConfigError naming `file` and the
    field; unknown providers / fields are logged and left out.
    """
    from .providers import available  # noqa: PLC0415  (the providers import this module)

    if raw is None:
        return {"providers": {}}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{file}: must be a mapping (default_provider, providers, ...), got {type(raw).__name__}")
    out: dict[str, Any] = {}
    known = set(TOP_LEVEL) | {"providers"}
    for name in raw:
        if name not in known:
            log.warning("%s: unknown field %r ignored (known: %s)", file, name, ", ".join(sorted(known)))

    if raw.get("default_provider") not in (None, ""):
        value = text(raw["default_provider"])
        if value not in available():
            raise ConfigError(f"{file}: default_provider {value!r} is not a provider (use one of {', '.join(available())})")
        out["default_provider"] = value
    for name in ("timeout", "max_image_mb"):
        if raw.get(name) not in (None, ""):
            out[name] = _number(raw[name], f"{file}: {name}")
    if raw.get("max_retries") not in (None, ""):
        out["max_retries"] = _number(raw["max_retries"], f"{file}: max_retries", integer=True, minimum=0)

    providers = raw.get("providers")
    if providers is None:
        providers = {}
    if not isinstance(providers, Mapping):
        raise ConfigError(f"{file}: providers must be a mapping of provider sections (openrouter, groq, ...)")
    clean: dict[str, dict[str, Any]] = {}
    for name, section in providers.items():
        name = str(name)
        if name not in available():
            log.warning("%s: providers.%s is not a known provider (%s) and is ignored", file, name, ", ".join(available()))
            continue
        clean[name] = _parse_section(name, section, file)
    out["providers"] = clean
    return out


def _parse_section(provider: str, section: Any, file: str | Path) -> dict[str, Any]:
    where = f"{file}: providers.{provider}"
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ConfigError(f"{where} must be a mapping (model, rotation, ...), got {type(section).__name__}")
    allowed = _SECTION_FIELDS.get(provider)
    out: dict[str, Any] = {}
    for name, value in section.items():
        name = str(name)
        if allowed is not None and name not in allowed:
            log.warning("%s.%s is not read by this provider and is ignored (known: %s)", where, name, ", ".join(allowed))
            continue
        if value is None:
            continue
        if name in _TEXT:
            if isinstance(value, (Mapping, list, tuple)):
                raise ConfigError(f"{where}.{name} must be a single value, got {type(value).__name__}")
            out[name] = text(value)
        elif name == "isolated":
            if not isinstance(value, bool):
                raise ConfigError(f"{where}.isolated must be true or false, got {value!r}")
            out[name] = value
        elif name == "rotation":
            value = text(value).lower()
            if value and value not in ROTATIONS:
                raise ConfigError(f"{where}.rotation must be one of {', '.join(ROTATIONS)}, got {value!r}")
            out[name] = value
        elif name == "timeout":
            out[name] = _number(value, f"{where}.timeout")
        elif name in ("api_keys", "api_key"):
            out["api_keys"] = [*out.get("api_keys", []), *_parse_api_keys(value, f"{where}.{name}")]
        elif name == "accounts":
            out[name] = _parse_accounts(provider, value, f"{where}.accounts")
    if "thinking" in out:
        _check_thinking(provider, out["thinking"], f"{where}.thinking")
    if "base_url" in out and out["base_url"] and not out["base_url"].startswith(("http://", "https://")):
        raise ConfigError(f"{where}.base_url must start with http:// or https://, got {out['base_url']!r}")
    return out


def _parse_api_keys(value: Any, where: str) -> list[dict[str, str]]:
    """[{name, key}] from a string or a list of strings / {name, key}; empty keys and placeholders are dropped."""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        label = f"{where}[{index - 1}]" if isinstance(value, (list, tuple)) else where
        if isinstance(item, Mapping):
            unknown = sorted(set(map(str, item)) - {"name", "key", "api_key"})
            if unknown:
                raise ConfigError(f"{label}: unknown field(s) {', '.join(unknown)} (use name and key)")
            secret, name = item.get("key", item.get("api_key")), item.get("name")
        elif isinstance(item, (str, int)) and not isinstance(item, bool) or item is None:
            secret, name = item, None
        else:
            raise ConfigError(f"{label}: expected a key string or {{name, key}}, got {type(item).__name__}")
        if isinstance(secret, (Mapping, list, tuple, bool)):
            raise ConfigError(f"{label}.key must be a string, got {type(secret).__name__}")
        secret = text(secret)
        if not secret or is_placeholder(secret):
            continue  # template line (sk-...) or an empty slot
        out.append({"name": text(name) or f"key {index}", "key": secret})
    return out


def _parse_accounts(provider: str, value: Any, where: str) -> list[dict[str, str]]:
    fields = _ACCOUNT_FIELDS.get(provider)
    if fields is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{where} must be a list of {{name, {', '.join(fields)}}}, got {type(value).__name__}")
    out: list[dict[str, str]] = []
    for index, item in enumerate(value):
        label = f"{where}[{index}]"
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, Mapping):
            raise ConfigError(f"{label}: expected {{name, {', '.join(fields)}}}, got {type(item).__name__}")
        unknown = sorted(set(map(str, item)) - {"name", *fields})
        if unknown:
            raise ConfigError(f"{label}: unknown field(s) {', '.join(unknown)} (use name, {', '.join(fields)})")
        entry = {"name": text(item.get("name")) or f"account {index + 1}"}
        for f in fields:
            raw = item.get(f)
            if isinstance(raw, (Mapping, list, tuple, bool)):
                raise ConfigError(f"{label}.{f} must be a directory path, got {type(raw).__name__}")
            entry[f] = str(Path(text(raw)).expanduser()) if text(raw) else ""
        out.append(entry)
    return out


def _check_thinking(provider: str, value: str, where: str) -> None:
    from .providers import _REGISTRY  # noqa: PLC0415

    levels = tuple(getattr(_REGISTRY.get(provider), "thinking_levels", ()) or ())
    if not levels:
        return
    allowed = ("", *levels, *(("off",) if provider == "claude_code" else ()))
    if value.lower() not in allowed:
        raise ConfigError(f"{where} must be one of {', '.join(repr(a) for a in allowed)}, got {value!r}")


def _number(value: Any, where: str, integer: bool = False, minimum: float = 0.0) -> Any:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok:
        number = int(value) if integer else float(value)
        if integer and float(value) != number:
            ok = False
        elif number < minimum or (number == minimum and minimum == 0.0 and not integer):
            ok = False
    if not ok:
        what = "a whole number >= 0" if integer else "a number > 0"
        raise ConfigError(f"{where} must be {what}, got {value!r}")
    return number


# ==================== Settings ====================

def _env(app: AppSettings, name: str) -> Any:
    """The env value of an app setting when it was set explicitly and is not empty, else None."""
    if name not in app.model_fields_set:
        return None
    value = getattr(app, name)
    return None if value in ("", None) else value


def _merge_keys(*groups: Any) -> list[dict[str, str]]:
    """[{name, key}] of the YAML keys, then the env keys (comma separated strings), without repeats."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        if isinstance(group, str):
            group = [{"key": part.strip()} for part in group.split(",")]
        for item in group:
            secret = text(item.get("key"))
            if secret and secret not in seen:
                seen.add(secret)
                out.append({"name": text(item.get("name")) or f"key {len(out) + 1}", "key": secret})
    return out


@dataclass(frozen=True)
class Settings:
    default_provider: str = "openrouter"
    timeout: float = 120.0
    max_retries: int = 2
    max_image_mb: float = 10.0
    # Sections by provider name (each provider reads only its own, see providers/*.py)
    providers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    file: str = ""  # the YAML file these came from ("" = none)

    @property
    def max_image_bytes(self) -> int:
        return int(self.max_image_mb * 1024 * 1024)

    def section(self, name: str) -> dict[str, Any]:
        """Copy of providers.<name> ({} when there is none)."""
        return copy.deepcopy(dict(self.providers.get(name) or {}))

    @classmethod
    def from_app(cls, app: AppSettings | None = None, path: str | Path | None = None, **overrides) -> "Settings":
        """Settings from llm.yaml (when it exists) merged with the backend's env settings, then ``overrides``
        (any field, ``providers`` merged per provider). Raises ConfigError for a bad YAML file."""
        app = app or get_settings()
        file = config_path(path, app)
        data: dict[str, Any] = {"providers": {}}
        if file.is_file():
            data = read_yaml(file)
        else:
            log.info("LLM config %s not found: using env settings only", file)
        yml: Mapping[str, Mapping[str, Any]] = data["providers"]

        def pick(provider: str, key: str, env_name: str, default: Any) -> Any:
            """env (explicit, non-empty) > YAML > app default."""
            env = _env(app, env_name)
            if env is not None:
                return env
            return yml.get(provider, {}).get(key, default)

        rotation_env = _env(app, "llm_key_rotation")

        def rotation(provider: str) -> str:
            return rotation_env or yml.get(provider, {}).get("rotation") or app.llm_key_rotation

        providers: dict[str, dict[str, Any]] = {
            "openrouter": {
                "model": pick("openrouter", "model", "openrouter_model", app.openrouter_model),
                "thinking": pick("openrouter", "thinking", "openrouter_thinking", app.openrouter_thinking),
                "api_keys": _merge_keys(yml.get("openrouter", {}).get("api_keys", []), app.openrouter_api_key, app.openrouter_api_keys),
                "base_url": pick("openrouter", "base_url", "openrouter_base_url", app.openrouter_base_url),
                "rotation": rotation("openrouter"),
                "app_name": yml.get("openrouter", {}).get("app_name", "fxnews"),
                **({"site_url": yml["openrouter"]["site_url"]} if yml.get("openrouter", {}).get("site_url") else {}),
            },
            "groq": {
                "model": pick("groq", "model", "groq_model", app.groq_model),
                "thinking": pick("groq", "thinking", "groq_thinking", app.groq_thinking),
                "api_keys": _merge_keys(yml.get("groq", {}).get("api_keys", []), app.groq_api_key, app.groq_api_keys),
                "rotation": rotation("groq"),
                **({"base_url": yml["groq"]["base_url"]} if yml.get("groq", {}).get("base_url") else {}),
            },
            "hive": {
                "model": pick("hive", "model", "hive_model", app.hive_model),
                "api_keys": _merge_keys(yml.get("hive", {}).get("api_keys", []), app.hive_api_key, app.hive_api_keys),
                "rotation": rotation("hive"),
                **({"base_url": yml["hive"]["base_url"]} if yml.get("hive", {}).get("base_url") else {}),
            },
            "claude_code": {
                "model": pick("claude_code", "model", "claude_code_model", app.claude_code_model),
                "thinking": pick("claude_code", "thinking", "claude_code_thinking", app.claude_code_thinking),
                "rotation": rotation("claude_code"),
                "accounts": yml.get("claude_code", {}).get("accounts", []),
                **_bin(pick("claude_code", "bin", "claude_code_bin", "")),
            },
            "opencode": {
                "model": pick("opencode", "model", "opencode_model", app.opencode_model),
                "thinking": pick("opencode", "thinking", "opencode_thinking", app.opencode_thinking),
                **({"isolated": yml["opencode"]["isolated"]} if "isolated" in yml.get("opencode", {}) else {}),
                "timeout": pick("opencode", "timeout", "opencode_timeout_seconds", app.opencode_timeout_seconds),
                "rotation": rotation("opencode"),
                "accounts": yml.get("opencode", {}).get("accounts", []),
                **_bin(pick("opencode", "bin", "opencode_bin", "")),
            },
        }
        # Sections of providers this function does not know (a provider added later) pass through as written.
        for name, section in yml.items():
            providers.setdefault(name, dict(section))

        def top(key: str, env_name: str, default: Any) -> Any:
            env = _env(app, env_name)
            return env if env is not None else data.get(key, default)

        settings = cls(
            default_provider=top("default_provider", "llm_default_provider", app.llm_default_provider),
            timeout=top("timeout", "llm_timeout_seconds", app.llm_timeout_seconds),
            max_retries=top("max_retries", "llm_max_retries", app.llm_max_retries),
            max_image_mb=data.get("max_image_mb", cls.max_image_mb),
            providers=providers,
            file=str(file) if file.is_file() else "",
        )
        return settings.with_overrides(**overrides)

    def with_overrides(self, **overrides) -> "Settings":
        overrides = {k: v for k, v in overrides.items() if v is not None}
        providers = overrides.pop("providers", None)
        out = replace(self, **overrides) if overrides else self
        if providers:
            merged = {name: dict(section) for name, section in out.providers.items()}
            for name, section in providers.items():
                merged.setdefault(name, {}).update(section or {})
            out = replace(out, providers=merged)
        return out


def _bin(value: Any) -> dict[str, str]:
    return {"bin": text(value)} if text(value) else {}
