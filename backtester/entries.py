"""Entry conditions.

Each condition returns a boolean Series: True at bar t means "condition satisfied at the close of bar t".
An entry set is the AND of its conditions; the trade is filled at the next bar's open (or the signal
bar's close if entry_on = "close").

Add a new condition:
    @register("my_condition")
    def my_condition(df, param_a=1, param_b=2):
        return <bool Series>
then use  {type: my_condition, param_a: 5}  in the YAML config.
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind

ENTRY_REGISTRY: dict = {}


def register(name: str):
    def deco(fn):
        ENTRY_REGISTRY[name] = fn
        return fn
    return deco


def _fresh(cond: pd.Series) -> pd.Series:
    """True only on the first bar the condition turns True."""
    return cond & ~cond.shift(1, fill_value=False)


def _ma(kind: str):
    return {"sma": ind.sma, "ema": ind.ema}[kind.lower()]


# ---------------------------------------------------------------------------- trend filters
@register("sma_above")
def sma_above(df, period=200, kind="sma", timeframe=None, slope_bars=0, fresh=False):
    """Close above moving average (e.g. the 200 SMA). Optional: MA rising over `slope_bars` bars.
    timeframe='1D' -> use the daily MA on an intraday chart."""
    ma = ind.on_timeframe(df, timeframe, lambda b: _ma(kind)(b["close"], period))
    cond = df["close"] > ma
    if slope_bars:
        cond &= ma > ma.shift(slope_bars)
    return _fresh(cond) if fresh else cond


@register("sma_below")
def sma_below(df, period=200, kind="sma", timeframe=None):
    ma = ind.on_timeframe(df, timeframe, lambda b: _ma(kind)(b["close"], period))
    return df["close"] < ma


@register("ma_cross")
def ma_cross(df, fast=50, slow=200, kind="sma", state=False):
    """Fast MA crosses above slow MA (golden cross). state=True -> fast simply above slow."""
    f, s = _ma(kind)(df["close"], fast), _ma(kind)(df["close"], slow)
    cond = f > s
    return cond if state else _fresh(cond)


@register("ma_stack")
def ma_stack(df, periods=(50, 150, 200), kind="sma"):
    """Close > MA1 > MA2 > MA3 ... (Minervini-style trend template)."""
    mas = [_ma(kind)(df["close"], p) for p in periods]
    cond = df["close"] > mas[0]
    for a, b in zip(mas, mas[1:]):
        cond &= a > b
    return cond


# ---------------------------------------------------------------------------- breakouts
@register("high_52w")
def high_52w(df, lookback_days=365, price="close", within_pct=0.0, fresh=False):
    """52-week high breakout.
    within_pct=0 -> `price` (close or high) above the highest high of the prior 52 weeks.
    within_pct=5 -> close within 5% of the prior 52-week high ("near highs").
    fresh=True  -> only the first bar of a new breakout.
    Works on any timeframe (uses a calendar window, not a bar count)."""
    prior = ind.prior_high_calendar(df, lookback_days)
    if within_pct:
        cond = df["close"] >= prior * (1 - within_pct / 100)
    else:
        cond = df[price] > prior
    return _fresh(cond) if fresh else cond


@register("donchian_breakout")
def donchian_breakout(df, period=20, price="close", fresh=False):
    """Price above the highest high of the previous `period` bars."""
    cond = df[price] > ind.prior_high_bars(df, period)
    return _fresh(cond) if fresh else cond


@register("all_time_high")
def all_time_high(df, price="close", min_bars=252):
    prior = df["high"].shift(1).expanding(min_periods=min_bars).max()
    return df[price] > prior


# ---------------------------------------------------------------------------- filters
@register("volume_surge")
def volume_surge(df, mult=1.5, period=50):
    """Volume > mult x average volume of the previous `period` bars."""
    avg = df["volume"].shift(1).rolling(period, min_periods=period).mean()
    return df["volume"] > mult * avg


@register("rsi_above")
def rsi_above(df, level=50, period=14):
    return ind.rsi(df["close"], period) > level


@register("rsi_below")
def rsi_below(df, level=30, period=14):
    return ind.rsi(df["close"], period) < level


@register("min_price")
def min_price(df, price=5.0):
    return df["close"] >= price


@register("min_avg_dollar_volume")
def min_avg_dollar_volume(df, amount=10_000_000, period=20):
    return (df["close"] * df["volume"]).rolling(period, min_periods=period).mean() >= amount


@register("up_bar")
def up_bar(df):
    return df["close"] > df["open"]


@register("time_window")
def time_window(df, start="09:45", end="15:30"):
    """Intraday only: signal bar must START between start and end (ET)."""
    t = df.index.strftime("%H:%M")
    return pd.Series((t >= start) & (t <= end), index=df.index)


# ---------------------------------------------------------------------------- "still valid" (for exits)
# How each condition is judged WHILE a trade is open (used by the entry_invalid exit):
#   ("state", bool Series)  -> trade stays valid while True       (trend / filter conditions)
#   ("level", float Series) -> trade stays valid while close >= the level as of the SIGNAL bar
#                              (breakouts: falling back below the broken level = failed breakout)
#   None                    -> one-bar trigger, ignored while holding (volume surge, up bar, ...)
def _hold_52w(df, lookback_days=365, price="close", within_pct=0.0, fresh=False):
    return "level", ind.prior_high_calendar(df, lookback_days) * (1 - within_pct / 100)


def _hold_donchian(df, period=20, price="close", fresh=False):
    return "level", ind.prior_high_bars(df, period)


def _hold_ath(df, price="close", min_bars=252):
    return "level", df["high"].shift(1).expanding(min_periods=min_bars).max()


def _hold_state(name):
    def fn(df, **params):
        params.pop("fresh", None)  # judge the ongoing state, not the first bar of it
        if name == "ma_cross":
            params["state"] = True
        return "state", ENTRY_REGISTRY[name](df, **params).fillna(False).astype(bool)
    return fn


HOLD_REGISTRY = {
    "high_52w": _hold_52w,
    "donchian_breakout": _hold_donchian,
    "all_time_high": _hold_ath,
    **{n: _hold_state(n) for n in ("sma_above", "sma_below", "ma_cross", "ma_stack", "rsi_above",
                                   "rsi_below", "min_price", "min_avg_dollar_volume")},
    # one-bar triggers -> not checked while holding
    "volume_surge": None, "up_bar": None, "time_window": None,
}


def build_hold(df: pd.DataFrame, conditions: list[dict]):
    """Return (state: bool ndarray, levels: list of float ndarrays) for an entry set."""
    state = pd.Series(True, index=df.index)
    levels = []
    for c in conditions:
        fn = HOLD_REGISTRY.get(c["type"])
        if fn is None:
            continue
        params = {k: v for k, v in c.items() if k not in ("type", "negate")}
        kind, s = fn(df, **params)
        if kind == "state":
            state &= ~s if c.get("negate") else s
        elif not c.get("negate"):
            levels.append(s.to_numpy(dtype=float))
    return state.to_numpy(), levels


# ---------------------------------------------------------------------------- combination
def build_signal(df: pd.DataFrame, conditions: list[dict], cache: dict | None = None) -> pd.Series:
    """AND of all conditions. `cache` reuses identical conditions across entry sets."""
    sig = pd.Series(True, index=df.index)
    for c in conditions:
        params = {k: v for k, v in c.items() if k not in ("type", "negate")}
        key = (c["type"], repr(sorted(params.items())))
        if cache is not None and key in cache:
            s = cache[key]
        else:
            if c["type"] not in ENTRY_REGISTRY:
                raise ValueError(f"Unknown entry type '{c['type']}'. Available: {sorted(ENTRY_REGISTRY)}")
            s = ENTRY_REGISTRY[c["type"]](df, **params).fillna(False).astype(bool)
            if cache is not None:
                cache[key] = s
        sig &= ~s if c.get("negate") else s
    return sig
