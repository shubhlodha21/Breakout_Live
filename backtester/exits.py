"""Exit rules.

An exit set is a list of rules; the trade closes on whichever fires FIRST (OR logic).

Rule hooks (all optional):
    prepare(df)        precompute arrays once per ticker/timeframe
    reset(t)           called at trade entry (t = TradeState)
    stop(j, t)         protective stop price active during bar j (NaN = none). Uses data up to bar j-1.
    target(j, t)       profit-target price active during bar j (NaN = none)
    after_bar(j, t)    called after bar j closes; return None or (reason, when) where
                       when in {"close", "next_open", None (= global default)}

Intrabar fill rules (engine): gap through stop -> fill at open; touch -> fill at stop.
If stop and target are both touched inside one bar, the STOP is assumed first (conservative).

Add a new rule: subclass ExitRule, decorate with @register("name").
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import indicators as ind

NAN = float("nan")
EXIT_REGISTRY: dict = {}


def register(name: str):
    def deco(cls):
        EXIT_REGISTRY[name] = cls
        return cls
    return deco


@dataclass
class TradeState:
    signal_idx: int
    entry_idx: int
    entry_price: float
    hh: float              # highest high since entry (completed bars)
    hc: float              # highest close since entry (completed bars)
    risk: float = NAN      # entry - initial stop (for R-multiples)
    extra: dict = field(default_factory=dict)


class ExitRule:
    reason = "Exit"

    def __init__(self, label: str | None = None, **params):
        if label:
            self.reason = label
        self.p = params

    def prepare(self, df):
        pass

    def reset(self, t: TradeState):
        pass

    def stop(self, j: int, t: TradeState) -> float:
        return NAN

    def target(self, j: int, t: TradeState) -> float:
        return NAN

    def after_bar(self, j: int, t: TradeState):
        return None


def _need_one(name, **kw):
    given = [k for k, v in kw.items() if v]
    if len(given) != 1:
        raise ValueError(f"{name}: specify exactly one of {list(kw)}")


# ---------------------------------------------------------------------------- stops
@register("fixed_sl")
class FixedSL(ExitRule):
    """Fixed stop below entry: pct (e.g. 8 = 8%) or atr_mult x ATR(atr_period) at the signal bar."""
    reason = "Fixed SL"

    def __init__(self, pct=None, atr_mult=None, atr_period=14, label=None):
        super().__init__(label)
        _need_one("fixed_sl", pct=pct, atr_mult=atr_mult)
        self.pct, self.atr_mult, self.atr_period = pct, atr_mult, atr_period

    def prepare(self, df):
        if self.atr_mult:
            self.atr = ind.atr(df, self.atr_period).to_numpy()

    def reset(self, t):
        if self.pct:
            self.level = t.entry_price * (1 - self.pct / 100)
        else:
            a = self.atr[t.signal_idx]
            self.level = t.entry_price - self.atr_mult * a if np.isfinite(a) else NAN

    def stop(self, j, t):
        return self.level


@register("trailing_sl")
class TrailingSL(ExitRule):
    """Trailing stop from the highest high (or close) since entry.
    pct=15 -> trail 15% below the peak;  atr_mult=3 -> peak - 3 x ATR (chandelier).
    activate_pct=10 -> trailing only starts once the peak is 10% above entry."""
    reason = "Trailing SL"

    def __init__(self, pct=None, atr_mult=None, atr_period=14, source="high", activate_pct=0, label=None):
        super().__init__(label)
        _need_one("trailing_sl", pct=pct, atr_mult=atr_mult)
        self.pct, self.atr_mult, self.atr_period = pct, atr_mult, atr_period
        self.source, self.activate_pct = source, activate_pct

    def prepare(self, df):
        if self.atr_mult:
            self.atr = ind.atr(df, self.atr_period).to_numpy()

    def stop(self, j, t):
        peak = t.hh if self.source == "high" else t.hc
        if self.activate_pct and peak < t.entry_price * (1 + self.activate_pct / 100):
            return NAN
        if self.pct:
            return peak * (1 - self.pct / 100)
        a = self.atr[j - 1]
        return peak - self.atr_mult * a if np.isfinite(a) else NAN


@register("channel_trail")
class ChannelTrail(ExitRule):
    """Stop at the lowest low of the previous `period` bars (Donchian / Turtle style exit)."""
    reason = "Channel Low"

    def __init__(self, period=10, label=None):
        super().__init__(label)
        self.period = period

    def prepare(self, df):
        self.low = ind.prior_low_bars(df, self.period).to_numpy()

    def stop(self, j, t):
        return self.low[j]


@register("breakeven")
class Breakeven(ExitRule):
    """Move stop to entry (+offset_pct) once price has risen trigger_pct % (or trigger_r x risk)."""
    reason = "Breakeven"

    def __init__(self, trigger_pct=None, trigger_r=None, offset_pct=0.0, label=None):
        super().__init__(label)
        _need_one("breakeven", trigger_pct=trigger_pct, trigger_r=trigger_r)
        self.trigger_pct, self.trigger_r, self.offset_pct = trigger_pct, trigger_r, offset_pct

    def stop(self, j, t):
        if self.trigger_pct:
            trig = t.entry_price * (1 + self.trigger_pct / 100)
        elif np.isfinite(t.risk):
            trig = t.entry_price + self.trigger_r * t.risk
        else:
            return NAN
        return t.entry_price * (1 + self.offset_pct / 100) if t.hh >= trig else NAN


# ---------------------------------------------------------------------------- targets
@register("target")
class Target(ExitRule):
    """Profit target: pct above entry, r_multiple x initial risk (needs a stop), or atr_mult x ATR."""
    reason = "Target"

    def __init__(self, pct=None, r_multiple=None, atr_mult=None, atr_period=14, label=None):
        super().__init__(label)
        _need_one("target", pct=pct, r_multiple=r_multiple, atr_mult=atr_mult)
        self.pct, self.r_multiple, self.atr_mult, self.atr_period = pct, r_multiple, atr_mult, atr_period

    def prepare(self, df):
        if self.atr_mult:
            self.atr = ind.atr(df, self.atr_period).to_numpy()

    def reset(self, t):
        self.level = None

    def target(self, j, t):
        if self.level is None:  # computed lazily: risk is known only after all stops are reset
            if self.pct:
                self.level = t.entry_price * (1 + self.pct / 100)
            elif self.r_multiple:
                self.level = t.entry_price + self.r_multiple * t.risk if np.isfinite(t.risk) else NAN
            else:
                a = self.atr[t.signal_idx]
                self.level = t.entry_price + self.atr_mult * a if np.isfinite(a) else NAN
        return self.level


@register("risk_reward")
class RiskReward(ExitRule):
    """Stop-loss + take-profit at a fixed risk:reward. sl_pct=2, rr=2 -> SL 2% below entry, TP 4% above (1:2)."""
    reason = "RR"
    stop_reason, target_reason = "RR Stop Loss", "RR Take Profit"

    def __init__(self, sl_pct=2.0, rr=2.0, label=None):
        super().__init__(label)
        self.sl_pct, self.rr = sl_pct, rr

    def reset(self, t):
        self.sl = t.entry_price * (1 - self.sl_pct / 100)
        self.tp = t.entry_price * (1 + self.sl_pct * self.rr / 100)

    def stop(self, j, t):
        return self.sl

    def target(self, j, t):
        return self.tp


# ---------------------------------------------------------------------------- signal exits
@register("ma_exit")
class MAExit(ExitRule):
    """Exit when close crosses below a moving average (e.g. 50 SMA). timeframe='1D' on intraday."""
    reason = "MA Exit"

    def __init__(self, period=50, kind="sma", timeframe=None, label=None):
        super().__init__(label)
        self.period, self.kind, self.timeframe = period, kind, timeframe

    def prepare(self, df):
        fn = ind.sma if self.kind == "sma" else ind.ema
        ma = ind.on_timeframe(df, self.timeframe, lambda b: fn(b["close"], self.period))
        self.below = (df["close"] < ma).to_numpy()

    def after_bar(self, j, t):
        return (self.reason, None) if self.below[j] else None


@register("ma_cross_exit")
class MACrossExit(ExitRule):
    """Death cross: exit when the fast MA crosses BELOW the slow MA (e.g. EMA 50 below EMA 200).
    Only a cross that happens while in the trade triggers (not a state already present at entry)."""
    reason = "Death Cross"

    def __init__(self, fast=50, slow=200, kind="ema", label=None):
        super().__init__(label)
        self.fast, self.slow, self.kind = fast, slow, kind

    def prepare(self, df):
        fn = ind.sma if self.kind == "sma" else ind.ema
        below = fn(df["close"], self.fast) < fn(df["close"], self.slow)
        self.cross = (below & ~below.shift(1, fill_value=False)).to_numpy()

    def after_bar(self, j, t):
        return (self.reason, None) if self.cross[j] else None


@register("entry_invalid")
class EntryInvalid(ExitRule):
    """Exit when the trade's own entry condition is no longer valid: trend/filter conditions turn
    false, or the close falls back below the breakout level (e.g. the 52-week high) broken at entry."""
    reason = "Entry Invalid"

    def bind_entry(self, df, conditions):
        from .entries import build_hold
        self.state, self.levels = build_hold(df, conditions)

    def reset(self, t):
        lv = [a[t.signal_idx] for a in self.levels]
        self.level = max((v for v in lv if v == v), default=NAN)

    def after_bar(self, j, t):
        if not self.state[j]:
            return (self.reason, None)
        if self.level == self.level and self._close[j] < self.level:
            return ("Failed Breakout", None)
        return None

    def prepare(self, df):
        self._close = df["close"].to_numpy(dtype=float)
        if not hasattr(self, "state"):
            raise RuntimeError("entry_invalid needs bind_entry() before running")


@register("time_exit")
class TimeExit(ExitRule):
    """Exit at the close of the bar on which the trade has been held `bars` bars."""
    reason = "Time Exit"

    def __init__(self, bars=20, label=None):
        super().__init__(label)
        self.bars = bars

    def after_bar(self, j, t):
        return (self.reason, "close") if j - t.entry_idx + 1 >= self.bars else None


@register("eod_exit")
class EODExit(ExitRule):
    """Intraday: flat at the close of the last bar of each session (no overnight holding)."""
    reason = "EOD"

    def prepare(self, df):
        d = df.index.normalize().to_numpy()
        self.last_of_day = np.append(d[1:] != d[:-1], True)

    def after_bar(self, j, t):
        return (self.reason, "close") if self.last_of_day[j] else None


@register("lower_low_exit")
class LowerLowExit(ExitRule):
    """Exit when close is below the lowest low of the previous `period` bars (close-based, unlike channel_trail)."""
    reason = "Lower Low"

    def __init__(self, period=10, label=None):
        super().__init__(label)
        self.period = period

    def prepare(self, df):
        self.hit = (df["close"] < ind.prior_low_bars(df, self.period)).to_numpy()

    def after_bar(self, j, t):
        return (self.reason, None) if self.hit[j] else None


def build_exits(rules: list[dict]) -> list[ExitRule]:
    out = []
    for r in rules:
        params = {k: v for k, v in r.items() if k != "type"}
        if r["type"] not in EXIT_REGISTRY:
            raise ValueError(f"Unknown exit type '{r['type']}'. Available: {sorted(EXIT_REGISTRY)}")
        out.append(EXIT_REGISTRY[r["type"]](**params))
    return out
