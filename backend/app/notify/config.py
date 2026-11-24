"""Notification channels and rules: backend/notify.yaml (template: backend/notify.ex.yaml).

Same shape as llm.yaml: scalar settings on top, a ``channels`` section with one entry per channel type, and
``accounts`` (named entries holding the secret) under each channel. The Notifications page of the UI rewrites
the file; the file is read on every send, so a change applies without a restart.

Env fallback (like the LLM layer): ``DISCORD_WEBHOOK_URL`` and ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``
add one account each, so an existing .env setup keeps working.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import BACKEND_DIR, get_settings

log = logging.getLogger(__name__)

CONFIG_FILE = BACKEND_DIR / "notify.yaml"
EXAMPLE_FILE = BACKEND_DIR / "notify.ex.yaml"
CONFIG_ENV = "NOTIFY_CONFIG"

CHANNEL_TYPES = ("telegram", "discord")
IMPACTS = ("High", "Medium", "Low")

HEADER = """# Notification settings of the backend (app/notify). This is backend/notify.yaml (never committed: it holds
# the keys). The Notifications page of the UI edits this file; hand edits work too (no restart needed).
"""

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


class ConfigError(ValueError):
    """notify.yaml cannot be read, or a value in it is wrong. The message names the file and the field."""


class _Quoted(str):
    """A string written quoted, so a secret is never read back as another type (e.g. "190:20:30")."""


class _Dumper(yaml.SafeDumper):
    """Writes the quoted strings; the keys stay plain."""


def _quoted_representer(dumper: yaml.SafeDumper, data: _Quoted):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


_Dumper.add_representer(_Quoted, _quoted_representer)


@dataclass
class Account:
    """One notification account: a Telegram bot+chat or a Discord webhook."""

    name: str
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    webhook_url: str = ""


@dataclass
class Config:
    min_impact: str = "High"
    pre_event_lead_minutes: int = 10
    notify_analyses: bool = True
    notify_pre_event: bool = True
    min_confidence: int = 60
    channels: dict[str, list[Account]] = field(default_factory=dict)  # channel type -> accounts (the file)
    env_channels: dict[str, list[Account]] = field(default_factory=dict)  # the .env accounts (never written)
    file: str = ""

    def accounts(self, channel: str) -> list[Account]:
        return self.channels.get(channel) or []

    def all_accounts(self, channel: str) -> list[Account]:
        """The file's accounts plus the env ones."""
        return [*self.channels.get(channel, []), *self.env_channels.get(channel, [])]


# ── reading ──────────────────────────────────────────────────────────


def config_path() -> Path:
    """The YAML file used: NOTIFY_CONFIG (env / backend/.env) or backend/notify.yaml."""
    raw = (getattr(get_settings(), "notify_config", "") or os.environ.get(CONFIG_ENV, "")).strip()
    if not raw:
        return CONFIG_FILE
    file = Path(raw).expanduser()
    return file if file.is_absolute() else BACKEND_DIR / file


def _text(value: Any) -> str:
    if value is None or value is False:
        return ""
    if value is True:
        return "on"
    return str(value).strip()


def _bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{where} must be true or false, got {value!r}")


