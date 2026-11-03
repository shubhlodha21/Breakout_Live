"""Price data loading.

Sources
-------
* DataBento 1-second OHLCV  (<data_dir>/<TICKER>.parquet or <TICKER>.dbn.zst)
    - unadjusted prices, extended hours 04:00-20:00 ET, 2018-05 onwards
    - converted once to split-adjusted 1-minute bars and cached in <cache_dir>/1min_<session>/
* Yahoo-style daily CSV      (<csv_dir>/<TICKER>.csv)  Date,Open,High,Low,Close,Adj Close,Volume
    - split-adjusted, long history (1999+). Used for 1D / 1W when daily_source = "csv".
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

# User-facing timeframe aliases -> pandas rule
TF_ALIASES = {
    "1m": "1min", "1min": "1min",
    "3m": "3min", "3min": "3min",
    "5m": "5min", "5min": "5min",
    "10m": "10min", "10min": "10min",
    "15m": "15min", "15min": "15min",
    "30m": "30min", "30min": "30min",
    "1h": "60min", "60m": "60min", "60min": "60min",
    "2h": "120min", "120min": "120min",
    "4h": "240min", "240min": "240min",
    "1d": "1D", "d": "1D", "daily": "1D",
    "1w": "1W", "w": "1W", "weekly": "1W",
}

SESSIONS = {"rth": ("09:30", "15:59:59"), "all": ("04:00", "19:59:59")}

# Common split ratios (forward and reverse) used to snap detected factors.
_NICE_RATIOS = sorted({r for n in (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50) for r in (n, 1 / n)}
                      | {4 / 3, 3 / 4, 5 / 4, 4 / 5, 5 / 3, 3 / 5})
_STRICT_RATIOS = sorted({r for n in (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50) for r in (n, 1 / n)})


def normalize_tf(tf: str) -> str:
    key = str(tf).strip().lower()
    if key not in TF_ALIASES:
        raise ValueError(f"Unknown timeframe '{tf}'. Valid: {sorted(set(TF_ALIASES))}")
    return TF_ALIASES[key]


def is_intraday(rule: str) -> bool:
    return rule.endswith("min")


class DataStore:
    def __init__(self, data_dir: str, csv_dir: str | None, cache_dir: str,
                 session: str = "rth", daily_source: str = "csv", rebuild_cache: bool = False):
        self.data_dir = Path(data_dir)
        self.csv_dir = Path(csv_dir) if csv_dir else None
        self.cache_dir = Path(cache_dir)
        if session not in SESSIONS:
            raise ValueError(f"session must be one of {list(SESSIONS)}")
        self.session = session
        self.daily_source = daily_source
        self.rebuild_cache = rebuild_cache

    # ------------------------------------------------------------------ discovery
    def intraday_tickers(self) -> list[str]:
        names = {p.name[: -len(".parquet")] for p in self.data_dir.glob("*.parquet")}
        names |= {p.name[: -len(".dbn.zst")] for p in self.data_dir.glob("*.dbn.zst")}
        return sorted(names)

    def csv_tickers(self) -> list[str]:
        if not self.csv_dir or not self.csv_dir.exists():
            return []
        # ticker files are upper-case (skips helper files such as constituents.csv)
        return sorted(p.stem for p in self.csv_dir.glob("*.csv") if p.stem == p.stem.upper())

    def all_tickers(self, timeframes: list[str]) -> list[str]:
        rules = [normalize_tf(t) for t in timeframes]
        names = set(self.intraday_tickers())
        # CSV-only tickers are usable only when every requested timeframe is daily/weekly from CSV
        if self.daily_source == "csv" and not any(is_intraday(r) for r in rules):
            names |= set(self.csv_tickers())
        return sorted(names)

    # ------------------------------------------------------------------ public API
    def get_bars(self, ticker: str, timeframe: str) -> tuple[pd.DataFrame, str]:
        """Return (bars, source_label). Index = bar start time (ET, tz-naive)."""
        rule = normalize_tf(timeframe)
        csv_path = self.csv_dir / f"{ticker}.csv" if self.csv_dir else None

        if not is_intraday(rule) and self.daily_source == "csv" and csv_path and csv_path.exists():
            daily = self.load_daily_csv(ticker)
            return (daily if rule == "1D" else _to_weekly(daily)), "yahoo_csv"

        m1 = self.load_1min(ticker)
        if rule == "1min":
            return m1, "databento"
        if is_intraday(rule):
            return _resample_intraday(m1, rule, SESSIONS[self.session][0]), "databento"
        daily = _to_daily(m1)
        return (daily if rule == "1D" else _to_weekly(daily)), "databento"

    # ------------------------------------------------------------------ loaders
    def load_daily_csv(self, ticker: str) -> pd.DataFrame:
        df = pd.read_csv(self.csv_dir / f"{ticker}.csv", parse_dates=["Date"])
        df = df.rename(columns=str.lower).set_index("date").sort_index()
        df = df[OHLCV].apply(pd.to_numeric, errors="coerce").dropna(subset=["open", "high", "low", "close"])
        df = df[(df["close"] > 0)]
        df.index.name = "time"
        return df

    def load_1min(self, ticker: str) -> pd.DataFrame:
        cache = self.cache_dir / f"1min_{self.session}" / f"{ticker}.parquet"
        if cache.exists() and not self.rebuild_cache:
            return pd.read_parquet(cache)

        # Stream the 1-second file in chunks (some tickers have 30M+ rows) and aggregate to 1-minute.
        start, end = SESSIONS[self.session]
        t0, t1 = (_hms_seconds(start), _hms_seconds(end))
        parts = []
        for chunk in self._iter_raw_seconds(ticker):
            tod = chunk.index.hour * 3600 + chunk.index.minute * 60 + chunk.index.second
            chunk = chunk[(tod >= t0) & (tod <= t1)]
            if len(chunk):
                parts.append(chunk.groupby(chunk.index.floor("1min")).agg(AGG))
        if not parts:
            raise ValueError(f"No {self.session} data for {ticker}")
        m1 = pd.concat(parts)
        if m1.index.has_duplicates or not m1.index.is_monotonic_increasing:  # minute split across chunks
            m1 = m1.sort_index(kind="stable").groupby(level=0).agg(AGG)
        m1.index.name = "time"
        m1, splits = self._adjust_splits(ticker, m1)
        m1.attrs = {}
        cache.parent.mkdir(parents=True, exist_ok=True)
        m1.to_parquet(cache)
        # audit trail: cache/splits/<TICKER>.csv (rewritten whenever the ticker's cache is rebuilt)
        split_log = self.cache_dir / "splits" / f"{ticker}.csv"
        split_log.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([(ticker, d.date(), round(f, 6)) for d, f in splits],
                     columns=["ticker", "date", "factor"]).to_csv(split_log, index=False)
        return m1

    def _iter_raw_seconds(self, ticker: str, chunk_rows: int = 2_000_000):
        """Yield chronological chunks of 1-second bars indexed by ET (tz-naive) timestamps."""
        pq_path = self.data_dir / f"{ticker}.parquet"
        dbn = self.data_dir / f"{ticker}.dbn.zst"
        if pq_path.exists():
            import pyarrow.parquet as pq
            for batch in pq.ParquetFile(pq_path).iter_batches(batch_size=chunk_rows, columns=["time"] + OHLCV):
                df = batch.to_pandas()
                df.index = pd.to_datetime(df.pop("time"), format="%Y-%m-%d %H:%M:%S")  # already ET
                yield df
        elif dbn.exists():
            import warnings
            import databento as db
            with warnings.catch_warnings():  # files still downloading may end in a partial record
                warnings.simplefilter("ignore")
                for df in db.DBNStore.from_file(dbn).to_df(count=chunk_rows):
                    df = df[OHLCV]
                    df.index = df.index.tz_convert("America/New_York").tz_localize(None)  # UTC -> ET
                    yield df
        else:
            raise FileNotFoundError(f"No intraday data for {ticker}")

    # ------------------------------------------------------------------ split adjustment
    def _adjust_splits(self, ticker: str, m1: pd.DataFrame):
        """Detect splits from overnight gaps and back-adjust prices/volume.

        A gap prev_close -> open of > 30% is a split candidate. If the Yahoo CSV covers both days and
        shows no such gap there, it is a split (factor = raw gap / yahoo gap). Without a CSV, the gap
        must be within 4% of a common split ratio (2:1, 3:1, 1:10, ...) to be treated as a split.
        """
        daily = _to_daily(m1)
        if len(daily) < 2:
            return m1, []
        yahoo = None
        if self.csv_dir and (self.csv_dir / f"{ticker}.csv").exists():
            yahoo = self.load_daily_csv(ticker)

        prev_close = daily["close"].shift(1)
        gap = (prev_close / daily["open"]).iloc[1:]
        splits = []
        for day, g in gap.items():
            if not np.isfinite(g) or abs(math.log(g)) < math.log(1.3):
                continue
            prev_day = daily.index[daily.index.get_loc(day) - 1]
            factor = None
            if yahoo is not None and day in yahoo.index and prev_day in yahoo.index:
                gy = yahoo.at[prev_day, "close"] / yahoo.at[day, "open"]
                f = g / gy
                if abs(math.log(f)) >= math.log(1.3):
                    factor = _snap(f, 0.06) or f
            elif abs(math.log(g)) >= math.log(1.8):
                # No reference data: only whole-number splits / reverse splits. Fractional ratios
                # (4:3, 3:2 ...) look too much like genuine gaps on volatile names (e.g. GME 2021).
                factor = _snap(g, 0.03, _STRICT_RATIOS)
            if factor:
                splits.append((day, factor))

        splits = _apply_overrides(ticker, splits)
        if splits:
            m1 = m1.astype({"volume": "float64"})  # uint64 can't take fractional split factors
            px = ["open", "high", "low", "close"]
            for day, f in splits:
                mask = m1.index < day
                m1.loc[mask, px] = m1.loc[mask, px] / f
                m1.loc[mask, "volume"] = m1.loc[mask, "volume"] * f
        return m1, splits


SPLIT_OVERRIDES = Path(__file__).resolve().parent.parent / "split_overrides.csv"


def _apply_overrides(ticker: str, splits: list) -> list:
    """split_overrides.csv (ticker,date,factor) replaces auto-detected splits on that date.
    factor = 1 cancels a false detection; any other factor adds/corrects a split."""
    if not SPLIT_OVERRIDES.exists():
        return splits
    ov = pd.read_csv(SPLIT_OVERRIDES, comment="#")
    ov = ov[ov["ticker"].str.upper() == ticker.upper()]
    if ov.empty:
        return splits
    by_day = {d: f for d, f in splits}
    for _, r in ov.iterrows():
        by_day[pd.Timestamp(r["date"])] = float(r["factor"])
    return sorted((d, f) for d, f in by_day.items() if f != 1)


def _hms_seconds(hms: str) -> int:
    parts = [int(p) for p in hms.split(":")] + [0]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _snap(x: float, tol: float, ratios=None):
    for r in ratios or _NICE_RATIOS:
        if abs(x / r - 1) <= tol:
            return r
    return None


def _to_daily(bars: pd.DataFrame) -> pd.DataFrame:
    d = bars.groupby(bars.index.normalize()).agg(AGG)
    d.index.name = "time"
    return d.dropna(subset=["open"])


def _to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    # Label each week by its first trading day so "bar start" semantics match other timeframes.
    wk = daily.index.to_period("W-FRI")
    w = daily.groupby(wk).agg(AGG)
    w.index = daily.index.to_series().groupby(wk).first().values
    w.index.name = "time"
    return w


def _resample_intraday(m1: pd.DataFrame, rule: str, session_start: str) -> pd.DataFrame:
    h, m = map(int, session_start.split(":"))
    out = m1.resample(rule, label="left", closed="left", origin="start_day",
                      offset=pd.Timedelta(hours=h, minutes=m)).agg(AGG)
    return out.dropna(subset=["open"])
