from datetime import date, timedelta

import pytest

from app.analysis import macro_context as mc
from app.config import get_settings
from app.vibe.client import VibeError
from app.vibe.schemas import MacroObservation, MacroSeries


def _series(sid, n=400, step=1.0):
    today = date.today()
    obs = [MacroObservation(date=str(today - timedelta(days=n - i)), value=4.0 + i * 0.001 * step) for i in range(n)]
    return MacroSeries(series_id=sid, observations=obs)


class Fake:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def get_macro_series(self, series_id, start=None, end=None, limit=2000):
        self.calls.append(series_id)
        if self.fail:
            raise VibeError("FRED_API_KEY is not configured")
        return _series(series_id)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mc.clear_cache()
    yield
    mc.clear_cache()


def _patch(monkeypatch, fake):
    monkeypatch.setattr("app.vibe.client.get_client", lambda: fake)


async def test_block_format(monkeypatch):
    fake = Fake()
    _patch(monkeypatch, fake)
    out = await mc.macro_block(["JPY"])
    assert out.startswith("# Macro snapshot (FRED via Vibe-Trading)")
    assert "Japan 10y JGB yield (%)" in out and "1m +" in out and "1y +" in out
    assert fake.calls == ["IRLTLT01JPM156N"]


async def test_cache(monkeypatch):
    fake = Fake()
    _patch(monkeypatch, fake)
    await mc.macro_block(["JPY"])
    await mc.macro_block(["JPY"])
    assert len(fake.calls) == 1


async def test_failure_returns_empty_and_is_remembered(monkeypatch):
    fake = Fake(fail=True)
    _patch(monkeypatch, fake)
    assert await mc.macro_block(["USD", "EUR"]) == ""
    n = len(fake.calls)
    assert await mc.macro_block(["USD", "EUR"]) == ""
    assert len(fake.calls) == n


async def test_unknown_currency(monkeypatch):
    fake = Fake()
    _patch(monkeypatch, fake)
    assert await mc.macro_block(["XAU"]) == ""
    assert fake.calls == []


async def test_disabled_not_called(monkeypatch):
    fake = Fake()
    _patch(monkeypatch, fake)
    s = get_settings()
    monkeypatch.setattr(s, "advisor_macro_enabled", False)
    monkeypatch.setattr(s, "vibe_enabled", True)
    assert await mc.macro_suffix(["USD"]) == ""
    assert fake.calls == []


async def test_suffix_enabled(monkeypatch):
    fake = Fake()
    _patch(monkeypatch, fake)
    s = get_settings()
    monkeypatch.setattr(s, "advisor_macro_enabled", True)
    monkeypatch.setattr(s, "vibe_enabled", True)
    assert "# Macro snapshot" in await mc.macro_suffix(["JPY"])


async def test_advisor_prompt_includes_block(monkeypatch):
    from app.analysis import advisor

    async def fake_suffix(cur):
        return "\n\n# Macro snapshot (FRED via Vibe-Trading)\n- x"

    seen = {}

    async def fake_run(model_id, user_msg, allowed):
        seen["msg"] = user_msg
        return {"summary": "s", "currency_impact": [], "trades": []}, None, 1, "{}", "llm"

    monkeypatch.setattr(advisor, "macro_suffix", fake_suffix)
    monkeypatch.setattr(advisor, "_run_llm", fake_run)
    try:
        await advisor.advise("EURUSD", question="outlook?")
    except Exception:
        pass
    assert "# Macro snapshot" in seen.get("msg", "")
