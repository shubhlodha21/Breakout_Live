"""Run a backtest from a YAML config.

Examples
    python run_backtest.py -c configs/breakout.yaml
    python run_backtest.py -c configs/breakout.yaml --tickers AAPL,NVDA,MSFT --timeframes 1D,1h
    python run_backtest.py -c configs/breakout.yaml --start 2020-01-01 --end 2025-12-31 --workers 8
    python run_backtest.py --list        # show available entry / exit types
"""
import argparse
import inspect
import os

from backtester.entries import ENTRY_REGISTRY
from backtester.exits import EXIT_REGISTRY
from backtester.runner import load_config, run


def list_types():
    print("ENTRY CONDITIONS (all conditions in a set must be true):")
    for name, fn in sorted(ENTRY_REGISTRY.items()):
        sig = str(inspect.signature(fn)).replace("(df, ", "(").replace("(df)", "()")
        print(f"  {name}{sig}\n      {(fn.__doc__ or '').strip().splitlines()[0] if fn.__doc__ else ''}")
    print("\nEXIT RULES (first rule to trigger closes the trade):")
    for name, cls in sorted(EXIT_REGISTRY.items()):
        sig = str(inspect.signature(cls.__init__)).replace("(self, ", "(").replace("(self)", "()")
        print(f"  {name}{sig}\n      {(cls.__doc__ or '').strip().splitlines()[0] if cls.__doc__ else ''}")


def main():
    ap = argparse.ArgumentParser(description="Modular strategy backtester")
    ap.add_argument("-c", "--config", help="YAML config file")
    ap.add_argument("-x", "--excel", help="Runner.xlsx control panel (instead of --config)")
    ap.add_argument("--open", action="store_true", help="open the output folder when finished")
    ap.add_argument("--tickers", help="ALL or comma list, overrides config")
    ap.add_argument("--timeframes", help="comma list e.g. 1D,1h,15m, overrides config")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--run-name")
    ap.add_argument("--rebuild-cache", action="store_true", help="re-create cached 1-min bars from raw data")
    ap.add_argument("--list", action="store_true", help="list available entry/exit types and exit")
    a = ap.parse_args()

    if a.list:
        list_types()
        return
    if not (a.config or a.excel):
        ap.error("--config or --excel is required")
    base = None
    if a.excel:
        from backtester.excel_runner import read_runner
        base = read_runner(a.excel)
    cfg = load_config(a.config, {
        "tickers": a.tickers,
        "timeframes": a.timeframes.split(",") if a.timeframes else None,
        "start": a.start, "end": a.end, "workers": a.workers, "run_name": a.run_name,
    }, base=base)
    out_dir = run(cfg, rebuild_cache=a.rebuild_cache)
    if a.open:
        os.startfile(out_dir)


if __name__ == "__main__":
    main()