def _int(value: Any, where: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be a whole number, got {value!r}") from None
    if not low <= number <= high:
        raise ConfigError(f"{where} must be between {low} and {high}, got {number}")
    return number


def parse(raw: Any, file: str = "notify.yaml") -> Config:
    """Validate the loaded YAML and return the config. Raises ConfigError naming the file and the field."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{file}: must be a mapping (min_impact, channels, ...), got {type(raw).__name__}")
    cfg = Config()
    impact = _text(raw.get("min_impact")) or cfg.min_impact
    if impact not in IMPACTS:
        raise ConfigError(f"{file}: min_impact must be one of {', '.join(IMPACTS)}, got {impact!r}")
    cfg.min_impact = impact
    cfg.pre_event_lead_minutes = _int(raw.get("pre_event_lead_minutes", cfg.pre_event_lead_minutes),
                                      f"{file}: pre_event_lead_minutes", 0, 1440)
    cfg.notify_analyses = _bool(raw.get("notify_analyses", cfg.notify_analyses), f"{file}: notify_analyses")
    cfg.notify_pre_event = _bool(raw.get("notify_pre_event", cfg.notify_pre_event), f"{file}: notify_pre_event")
    cfg.min_confidence = _int(raw.get("min_confidence", cfg.min_confidence), f"{file}: min_confidence", 0, 100)

    channels = raw.get("channels") or {}
    if not isinstance(channels, Mapping):
        raise ConfigError(f"{file}: channels must be a mapping of channel types ({', '.join(CHANNEL_TYPES)})")
    for ctype, section in channels.items():
        ctype = str(ctype)
        if ctype not in CHANNEL_TYPES:
            log.warning("%s: channels.%s is not a known channel (%s) and is ignored", file, ctype,
                        ", ".join(CHANNEL_TYPES))
            continue
        if not isinstance(section, Mapping):
            raise ConfigError(f"{file}: channels.{ctype} must be a mapping with an accounts list")
        accounts = section.get("accounts") or []
        if not isinstance(accounts, (list, tuple)):
            raise ConfigError(f"{file}: channels.{ctype}.accounts must be a list")
        parsed: list[Account] = []
        for index, item in enumerate(accounts):
            parsed.append(_parse_account(ctype, item, f"{file}: channels.{ctype}.accounts[{index}]"))
        cfg.channels[ctype] = parsed
    return cfg


def _parse_account(ctype: str, item: Any, where: str) -> Account:
    if isinstance(item, str):  # a bare secret
        item = {"name": "main", "bot_token" if ctype == "telegram" else "webhook_url": item}
    if not isinstance(item, Mapping):
        raise ConfigError(f"{where}: expected a mapping with name and the secret, got {type(item).__name__}")
    unknown = sorted(set(map(str, item)) - {"name", "enabled", "bot_token", "chat_id", "webhook_url"})
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) {', '.join(unknown)}")
    account = Account(
        name=_text(item.get("name")) or "main",
        enabled=_bool(item.get("enabled", True), f"{where}.enabled"),
        bot_token=_text(item.get("bot_token")),
        chat_id=_text(item.get("chat_id")),
        webhook_url=_text(item.get("webhook_url")),
    )
    if ctype == "telegram" and not account.chat_id:
        raise ConfigError(f"{where}.chat_id is required for a Telegram account")
    return account


def load() -> Config:
    """The config: notify.yaml (when it exists) plus the env accounts.

    Raises ConfigError when the file is broken: the caller decides (the API shows the message, the
    background jobs skip the notifications).
    """
    file = config_path()
    cfg = Config()
    if file.is_file():
        cfg = parse(read_yaml(file), str(file))
        cfg.file = str(file)
    else:
        log.info("Notification config %s not found (copy %s to start): using the defaults and the env accounts",
                 file, EXAMPLE_FILE)
    _add_env_accounts(cfg)
    return cfg


def load_safe() -> Config | None:
    """The config, or None when the YAML file is broken (the caller logs and skips)."""
    try:
        return load()
    except ConfigError as exc:
        log.warning("%s", exc)
        return None


def read_yaml(file: Path) -> Any:
    try:
        return yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = " ".join(str(getattr(exc, "problem", None) or exc).split())[:200]
        raise ConfigError(f"{file}: not valid YAML{where}: {problem}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {file}: {exc}") from None


def _add_env_accounts(cfg: Config) -> None:
    """The env credentials add one account each (like the env keys of the LLM layer). They live outside the
    file, so saving from the UI never copies them into notify.yaml (and the .env stays the source)."""
    s = get_settings()
    if s.discord_webhook_url:
        _append(cfg.env_channels, "discord", Account(name="env", enabled=True, webhook_url=s.discord_webhook_url))
    if s.telegram_bot_token and s.telegram_chat_id:
        _append(cfg.env_channels, "telegram", Account(name="env", enabled=True, bot_token=s.telegram_bot_token,
                                                       chat_id=s.telegram_chat_id))


def _append(accounts: dict[str, list[Account]], channel: str, account: Account) -> None:
    items = accounts.setdefault(channel, [])
    if any(a.name == account.name for a in items):
        return
    items.append(account)


# ── writing ──────────────────────────────────────────────────────────


def to_yaml(cfg: Config, header: str = HEADER) -> str:
    """The file content for a config (the body is regenerated, the header comment is kept)."""
    data: dict[str, Any] = {
        "min_impact": _Quoted(cfg.min_impact),
        "pre_event_lead_minutes": cfg.pre_event_lead_minutes,
        "notify_analyses": cfg.notify_analyses,
        "notify_pre_event": cfg.notify_pre_event,
        "min_confidence": cfg.min_confidence,
    }
    channels = {t: {"accounts": [_account_yaml(a) for a in accs]}
                for t, accs in cfg.channels.items() if accs}
    if channels:
        data["channels"] = channels
    return header.rstrip("\n") + "\n\n" + yaml.dump(
        data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _account_yaml(account: Account) -> dict[str, Any]:
    out: dict[str, Any] = {"name": account.name, "enabled": account.enabled}
    if account.bot_token or account.chat_id:
        out["bot_token"] = _Quoted(account.bot_token)
        out["chat_id"] = _Quoted(account.chat_id)
    if account.webhook_url:
        out["webhook_url"] = _Quoted(account.webhook_url)
    return out


def write(cfg: Config, file: Path | None = None) -> Path:
    """Write the config to its YAML file (the file's own header comment is kept)."""
    target = file or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    header = HEADER
    if target.is_file():
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        head = [line for line in lines if line.startswith("#")]
        if head:
            header = "\n".join(head) + "\n"
    target.write_text(to_yaml(cfg, header), encoding="utf-8")
    return target


# ── the UI update ────────────────────────────────────────────────────


def apply_update(cfg: Config, rules: dict[str, Any] | None, accounts: list[dict[str, Any]] | None) -> Config:
    """Merge a UI payload into the config. A secret that is not sent keeps its value; "" clears it.

    An account is matched by its id first (so renaming an account keeps its secret), then by its name.
    """
    out = Config(min_impact=cfg.min_impact, pre_event_lead_minutes=cfg.pre_event_lead_minutes,
                 notify_analyses=cfg.notify_analyses, notify_pre_event=cfg.notify_pre_event,
                 min_confidence=cfg.min_confidence, channels={t: list(a) for t, a in cfg.channels.items()},
                 file=cfg.file)
    for key in ("min_impact", "pre_event_lead_minutes", "notify_analyses", "notify_pre_event", "min_confidence"):
        if rules and rules.get(key) is not None:
            setattr(out, key, rules[key])
    if out.min_impact not in IMPACTS:
        raise ConfigError(f"min_impact must be one of {', '.join(IMPACTS)}")
    out.pre_event_lead_minutes = _int(out.pre_event_lead_minutes, "pre_event_lead_minutes", 0, 1440)
    out.min_confidence = _int(out.min_confidence, "min_confidence", 0, 100)
    if accounts is None:
        return out

    previous = {(t, f"{t}:{a.name}"): a for t, accs in cfg.channels.items() for a in accs}
    by_name = {(t, a.name): a for t, accs in cfg.channels.items() for a in accs}
    channels: dict[str, list[Account]] = {}
    for item in accounts:
        ctype = str(item.get("channel") or "")
        if ctype not in CHANNEL_TYPES:
            continue
        if item.get("source") == "env":
            continue  # the .env accounts are never written to the file
        name = _text(item.get("name")) or "main"
        old = previous.get((ctype, str(item.get("id") or ""))) or by_name.get((ctype, name))
        account = Account(
            name=name,
            enabled=bool(item.get("enabled", True)),
            bot_token=_secret(item, "bot_token", old.bot_token if old else ""),
            chat_id=_text(item.get("chat_id")) or (old.chat_id if old else ""),
            webhook_url=_secret(item, "webhook_url", old.webhook_url if old else ""),
        )
        if ctype == "telegram" and not account.chat_id:
            raise ConfigError("a Telegram account needs a chat id")
        channels.setdefault(ctype, []).append(account)
    out.channels = channels
    return out


def _secret(item: dict[str, Any], key: str, current: str) -> str:
    """None = keep the stored secret, "" = clear it, a value = replace it."""
    value = item.get(key)
    if value is None:
        return current
    return _text(value)


# ── the API view ─────────────────────────────────────────────────────


def public(cfg: Config) -> dict[str, Any]:
    """The config as the API shows it: the secrets are reported as "set", never as values."""
    accounts: list[dict[str, Any]] = []
    for ctype, items in cfg.channels.items():
        for account in items:
            accounts.append({
                "id": f"{ctype}:{account.name}",
                "channel": ctype,
                "name": account.name,
                "enabled": account.enabled,
                "chat_id": account.chat_id,
                "secret_set": bool(account.bot_token or account.webhook_url),
                "source": "file",
            })
    for ctype, items in cfg.env_channels.items():
        for account in items:
            accounts.append({
                "id": f"{ctype}:{account.name}",
                "channel": ctype,
                "name": account.name,
                "enabled": account.enabled,
                "chat_id": account.chat_id,
                "secret_set": bool(account.bot_token or account.webhook_url),
                "source": "env",
            })
    return {
        "rules": {
            "min_impact": cfg.min_impact,
            "pre_event_lead_minutes": cfg.pre_event_lead_minutes,
            "notify_analyses": cfg.notify_analyses,
            "notify_pre_event": cfg.notify_pre_event,
            "min_confidence": cfg.min_confidence,
        },
        "accounts": accounts,
        "file": cfg.file,
    }
