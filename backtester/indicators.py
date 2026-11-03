"""Indicator functions. All take/return pandas objects aligned to the bar index.

Values at bar t use data up to and including bar t's close (signals are acted on at the NEXT bar).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import AGG, normalize_tf


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()  # Wilder smoothing


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def prior_high_calendar(df: pd.DataFrame, days: int, col: str = "high") -> pd.Series:
    """Highest `col` over the previous `days` calendar days, EXCLUDING the current bar.
    NaN until a full window of history exists (avoids fake '52-week highs' in the first year)."""
    out = df[col].rolling(f"{days}D", closed="left").max()
    out[df.index < df.index[0] + pd.Timedelta(days=days)] = np.nan
    return out


def prior_high_bars(df: pd.DataFrame, n: int, col: str = "high") -> pd.Series:
    return df[col].shift(1).rolling(n, min_periods=n).max()


def prior_low_bars(df: pd.DataFrame, n: int, col: str = "low") -> pd.Series:
    return df[col].shift(1).rolling(n, min_periods=n).min()


def on_timeframe(df: pd.DataFrame, timeframe: str | None, fn) -> pd.Series:
    """Compute `fn(bars)` on a higher timeframe (e.g. daily 200 SMA while trading 15min) and map it
    back onto df's index using only COMPLETED higher-timeframe bars (no look-ahead).

    If timeframe is None or not higher than df's own, fn is applied to df directly.
    """
    if timeframe is None:
        return fn(df)
    rule = normalize_tf(timeframe)
    if rule not in ("1D", "1W"):
        raise ValueError("Indicator 'timeframe' supports 1D or 1W")
    key = df.index.normalize() if rule == "1D" else df.index.to_period("W-FRI").start_time
    htf = df.groupby(key).agg(AGG)
    if len(htf) == len(df):  # df is already at this timeframe
        return fn(df)
    vals = fn(htf).shift(1)  # previous completed day/week
    return pd.Series(vals.reindex(key).values, index=df.index)
