"""Built-in FX strategy templates -> Vibe-Trading `signal_engine.py` source.

Signals are set from data up to each bar's close and are NOT shifted: the sidecar engine fills at the next bar's open.
Parameters are coerced to int/float before being formatted into the source, so nothing user-supplied is spliced as code.
"""

from __future__ import annotations

from app.vibe.schemas import StrategySpec

_SMA = """\
import pandas as pd


class SignalEngine:
    def generate(self, data_map):
        out = {}
        for code, df in data_map.items():
            close = df["close"]
            fast = close.rolling(%(fast)d).mean()
            slow = close.rolling(%(slow)d).mean()
            sig = (fast > slow).astype(float) - (fast < slow).astype(float)
            out[code] = sig.where(slow.notna(), 0.0).fillna(0.0)
        return out
"""

_RSI = """\
import pandas as pd


class SignalEngine:
    def generate(self, data_map):
        out = {}
        for code, df in data_map.items():
            close = df["close"]
            delta = close.diff()
            gain = delta.clip(lower=0).ewm(alpha=1.0 / %(period)d, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / %(period)d, adjust=False).mean()
            rsi = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, float("nan")))
            sig = pd.Series(float("nan"), index=close.index)
            sig[rsi < %(low)f] = 1.0
            sig[rsi > %(high)f] = -1.0
            sig[(rsi >= 50.0 - 5.0) & (rsi <= 50.0 + 5.0)] = 0.0
            out[code] = sig.ffill().fillna(0.0)
        return out
"""

_DONCHIAN = """\
import pandas as pd


class SignalEngine:
    def generate(self, data_map):
        out = {}
        for code, df in data_map.items():
            close = df["close"]
            upper = df["high"].rolling(%(lookback)d).max().shift(1)
            lower = df["low"].rolling(%(lookback)d).min().shift(1)
            sig = pd.Series(float("nan"), index=close.index)
            sig[close > upper] = 1.0
            sig[close < lower] = -1.0
            out[code] = sig.ffill().fillna(0.0)
        return out
"""


def _num(params: dict[str, float], key: str, default: float, lo: float, hi: float) -> float:
    v = float(params.get(key, default))
    if not lo <= v <= hi:
        raise ValueError(f"strategy param {key!r} must be within [{lo}, {hi}]")
    return v


def render(spec: StrategySpec) -> str:
    p = spec.params
    if spec.kind == "custom":
        return spec.code or ""
    if spec.kind == "sma_cross":
        fast, slow = int(_num(p, "fast", 20, 2, 500)), int(_num(p, "slow", 50, 3, 1000))
        if fast >= slow:
            raise ValueError("sma_cross requires fast < slow")
        return _SMA % {"fast": fast, "slow": slow}
    if spec.kind == "rsi_reversion":
        low, high = _num(p, "low", 30, 1, 49), _num(p, "high", 70, 51, 99)
        return _RSI % {"period": int(_num(p, "period", 14, 2, 200)), "low": low, "high": high}
    if spec.kind == "donchian_breakout":
        return _DONCHIAN % {"lookback": int(_num(p, "lookback", 20, 2, 500))}
    raise ValueError(f"unknown strategy kind {spec.kind!r}")
