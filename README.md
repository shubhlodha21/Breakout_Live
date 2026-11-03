# Modular Strategy Backtester

Tests every combination of **tickers × timeframes × entry sets × exit sets** and writes a trade sheet + summary reports.

## Quick start (Excel)
1. Open `Runner.xlsx` and fill in the yellow cells:
   - **Settings**: tickers (ALL / SELECTED / LIST), dates, costs, and the timeframes to run (Yes/No)
   - **Entries**: one row per condition. Rows with the same *Set Name* are ANDed.
   - **Exits**: one row per rule. Rows with the same *Set Name* are ORed: the first one to trigger closes the trade.
   - **Tickers**: set *Select = Yes* when Ticker mode = SELECTED
   - **Help**: every entry/exit type with its parameters
2. **Save** (Ctrl+S), then click **▶ RUN BACKTEST** (or double-click `Run_Backtest.bat`).
3. The results folder opens automatically when the run finishes.

## Command line
```
python run_backtest.py --excel Runner.xlsx
python run_backtest.py -c configs/breakout.yaml
python run_backtest.py -c configs/breakout.yaml --tickers AAPL,NVDA --timeframes 1D,1h --start 2020-01-01
python run_backtest.py --list                 # all entry / exit types
python make_runner.py --force                 # regenerate Runner.xlsx (e.g. after adding new types)
```

## Output: `output/<run_name>_<timestamp>/`
| File / sheet | Contents |
|---|---|
| `report.xlsx` → **Combo_Summary** | per timeframe × entry set × exit set, all tickers pooled: trades, win rate, avg/median return, profit factor, payoff, expectancy, max DD, % of tickers profitable, median ticker CAGR, exposure, buy & hold |
| **Ticker_Summary** | the same per ticker, plus compounded return, CAGR, max DD, exposure % and buy & hold % |
| **Trades** | the trade sheet: signal/entry/exit time and price, initial stop, exit reason, qty, bars/days held, P&L, return %, R-multiple, MAE %, MFE % |
| **Yearly**, **Exit_Reasons** | breakdown by year and by exit reason |
| **Data_Coverage**, **Errors**, **Config** | which data was used, skipped tickers, and the exact settings |
| `trades.csv`, `*_summary.csv` | CSV copies (trades.csv is never truncated) |
| `charts/` | equity + drawdown chart per combination, plus a chart comparing all combinations |

## Rules of the simulation
- Signals are evaluated on the **bar close**. Entries fill at the **next bar's open** by default (`entry_on: close` is an option).
- Stops and targets are checked inside each bar. A gap through the stop fills at the open. If the stop and target are both hit in the same bar, the **stop** is assumed.
- Signal-based exits (MA exit, lower-low exit) fill at the next open by default.
- Long only, one position at a time per ticker. Each trade uses a fixed `capital_per_trade`; % returns don't depend on it.
- Costs: commission per share (with a minimum per order) and slippage % per side.

## Data
- `D:\DataBento\data\*.parquet | *.dbn.zst` are 1-second bars, unadjusted, 04:00–20:00 ET. On first use they're converted to
  **split-adjusted 1-minute bars** cached in `cache/1min_<session>/`. Every higher timeframe is built from those.
  Splits are checked against the Yahoo CSV where one exists. Without a CSV, only whole-number ratios (2:1, 1:10, …) are treated as splits.
  Detected splits are logged in `cache/splits/`. Delete `cache/` (or pass `--rebuild-cache`) after updating the data.
- 1D / 1W bars come from `USA/*.csv` (adjusted, since 1999) when `daily_source = csv`; otherwise they're built from DataBento.
- Dividends are not adjusted in the intraday data.

## Adding a new condition
- Entry: add a function with `@register("name")` in `backtester/entries.py` that returns a boolean Series.
- Exit: add a class with `@register("name")` in `backtester/exits.py` that implements `stop` / `target` / `after_bar`.
- Then run `python make_runner.py --force` to refresh the Excel dropdowns and Help sheet.
