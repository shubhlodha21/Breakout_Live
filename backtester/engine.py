"""Bar-by-bar long-only simulation for one ticker / timeframe / entry set / exit set.

Timing (no look-ahead):
    * Entry signal is evaluated on the CLOSE of bar s.
    * entry_on = "next_open" -> filled at open of bar s+1 (default);  "close" -> filled at close of bar s.
    * Stops/targets are checked intrabar on every following bar, using levels known before that bar.
    * Signal-based exits (MA exit, etc.) fire on a bar close and fill at the next open (or close).
    * One position at a time per ticker; a new entry needs a new signal after the exit.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .exits import ExitRule, TradeState

NAN = float("nan")


def run_strategy(df: pd.DataFrame, signal: pd.Series, exits: list[ExitRule], *,
                 trade_mask: np.ndarray | None = None, entry_on: str = "next_open",
                 exit_on: str = "next_open", capital_per_trade: float = 10_000,
                 commission_per_share: float = 0.0, min_commission: float = 0.0,
                 slippage_pct: float = 0.0) -> list[dict]:
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    times = df.index
    open_t, close_t = _fill_times(times)
    n = len(df)
    sig = signal.to_numpy(dtype=bool)
    if trade_mask is not None:
        sig = sig & trade_mask
    sig_idx = np.flatnonzero(sig)
    slip = slippage_pct / 100

    for r in exits:
        r.prepare(df)

    trades = []
    next_allowed = 0  # first bar index on which a new signal may occur
    while True:
        k = np.searchsorted(sig_idx, next_allowed)
        if k >= len(sig_idx):
            break
        s = int(sig_idx[k])
        if entry_on == "close":
            e, first, raw_entry = s, s + 1, c[s]
            entry_t = close_t[s]
        else:
            e, first = s + 1, s + 1
            if e >= n:
                break
            raw_entry = o[e]
            entry_t = open_t[e]
        if first >= n:
            break
        entry = raw_entry * (1 + slip)

        t = TradeState(signal_idx=s, entry_idx=e, entry_price=entry, hh=raw_entry, hc=raw_entry)
        for r in exits:
            r.reset(t)
        init_stop = _max_level([r.stop(first, t) for r in exits])
        if math.isfinite(init_stop) and init_stop < entry:
            t.risk = entry - init_stop

        lo, hi = raw_entry, raw_entry   # for MAE / MFE
        exit_idx, exit_px, reason, exit_t = None, None, None, None
        for j in range(first, n):
            # ---- protective stops & targets active during bar j
            stop, stop_rule = NAN, None
            for r in exits:
                v = r.stop(j, t)
                if v == v and not (v <= stop):  # v is finite and > current best
                    stop, stop_rule = v, r
            tgt, tgt_rule = NAN, None
            for r in exits:
                v = r.target(j, t)
                if v == v and not (v >= tgt):
                    tgt, tgt_rule = v, r

            # rules that set both a stop and a target (risk_reward) label them separately
            if stop == stop and o[j] <= stop:
                exit_idx, exit_px, reason, exit_t = j, o[j], _stop_reason(stop_rule) + " (gap)", open_t[j]
            elif tgt == tgt and o[j] >= tgt:
                exit_idx, exit_px, reason, exit_t = j, o[j], _target_reason(tgt_rule) + " (gap)", open_t[j]
            elif stop == stop and l[j] <= stop:  # hit during the bar: exact time unknown -> bar close
                exit_idx, exit_px, reason, exit_t = j, stop, _stop_reason(stop_rule), close_t[j]
            elif tgt == tgt and h[j] >= tgt:
                exit_idx, exit_px, reason, exit_t = j, tgt, _target_reason(tgt_rule), close_t[j]
            if exit_idx is not None:  # intrabar order unknown: count only up to the fill price
                lo, hi = min(lo, exit_px), max(hi, exit_px)
                break

            lo, hi = min(lo, l[j]), max(hi, h[j])
            # ---- bar j has closed: update state, check close-based exits
            if h[j] > t.hh:
                t.hh = h[j]
            if c[j] > t.hc:
                t.hc = c[j]
            for r in exits:
                res = r.after_bar(j, t)
                if res:
                    reason, when = res[0], (res[1] or exit_on)
                    if when == "close" or j + 1 >= n:
                        exit_idx, exit_px, exit_t = j, c[j], close_t[j]
                    else:
                        exit_idx, exit_px, exit_t = j + 1, o[j + 1], open_t[j + 1]
                    break
            if exit_idx is not None:
                break

        if exit_idx is None:  # still open at end of data
            exit_idx, exit_px, reason, exit_t = n - 1, c[n - 1], "End of Data", close_t[n - 1]

        trades.append(_record(times, s, e, exit_idx, entry, exit_px * (1 - slip), reason, t,
                              init_stop, lo, hi, capital_per_trade, commission_per_share, min_commission))
        trades[-1].update(signal_time=close_t[s], entry_time=entry_t, exit_time=exit_t)
        # A signal on the close of the exit bar may re-enter (filled on the following bar).
        next_allowed = exit_idx + (1 if entry_on == "close" else 0)
    return trades


SESSION_OPEN, SESSION_CLOSE = pd.Timedelta("09:30:00"), pd.Timedelta("16:00:00")


def _fill_times(times: pd.DatetimeIndex):
    """Clock times at which a bar's open / close trade. Intraday bars keep their bar-start time.
    Daily/weekly bars are date-only (00:00), so stamp them with the session open (09:30) and close
    (16:00); a weekly bar closes on the Friday of its week."""
    if len(times) == 0 or not (times == times.normalize()).all():
        return times, times
    open_t = times + SESSION_OPEN
    if len(times) > 1 and (times[1:] - times[:-1]).median() > pd.Timedelta(days=3):  # weekly bars
        close_day = times.to_period("W-FRI").end_time.normalize()
    else:
        close_day = times
    return open_t, close_day + SESSION_CLOSE


def _stop_reason(rule):
    return getattr(rule, "stop_reason", rule.reason)


def _target_reason(rule):
    return getattr(rule, "target_reason", rule.reason)


def _max_level(vals):
    vals = [v for v in vals if v == v]
    return max(vals) if vals else NAN


def _record(times, s, e, x, entry, exit_px, reason, t, init_stop, lo, hi,
            capital, comm_ps, min_comm) -> dict:
    qty = math.floor(capital / entry) if entry > 0 else 0
    comm = 2 * max(qty * comm_ps, min_comm) if qty else 0.0
    gross = qty * (exit_px - entry)
    pnl = gross - comm
    ret = (exit_px / entry - 1) * 100 - (comm / (qty * entry) * 100 if qty else 0)
    return {
        "signal_time": times[s],
        "entry_time": times[e],
        "entry_price": round(entry, 4),
        "initial_stop": round(init_stop, 4) if init_stop == init_stop else None,
        "exit_time": times[x],
        "exit_price": round(exit_px, 4),
        "exit_reason": reason,
        "qty": qty,
        "bars_held": x - e + 1,
        "days_held": (times[x] - times[e]).days,
        "pnl": round(pnl, 2),
        "return_pct": round(ret, 4),
        "r_multiple": round((exit_px - entry) / t.risk, 3) if t.risk == t.risk else None,
        "mae_pct": round((lo / entry - 1) * 100, 3),
        "mfe_pct": round((hi / entry - 1) * 100, 3),
    }
