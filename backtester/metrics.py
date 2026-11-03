"""Performance statistics on a set of trades."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _max_streak(mask: np.ndarray) -> int:
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def trade_stats(tr: pd.DataFrame, years: float | None = None, capital_per_trade: float = 10_000) -> dict:
    """Stats for a pool of trades.

    Return-based figures use return_pct (independent of sizing). The $ equity curve assumes a fixed
    `capital_per_trade` per trade with P&L booked on the exit date (all tickers pooled).
    """
    n = len(tr)
    if n == 0:
        return {"trades": 0}
    r = tr["return_pct"].to_numpy()
    wins, losses = r[r > 0], r[r <= 0]
    gross_win, gross_loss = tr.loc[tr.pnl > 0, "pnl"].sum(), -tr.loc[tr.pnl <= 0, "pnl"].sum()

    tr_sorted = tr.sort_values("exit_time")
    cum = tr_sorted["pnl"].cumsum().to_numpy()
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    dd = cum - peak

    out = {
        "trades": n,
        "win_rate_%": round(100 * len(wins) / n, 2),
        "avg_return_%": round(r.mean(), 3),
        "median_return_%": round(float(np.median(r)), 3),
        "avg_win_%": round(wins.mean(), 3) if len(wins) else 0.0,
        "avg_loss_%": round(losses.mean(), 3) if len(losses) else 0.0,
        "payoff_ratio": round(wins.mean() / -losses.mean(), 3) if len(wins) and len(losses) and losses.mean() < 0 else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy_%": round(r.mean(), 3),
        "best_trade_%": round(r.max(), 2),
        "worst_trade_%": round(r.min(), 2),
        "total_pnl": round(tr["pnl"].sum(), 2),
        "max_drawdown_$": round(dd.min(), 2),
        "max_consec_losses": _max_streak(tr_sorted["return_pct"].to_numpy() <= 0),
        "avg_bars_held": round(tr["bars_held"].mean(), 1),
        "avg_days_held": round(tr["days_held"].mean(), 1),
        "avg_mae_%": round(tr["mae_pct"].mean(), 3),
        "avg_mfe_%": round(tr["mfe_pct"].mean(), 3),
    }
    if tr["r_multiple"].notna().any():
        out["avg_R"] = round(tr["r_multiple"].mean(), 3)
    if years and years > 0:
        out["trades_per_year"] = round(n / years, 2)
    return out


def compounded_stats(tr: pd.DataFrame, years: float) -> dict:
    """For ONE ticker (trades are sequential, never overlap): compound each trade's return as if the
    whole account were in each trade -> total return, CAGR, max drawdown (on closed trades)."""
    if tr.empty:
        return {}
    eq = np.cumprod(1 + tr.sort_values("exit_time")["return_pct"].to_numpy() / 100)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    total = eq[-1] - 1
    cagr = (eq[-1] ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else None
    return {
        "compounded_return_%": round(100 * total, 2),
        "cagr_%": round(100 * cagr, 2) if cagr is not None else None,
        "max_dd_compounded_%": round(100 * (eq / peak - 1).min(), 2),
    }
