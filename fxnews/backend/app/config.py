from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    app_version: str = "0.1.0"
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'fxnews.db'}"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Host header allow-list (Starlette TrustedHostMiddleware; the port is ignored, "*" disables the check).
    # Blocks DNS rebinding. Add your hostname here when serving from Docker / a LAN name.
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    # ── LLM (OpenRouter) ──
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "google/gemini-2.5-flash"
    # Per-symbol model override, JSON in env: {"XAUUSD": "anthropic/claude-sonnet-4.5"}
    openrouter_symbol_models: dict[str, str] = {}
    # Models offered in the UI dropdown (default model is always included)
    openrouter_suggested_models: list[str] = [
        "google/gemini-2.5-flash",
        "openai/gpt-4.1-mini",
        "anthropic/claude-sonnet-4.5",
        "deepseek/deepseek-chat",
        "qwen/qwen3-32b",
    ]
    llm_timeout_seconds: float = 60.0
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4000

    # ── Multi-provider LLM layer (app/llm, ported from Bot_Trade/llm_service) ──
    # The LLM layer is configured by backend/llm.yaml (template: llm.ex.yaml; several accounts/keys per provider).
    # The settings below are the env fallback/override: a non-empty key here is appended to that provider's
    # YAML key list, and an explicitly set model/thinking/... wins over the YAML. Keys are never defaulted/committed.
    # *_API_KEYS takes a comma list (rotation).
    llm_config: str = ""  # LLM_CONFIG: path of the YAML file ("" = backend/llm.yaml; a relative path is from backend/)
    llm_default_provider: str = "openrouter"
    llm_max_retries: int = 2
    llm_key_rotation: str = "failover"  # failover | round_robin
    openrouter_api_keys: str = ""  # extra OpenRouter keys, comma separated (OPENROUTER_API_KEY is the first)
    openrouter_thinking: str = ""  # "" = model default | off | low | medium | high
    groq_api_key: str = ""
    groq_api_keys: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_thinking: str = ""
    hive_api_key: str = ""
    hive_api_keys: str = ""
    hive_model: str = "deepseek-ai/deepseek-v4.1-flash-1017"
    claude_code_bin: str = ""  # path of the `claude` CLI ("" = look it up); uses the machine's Claude login, no key
    claude_code_model: str = "claude-haiku-5-5"
    claude_code_thinking: str = "low"
    opencode_bin: str = ""  # path of the `opencode` CLI ("" = PATH, then ~/.bun/bin/opencode); uses its own logins, no key
    opencode_model: str = "opencode/beam-free"  # <opencode provider>/<model>, see `opencode models`
    opencode_thinking: str = "high"  # the model's #variant ("" / off = none)
    opencode_timeout_seconds: float = 180.0

    # ── Data import / export (app/services/datastore.py) ──
    data_import_max_mb: int = 500  # largest accepted import upload
    data_backup_keep: int = 10  # automatic pre-import / pre-restore snapshots kept (oldest are pruned)
    data_backup_dir: str = ""  # "" = <directory of the SQLite file>/backups (backend/data/backups by default)

    # ── Backtest dataset (app/backtest) ──
    # Models compared by the prediction runner, as "<provider>:<model>" (providers: openrouter, groq, hive, claude_code, opencode).
    backtest_models: list[str] = [
        "openrouter:google/gemini-2.5-flash",
        "openrouter:openai/gpt-4.1-mini",
        "openrouter:anthropic/claude-sonnet-4.5",
        "claude_code:claude-haiku-5-5",
    ]
    # Candle intervals tried per event, finest first; the first one whose history reaches the event is used
    # (5m only reaches ~3.5 weeks back, 15m ~2.5 months, 1h ~10 months, 1d years). Stored in bt_outcomes.interval.
    backtest_candle_intervals: list[str] = ["5m", "15m", "1h", "1d"]
    backtest_candle_delay_seconds: float = 2.0  # pause between per-symbol candle downloads (provider rate limits)
    backtest_max_symbols: int = 3  # outcomes/predictions for at most this many related symbols per event
    backtest_primary_horizon: str = "1h"  # horizon behind the single "direction" label of an outcome
    backtest_llm_min_interval_seconds: float = 1.0  # pause between LLM calls of the runner (rate limits)
    backtest_llm_concurrency: int = 2  # parallel LLM calls (per run)
    backtest_prompt_context_items: int = 8  # earlier related events shown in a prompt (strictly before the release); 0 = off
    backtest_prompt_context_hours: float = 24.0  # how far before the release those events may lie

    # ── Historical headlines for the backtest (app/backtest/headline_history.py) ──
    # RSS feeds only expose their latest items, so past headlines come from public archives. Each source can be
    # switched off; one failing source never fails the run.
    headline_history_enabled: bool = True
    headline_history_investinglive: bool = True  # investinglive.com (ex ForexLive) weekly sitemaps, 2026-01..now
    headline_history_investinglive_fetch_titles: bool = False  # read og:title of each page instead of the URL slug
    headline_history_fxstreet_wayback: bool = True  # FXStreet article URLs (publish time is in the URL) via web.archive.org CDX
    headline_history_cnbc: bool = True  # CNBC sitemap (date only) + article page head for the publish time
    headline_history_cnbc_max_page_fetches: int = 1500  # page-head requests per run (finance-relevant titles first)
    headline_history_gdelt: bool = True  # GDELT DOC 2.0 article search (no key; 1 request / 5 s)
    headline_history_gdelt_chunk_hours: int = 24
    headline_history_gdelt_lookback_days: int = 100  # DOC ArtList only serves ~3 months; 0 = no limit
    headline_history_gdelt_min_interval_seconds: float = 5.5
    headline_history_marketaux_api_key: str = ""  # free key https://www.marketaux.com ; "" = skip
    headline_history_marketaux_max_pages: int = 3  # pages per day chunk (free plan: ~100 requests/day)
    headline_history_finnhub_api_key: str = ""  # free key https://finnhub.io ; "" = skip (recent items only)
    headline_history_relevant_only: bool = True  # keep only headlines with a mapped symbol or medium/high impact
    headline_history_max_total: int = 20000
    headline_history_request_delay_seconds: float = 1.0  # pause between requests to the same host

    # ── Market data ──
    # TradingView (public websocket, no key) is primary so candles match the TradingView chart exactly.
    tradingview_enabled: bool = True
    tradingview_max_sessions: int = 3  # concurrent TradingView websocket sessions, process-wide
    tradingview_connect_spacing_ms: int = 150  # minimum gap between starting two TradingView connections
    # Yahoo (no key) is the first fallback; Twelve Data (free key, 800 req/day) the last one.
    twelvedata_api_key: str = ""

    # ── Discord ──
    discord_webhook_url: str = ""
    notify_min_confidence: int = 60  # only push trade ideas with confidence >= this
    notify_pre_event: bool = True

    # ── Ingestion ──
    scheduler_enabled: bool = True
    ff_poll_minutes: float = 10.0  # faireconomy feed is rate limited; keep >= 5
    rss_poll_minutes: float = 3.0
    rss_feeds: dict[str, str] = Field(
        default_factory=lambda: {
            "fxstreet": "https://www.fxstreet.com/rss/news",
            "cnbc": "https://www.cnbc.com/id/10000664/device/rss/rss.html",  # finance/macro (Fed, rates)
        }
    )
    pre_event_alert_minutes: int = 30
    analyze_min_impact: str = "High"  # calendar events at/above this get pre_event analysis
    auto_analyze_headlines: bool = True
    max_headline_analyses_per_cycle: int = 5  # cost guard
    # Auto headline analysis: at most one per primary currency per this many minutes (the newest headline wins).
    headline_cooldown_minutes: float = 10.0
    # Hard budget for AUTOMATIC LLM calls (pre-event + headline) per rolling hour. Headlines stop when it is
    # spent; pre-event analyses are still run (they are the product) but are counted against it.
    max_auto_llm_calls_per_hour: int = 20
    watch_currencies: list[str] = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "XAU"]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url)

    def model_for(self, symbol: str | None = None, override: str | None = None) -> str:
        if override:
            return override
        if symbol and symbol.upper() in self.openrouter_symbol_models:
            return self.openrouter_symbol_models[symbol.upper()]
        return self.openrouter_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
