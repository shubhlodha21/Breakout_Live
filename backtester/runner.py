"""Config loading and parallel orchestration across tickers."""
from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .data import DataStore
from .engine import run_strategy
from .entries import ENTRY_REGISTRY, build_signal
from .exits import EXIT_REGISTRY, build_exits
from .report import write_report

PROJECT_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "run_name": "backtest",
    "data_dir": r"D:\DataBento\data",
    "csv_dir": r"D:\DataBento\data\USA",
    "cache_dir": str(PROJECT_DIR / "cache"),
    "output_dir": str(PROJECT_DIR / "output"),
    "session": "rth",
    "daily_source": "csv",
    "tickers": "ALL",
    "exclude_tickers": [],
    "timeframes": ["1D"],
    "start": None,
    "end": None,
    "entry_on": "next_open",
    "exit_on": "next_open",
    "capital_per_trade": 10_000,
    "commission_per_share": 0.005,
    "min_commission": 1.0,
    "slippage_pct": 0.05,
    "min_bars": 260,
    "workers": min(8, max(1, (os.cpu_count() or 2) - 2)),  # ~0.5 GB RAM each while building the cache
    "entries": [],
    "use_exits": True,   # False -> ignore `exits`, one trade per ticker/timeframe/entry set, held to end of data
    "exits": [],
}

TRADE_COLS = ["ticker", "timeframe", "entry_set", "exit_set", "signal_time", "entry_time", "entry_price",
              "initial_stop", "exit_time", "exit_price", "exit_reason", "qty", "bars_held", "days_held",
              "pnl", "return_pct", "r_multiple", "mae_pct", "mfe_pct"]


def load_config(path: str | None, overrides: dict | None = None, base: dict | None = None) -> dict:
    """Defaults <- YAML file (path) or already-parsed dict (base, e.g. from Runner.xlsx) <- overrides."""
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as fh:
            cfg.update(yaml.safe_load(fh) or {})
    if base:
        cfg.update({k: v for k, v in base.items() if v is not None})
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    validate(cfg)
    return cfg


NO_EXITS_SET = {"name": "No Exit (hold)", "rules": []}  # one trade: first signal, held to end of data


def validate(cfg: dict):
    if str(cfg.get("use_exits", True)).strip().lower() in ("no", "false", "0"):
        cfg["exits"] = [NO_EXITS_SET]  # exit rules switched off: hold while the entry is still valid
    if not cfg["entries"] or not cfg["exits"]:
        raise ValueError("Config needs at least one entry set and one exit set")
    for e in cfg["entries"]:
        for c in e["conditions"]:
            if c["type"] not in ENTRY_REGISTRY:
                raise ValueError(f"Unknown entry type '{c['type']}'. Available: {sorted(ENTRY_REGISTRY)}")
    for x in cfg["exits"]:
        build_exits(x["rules"])  # raises on bad types/params
    names = [e["name"] for e in cfg["entries"]], [x["name"] for x in cfg["exits"]]
    for lst in names:
        if len(set(lst)) != len(lst):
            raise ValueError(f"Duplicate entry/exit set names: {lst}")
    if isinstance(cfg["timeframes"], str):
        cfg["timeframes"] = [cfg["timeframes"]]


def _store(cfg, rebuild=False) -> DataStore:
    return DataStore(cfg["data_dir"], cfg["csv_dir"], cfg["cache_dir"], cfg["session"],
                     cfg["daily_source"], rebuild)


def run_ticker(ticker: str, cfg: dict, rebuild_cache: bool = False) -> dict:
    store = _store(cfg, rebuild_cache)
    trades, info, errors = [], [], []
    start = pd.Timestamp(cfg["start"]) if cfg["start"] else None
    end = pd.Timestamp(cfg["end"]) + pd.Timedelta(days=1) if cfg["end"] else None
    for tf in cfg["timeframes"]:
        try:
            df, source = store.get_bars(ticker, tf)
            if end is not None:
                df = df[df.index < end]       # never let data after `end` into the test
            mask = np.ones(len(df), bool) if start is None else (df.index >= start)
            if mask.sum() < 2 or len(df) < cfg["min_bars"]:
                errors.append({"ticker": ticker, "timeframe": tf, "error": f"not enough bars ({len(df)})"})
                continue
            in_rng = df[mask]
            info.append({"ticker": ticker, "timeframe": tf, "source": source,
                         "first_bar": in_rng.index[0], "last_bar": in_rng.index[-1], "bars": len(in_rng),
                         "buy_hold_pct": round((in_rng["close"].iloc[-1] / in_rng["open"].iloc[0] - 1) * 100, 2)})
            cache = {}
            for es in cfg["entries"]:
                sig = build_signal(df, es["conditions"], cache)
                for xs in cfg["exits"]:
                    rules = build_exits(xs["rules"])
                    for r in rules:
                        if hasattr(r, "bind_entry"):  # exits that depend on this entry set
                            r.bind_entry(df, es["conditions"])
                    res = run_strategy(
                        df, sig, rules, trade_mask=mask,
                        entry_on=cfg["entry_on"], exit_on=cfg["exit_on"],
                        capital_per_trade=cfg["capital_per_trade"],
                        commission_per_share=cfg["commission_per_share"],
                        min_commission=cfg["min_commission"], slippage_pct=cfg["slippage_pct"])
                    for t in res:
                        t.update(ticker=ticker, timeframe=tf, entry_set=es["name"], exit_set=xs["name"])
                    trades.extend(res)
        except Exception as ex:  # one bad ticker must not kill a 500-ticker run
            errors.append({"ticker": ticker, "timeframe": tf, "error": f"{type(ex).__name__}: {ex}",
                           "trace": traceback.format_exc(limit=3)})
    return {"trades": trades, "info": info, "errors": errors}


