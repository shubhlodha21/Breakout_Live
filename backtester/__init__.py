"""Modular strategy backtester.

Layers:
    data.py        -> load / clean / split-adjust / resample price data
    indicators.py  -> pure indicator functions
    entries.py     -> entry condition plug-ins (combined with AND)
    exits.py       -> exit rule plug-ins (combined with OR, first hit wins)
    engine.py      -> bar-by-bar simulation of one ticker / timeframe / entry set / exit set
    metrics.py     -> performance statistics
    report.py      -> Excel / CSV / chart output
    runner.py      -> config parsing + parallel orchestration
"""
