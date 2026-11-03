"""Report output: trades.csv, report.xlsx (multi-sheet), equity-curve PNGs."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import compounded_stats, trade_stats

COMBO = ["timeframe", "entry_set", "exit_set"]
EXCEL_MAX_ROWS = 1_000_000
HIDDEN_TRADE_COLS = ["r_multiple", "mae_pct", "mfe_pct"]  # used for summary stats, not shown per trade
HIDDEN_SUMMARY_COLS = ["payoff_ratio", "expectancy_%", "avg_mae_%", "avg_mfe_%", "avg_R",
                       "%_tickers_profitable", "median_ticker_cagr_%", "median_exposure_%"]

# chart styling (reference palette, light mode)
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, RED = "#2a78d6", "#e34948"


def build_summaries(trades: pd.DataFrame, info: pd.DataFrame, combos: list[tuple], capital: float):
    """Return dict of DataFrames: combo summary, ticker summary, yearly, exit reasons."""
    info = info.copy()
    info["years"] = (info["last_bar"] - info["first_bar"]).dt.days / 365.25

    # ---------------- per ticker x combo
    rows = []
    groups = dict(tuple(trades.groupby(["ticker"] + COMBO))) if len(trades) else {}
    for _, inf in info.iterrows():
        for tf, es, xs in combos:
            if tf != inf["timeframe"]:
                continue
            tr = groups.get((inf["ticker"], tf, es, xs), trades.iloc[0:0])
            row = {"ticker": inf["ticker"], "timeframe": tf, "entry_set": es, "exit_set": xs,
                   "data_source": inf["source"], "from": inf["first_bar"].date(), "to": inf["last_bar"].date()}
            row.update(trade_stats(tr, inf["years"], capital))
            row.update(compounded_stats(tr, inf["years"]))
            row["exposure_%"] = round(100 * tr["bars_held"].sum() / inf["bars"], 2) if inf["bars"] else None
            row["buy_hold_%"] = inf["buy_hold_pct"]
            rows.append(row)
    ticker_sum = pd.DataFrame(rows)

    # ---------------- per combo, all tickers pooled
    rows = []
    for tf, es, xs in combos:
        tr = trades[(trades.timeframe == tf) & (trades.entry_set == es) & (trades.exit_set == xs)] if len(trades) else trades
        inf = info[info.timeframe == tf]
        years = (inf["last_bar"].max() - inf["first_bar"].min()).days / 365.25 if len(inf) else None
        row = {"timeframe": tf, "entry_set": es, "exit_set": xs, "tickers_tested": len(inf)}
        row.update(trade_stats(tr, years, capital))
        ts = ticker_sum[(ticker_sum.timeframe == tf) & (ticker_sum.entry_set == es) & (ticker_sum.exit_set == xs)]
        traded = ts[ts["trades"] > 0]
        row["tickers_traded"] = len(traded)
        if len(traded):
            row["%_tickers_profitable"] = round(100 * (traded["total_pnl"] > 0).mean(), 1)
            if "cagr_%" in traded:
                row["median_ticker_cagr_%"] = round(traded["cagr_%"].median(), 2)
                row["median_ticker_max_dd_%"] = round(traded["max_dd_compounded_%"].median(), 2)
            row["median_exposure_%"] = round(traded["exposure_%"].median(), 2)
            row["median_buy_hold_%"] = round(traded["buy_hold_%"].median(), 2)
        rows.append(row)
    combo_sum = pd.DataFrame(rows)
    if "profit_factor" in combo_sum:
        combo_sum = combo_sum.sort_values(["timeframe", "profit_factor"], ascending=[True, False], na_position="last")

    # ---------------- yearly & exit reasons
    if len(trades):
        t = trades.assign(year=trades["exit_time"].dt.year)
        yearly = (t.groupby(COMBO + ["year"])
                  .agg(trades=("pnl", "size"), win_rate_pct=("return_pct", lambda r: round(100 * (r > 0).mean(), 2)),
                       avg_return_pct=("return_pct", "mean"), total_pnl=("pnl", "sum"),
                       tickers=("ticker", "nunique"))
                  .round(3).reset_index())
        reasons = (trades.groupby(COMBO + ["exit_reason"])
                   .agg(count=("pnl", "size"), avg_return_pct=("return_pct", "mean"),
                        total_pnl=("pnl", "sum"), avg_bars_held=("bars_held", "mean"))
                   .round(3).reset_index())
        reasons["share_%"] = (100 * reasons["count"] /
                              reasons.groupby(COMBO)["count"].transform("sum")).round(2)
    else:
        yearly = reasons = pd.DataFrame()
    return {"Combo_Summary": combo_sum, "Ticker_Summary": ticker_sum, "Yearly": yearly, "Exit_Reasons": reasons}


def write_report(trades: pd.DataFrame, info: pd.DataFrame, errors: list[dict], cfg: dict,
                 combos: list[tuple], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    sums = build_summaries(trades, info, combos, cfg["capital_per_trade"])
    sums = {k: v.drop(columns=HIDDEN_SUMMARY_COLS, errors="ignore") for k, v in sums.items()}
    trades =trades.drop(columns=HIDDEN_TRADE_COLS, errors="ignore")
    trades.to_csv(out_dir / "trades.csv", index=False)
    sums["Ticker_Summary"].to_csv(out_dir / "ticker_summary.csv", index=False)
    sums["Combo_Summary"].to_csv(out_dir / "combo_summary.csv", index=False)

    xlsx = out_dir / "report.xlsx"
    with pd.ExcelWriter(xlsx, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm",
                        date_format="yyyy-mm-dd") as xw:
        _sheet(xw, "Combo_Summary", sums["Combo_Summary"])
        _sheet(xw, "Ticker_Summary", sums["Ticker_Summary"])
        if len(trades) > EXCEL_MAX_ROWS:
            _sheet(xw, "Trades", trades.head(EXCEL_MAX_ROWS))
            print(f"  NOTE: {len(trades):,} trades - Excel 'Trades' sheet truncated; full list in trades.csv")
        else:
            _sheet(xw, "Trades", trades)
        _sheet(xw, "Yearly", sums["Yearly"])
        _sheet(xw, "Exit_Reasons", sums["Exit_Reasons"])
        _sheet(xw, "Data_Coverage", info)
        if errors:
            _sheet(xw, "Errors", pd.DataFrame(errors))

    if len(trades):
        _charts(trades, sums["Combo_Summary"], out_dir / "charts")
    return xlsx


def _sheet(xw, name, df: pd.DataFrame, filt=True):
    df.to_excel(xw, sheet_name=name, index=False)
    ws = xw.sheets[name]
    if df.empty:
        return
    ws.freeze_panes(1, 0)
    if filt:
        ws.autofilter(0, 0, len(df), len(df.columns) - 1)
    sample = df.head(500)
    for i, col in enumerate(df.columns):
        lens = sample[col].map(lambda v: 0 if pd.isna(v) else len(str(v)))
        width = max(len(str(col)), int(lens.quantile(0.95)) if len(lens) else 0)
        ws.set_column(i, i, min(max(width + 2, 9), 60))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:80]


def _charts(trades: pd.DataFrame, combo_sum: pd.DataFrame, chart_dir: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mtick

    chart_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "text.color": INK})

    # ---- equity + drawdown per combo (fixed $ per trade, all tickers pooled, booked at exit)
    for (tf, es, xs), tr in trades.groupby(COMBO):
        s = tr.sort_values("exit_time").set_index("exit_time")["pnl"].cumsum()
        dd = s - np.maximum.accumulate(np.maximum(s.to_numpy(), 0))
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                     gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08}, facecolor=SURFACE)
        for ax in (a1, a2):
            ax.set_facecolor(SURFACE)
            ax.grid(axis="y", color=GRID, linewidth=0.6)
            ax.spines[["top", "right"]].set_visible(False)
            ax.yaxis.set_major_formatter(mtick.StrMethodFormatter("${x:,.0f}"))
        a1.plot(s.index, s.values, color=BLUE, linewidth=2)
        a1.axhline(0, color=INK2, linewidth=0.8)
        a1.set_ylabel("Cumulative P&L")
        a1.annotate(f"${s.iloc[-1]:,.0f}", (s.index[-1], s.iloc[-1]), xytext=(4, 0),
                    textcoords="offset points", va="center", color=INK, fontsize=9)
        a2.fill_between(dd.index, dd.values, 0, color=RED, alpha=0.35, linewidth=0)
        a2.plot(dd.index, dd.values, color=RED, linewidth=1)
        a2.set_ylabel("Drawdown")
        fig.suptitle(f"{es}  |  {xs}  |  {tf}", x=0.06, ha="left", fontsize=12, color=INK)
        a1.set_title(f"{len(tr):,} trades across {tr.ticker.nunique()} tickers - fixed capital per trade, "
                     f"P&L booked at exit", loc="left", fontsize=9, color=INK2)
        fig.savefig(chart_dir / f"equity_{_slug(tf)}__{_slug(es)}__{_slug(xs)}.png", dpi=110,
                    bbox_inches="tight", facecolor=SURFACE)
        plt.close(fig)

    # ---- combo comparison: expectancy per trade
    cs = combo_sum.dropna(subset=["avg_return_%"]) if "avg_return_%" in combo_sum else combo_sum.iloc[0:0]
    if len(cs) > 1:
        cs = cs.sort_values("avg_return_%")
        labels = cs["entry_set"] + " | " + cs["exit_set"] + " | " + cs["timeframe"]
        fig, ax = plt.subplots(figsize=(11, 0.45 * len(cs) + 1.2), facecolor=SURFACE)
        ax.set_facecolor(SURFACE)
        ax.barh(labels, cs["avg_return_%"], color=BLUE, height=0.6)
        ax.axvline(0, color=INK2, linewidth=0.8)
        ax.grid(axis="x", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.xaxis.set_major_formatter(mtick.PercentFormatter(decimals=1))
        for y, (v, pf) in enumerate(zip(cs["avg_return_%"], cs.get("profit_factor", [None] * len(cs)))):
            ax.annotate(f"{v:.2f}%  (PF {pf if pf is not None else '-'})", (v, y), xytext=(4 if v >= 0 else -4, 0),
                        textcoords="offset points", va="center", ha="left" if v >= 0 else "right",
                        fontsize=8, color=INK2)
        ax.set_title("Average return per trade by strategy combination (all tickers)", loc="left",
                     fontsize=12, color=INK)
        fig.savefig(chart_dir / "combo_comparison.png", dpi=110, bbox_inches="tight", facecolor=SURFACE)
        plt.close(fig)