def run(cfg: dict, rebuild_cache: bool = False) -> Path:
    t0 = time.time()
    store = _store(cfg)
    if isinstance(cfg["tickers"], str) and cfg["tickers"].upper() == "ALL":
        tickers = store.all_tickers(cfg["timeframes"])
    else:
        tickers = [t.strip().upper() for t in (cfg["tickers"].split(",") if isinstance(cfg["tickers"], str)
                                               else cfg["tickers"])]
        known = set(store.intraday_tickers()) | set(store.csv_tickers())
        unknown = [t for t in tickers if t not in known]
        if unknown:
            print(f"WARNING: no data for {unknown} - skipped")
            tickers = [t for t in tickers if t in known]
        if not tickers:
            raise SystemExit("None of the requested tickers have data")
    excl = {t.upper() for t in cfg.get("exclude_tickers") or []}
    tickers = [t for t in tickers if t not in excl]
    combos = [(tf, e["name"], x["name"]) for tf in cfg["timeframes"] for e in cfg["entries"] for x in cfg["exits"]]
    print(f"Tickers: {len(tickers) if len(tickers) > 10 else ', '.join(tickers)} | timeframes: {cfg['timeframes']}")
    print(f"Entry sets: {[e['name'] for e in cfg['entries']]}")
    print(f"Exit sets:  {[x['name'] for x in cfg['exits']]}"
          + ("" if cfg.get("use_exits", True) not in (False, "No", "no") else "  (exit conditions OFF)"))
    print(f"Combos/ticker: {len(combos)} | workers: {cfg['workers']}")

    trades, info, errors = [], [], []
    if cfg["workers"] <= 1 or len(tickers) == 1:
        for i, t in enumerate(tickers, 1):
            r = run_ticker(t, cfg, rebuild_cache)
            trades += r["trades"]; info += r["info"]; errors += r["errors"]
            print(f"  [{i}/{len(tickers)}] {t}: {len(r['trades'])} trades")
    else:
        with ProcessPoolExecutor(max_workers=cfg["workers"]) as ex:
            futs = {ex.submit(run_ticker, t, cfg, rebuild_cache): t for t in tickers}
            for i, f in enumerate(as_completed(futs), 1):
                t = futs[f]
                try:
                    r = f.result()
                except Exception as e:
                    r = {"trades": [], "info": [], "errors": [{"ticker": t, "error": repr(e)}]}
                trades += r["trades"]; info += r["info"]; errors += r["errors"]
                if i % 10 == 0 or i == len(tickers):
                    print(f"  [{i}/{len(tickers)}] done  ({time.time() - t0:,.0f}s)")

    trades_df = pd.DataFrame(trades, columns=TRADE_COLS).sort_values(["ticker", "timeframe", "entry_set",
                                                                      "exit_set", "entry_time"])
    for col in ("signal_time", "entry_time", "exit_time"):
        trades_df[col] = pd.to_datetime(trades_df[col])
    info_df = pd.DataFrame(info, columns=["ticker", "timeframe", "source", "first_bar", "last_bar", "bars",
                                          "buy_hold_pct"]).sort_values(["ticker", "timeframe"])
    out_dir = Path(cfg["output_dir"]) / f"{cfg['run_name']}_{datetime.now():%Y%m%d_%H%M%S}"
    xlsx = write_report(trades_df, info_df, errors, cfg, combos, out_dir)
    print(f"\n{len(trades_df):,} trades | {len(errors)} errors | {time.time() - t0:,.0f}s")
    print(f"Report: {xlsx}")
    return out_dir
