"""Excel control panel (Runner.xlsx): build the template and read it back into a config dict.

Sheets
    Settings  - run name, dates, timeframe Yes/No list + RUN link (always runs ALL tickers)
    Entries   - one row per condition; rows sharing a Set Name are ANDed
    Exits     - one row per rule; rows sharing a Set Name are ORed (first hit wins)
    Help      - every entry/exit type with its parameters and defaults

Everything else (session, fills, costs, workers, data folders) comes from runner.DEFAULTS.
"""
from __future__ import annotations

import inspect
import re
from datetime import date, datetime
from pathlib import Path

import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

from .entries import ENTRY_REGISTRY
from .exits import EXIT_REGISTRY

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1D", "1W"]

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(bold=True, color="FFFFFF")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
SECTION_FONT = Font(bold=True, size=12, color="1F3864")
THIN = Border(*(Side(style="thin", color="BFBFBF"),) * 4)

# (key, label, default, dropdown options or None, note)
SETTINGS = [
    ("run_name", "Run name", "breakout_52wh_200sma", None, "Used for the output folder name"),
    ("tickers", "Tickers", "ALL", "@tickers",
     "ALL = every ticker | pick ONE ticker from the dropdown | or type several: AAPL, NVDA, MSFT"),
    ("start", "Start date", date(2019, 1, 1), None, "First allowed entry (earlier data used for warm-up)"),
    ("end", "End date", date(2026, 4, 30), None, "No data after this date is used"),
    ("use_exits", "Use exit conditions", "Yes", ["Yes", "No"],
     "Yes = exit sets on the Exits sheet | No = no exits: enter once on the first signal and hold "
     "to the end of data (one trade per ticker / timeframe / entry set)"),
]

DEFAULT_ENTRIES = [
    ("52WH", "Yes", "high_52w", "lookback_days=365, price=close"),
    ("Above 200SMA", "Yes", "sma_above", "period=200"),
]

DEFAULT_EXITS = [
    ("SL 2%", "Yes", "fixed_sl", "pct=2"),
    ("Risk:Reward 1:2", "Yes", "risk_reward", "sl_pct=2, rr=2"),
]

MAX_RULE_ROWS = 200


# ============================================================================ template
def build_template(path: Path, bat_name: str = "Run_Backtest.bat"):
    """Create Runner.xlsx. If it already exists, the user's settings and Entries/Exits rows are kept."""
    values, entries, exits = {}, DEFAULT_ENTRIES, DEFAULT_EXITS
    if Path(path).exists():
        old = load_workbook(path, data_only=True)
        values = _settings_values(old["Settings"])
        entries = _rule_rows(old["Entries"]) or DEFAULT_ENTRIES
        exits = _rule_rows(old["Exits"]) or DEFAULT_EXITS
    wb = Workbook()
    lists = wb.create_sheet("Lists")
    n_list = _lists_sheet(lists)
    _settings_sheet(wb.active, bat_name, values, n_list)
    _rules_sheet(wb.create_sheet("Entries"), entries, sorted(ENTRY_REGISTRY),
                 "ENTRY SETS - all ACTIVE rows with the same Set Name must be true (AND)")
    _rules_sheet(wb.create_sheet("Exits"), exits, sorted(EXIT_REGISTRY),
                 "EXIT SETS - rows with the same Set Name: the first rule to trigger closes the trade (OR)")
    _help_sheet(wb.create_sheet("Help"))
    wb.move_sheet(lists, offset=len(wb.sheetnames))  # keep the hidden sheet last
    wb.active = 0
    wb.save(path)


def _lists_sheet(ws) -> int:
    """Hidden source for the Tickers dropdown: ALL + every ticker with data. Returns row count."""
    from .data import DataStore
    from .runner import DEFAULTS
    store = DataStore(DEFAULTS["data_dir"], DEFAULTS["csv_dir"], DEFAULTS["cache_dir"])
    names = ["ALL"] + sorted(set(store.intraday_tickers()) | set(store.csv_tickers()))
    for i, t in enumerate(names, 1):
        ws.cell(row=i, column=1, value=t)
    ws.sheet_state = "hidden"
    return len(names)


def _settings_values(ws) -> dict:
    """{key: value} from the hidden key column Z (settings and 'tf:<timeframe>' rows)."""
    return {ws.cell(row=r, column=26).value: ws.cell(row=r, column=2).value
            for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=26).value}


def _rule_rows(ws) -> list[tuple]:
    return [row for row in ws.iter_rows(min_row=5, max_col=4, values_only=True) if any(row)]


def _yes_no(ws, ref):
    dv = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(ref)


def _header(ws, row, cols):
    for i, txt in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=txt)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, THIN


def _settings_sheet(ws, bat_name, values: dict, n_list: int):
    ws.title = "Settings"
    ws["A1"] = "STRATEGY BACKTEST RUNNER  -  runs on ALL tickers"
    ws["A1"].font = Font(bold=True, size=16, color="1F3864")
    ws["A2"] = ("1) Fill the yellow cells on Settings / Entries / Exits   "
                "2) SAVE the workbook (Ctrl+S)   3) Click RUN BACKTEST")
    ws["A2"].font = Font(italic=True, color="595959")

    run = ws["E4"]
    run.value = "▶  RUN BACKTEST"
    run.hyperlink = bat_name
    run.font = Font(bold=True, size=14, color="FFFFFF")
    run.fill = PatternFill("solid", fgColor="00A65A")
    run.alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells("E4:F5")
    ws["E6"] = "(save first - Excel may ask to confirm opening the .bat)"
    ws["E6"].font = Font(italic=True, size=9, color="595959")

    _header(ws, 4, ["Parameter", "Value", "Notes"])
    r = 5
    for key, label, default, opts, note in SETTINGS:
        ws.cell(row=r, column=1, value=label).border = THIN
        v = ws.cell(row=r, column=2, value=values.get(key, default))
        v.fill, v.border = INPUT_FILL, THIN
        if isinstance(default, date):
            v.number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=3, value=note).font = Font(size=9, color="595959")
        ws.cell(row=r, column=26, value=key)  # hidden key column used by the reader
        if opts:
            # dropdown offers the options but free text is still accepted (e.g. a ticker list).
            # opts = list of literals, or "@tickers" -> range on the hidden Lists sheet (too long to inline).
            formula = (f"Lists!$A$1:$A${n_list}" if opts == "@tickers"
                       else '"' + ",".join(opts) + '"')
            dv = DataValidation(type="list", formula1=formula, allow_blank=False, showErrorMessage=False)
            ws.add_data_validation(dv)
            dv.add(v.coordinate)
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="TIMEFRAMES").font = SECTION_FONT
    r += 1
    _header(ws, r, ["Timeframe", "Run?", "Notes"])
    notes = {"1D": "Daily (from CSV or DataBento)", "1W": "Weekly",
             "1m": "Slowest - first intraday run builds a cache for all tickers"}
    for tf in TIMEFRAMES:
        r += 1
        ws.cell(row=r, column=1, value=tf).border = THIN
        v = ws.cell(row=r, column=2, value=values.get(f"tf:{tf}", "Yes" if tf == "1D" else "No"))
        v.fill, v.border = INPUT_FILL, THIN
        _yes_no(ws, v.coordinate)
        ws.cell(row=r, column=3, value=notes.get(tf, "")).font = Font(size=9, color="595959")
        ws.cell(row=r, column=26, value=f"tf:{tf}")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 70
    ws.column_dimensions["E"].width = 22
    ws.column_dimensions["F"].width = 22
    ws.column_dimensions["Z"].hidden = True
    ws.freeze_panes = "A5"


def _rules_sheet(ws, defaults, types, title):
    ws["A1"] = title
    ws["A1"].font = SECTION_FONT
    ws["A2"] = ("Parameters: key=value, comma separated, e.g.  period=200, timeframe=1D   "
                "(blank = defaults; see Help sheet).  Active = No to skip a row.")
    ws["A2"].font = Font(italic=True, color="595959")
    _header(ws, 4, ["Set Name", "Active", "Type", "Parameters"])
    dv_type = DataValidation(type="list", formula1='"' + ",".join(types) + '"', allow_blank=True)
    ws.add_data_validation(dv_type)
    for i in range(MAX_RULE_ROWS):
        r = 5 + i
        vals = defaults[i] if i < len(defaults) else (None, None, None, None)
        for c, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.fill, cell.border = INPUT_FILL, THIN
    _yes_no(ws, f"B5:B{4 + MAX_RULE_ROWS}")
    dv_type.add(f"C5:C{4 + MAX_RULE_ROWS}")
    for col, w in zip("ABCD", (34, 9, 22, 60)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"


def _help_sheet(ws):
    _header(ws, 1, ["Kind", "Type", "Parameters (defaults)", "Description"])
    r = 2
    for kind, reg in (("ENTRY", ENTRY_REGISTRY), ("EXIT", EXIT_REGISTRY)):
        for name, obj in sorted(reg.items()):
            fn = obj if kind == "ENTRY" else obj.__init__
            params = [f"{p.name}={p.default}" for p in inspect.signature(fn).parameters.values()
                      if p.name not in ("df", "self", "label")]
            doc = " ".join((obj.__doc__ or "").split())
            for c, v in enumerate((kind, name, ", ".join(params) or "(none)", doc), 1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            r += 1
    r += 1
    notes = [
        "Percent parameters are in percent: pct=8 means 8%.",
        "Any entry condition accepts negate=true to invert it.",
        "Entry/exit 'label=My Name' renames the exit reason shown in reports.",
        "timeframe=1D on sma_above / sma_below / ma_exit uses the DAILY moving average on an intraday chart.",
        "Stops: gap through the stop fills at the open; if stop and target are both hit in one bar, the stop is assumed.",
    ]
    for n in notes:
        ws.cell(row=r, column=1, value="NOTE")
        ws.cell(row=r, column=2, value=n)
        r += 1
    for col, w in zip("ABCD", (8, 22, 55, 90)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"


# ============================================================================ reader
def _split_params(text: str) -> list[str]:
    """Split on commas that are not inside [] or ()."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def parse_params(text) -> dict:
    if text is None or str(text).strip() == "":
        return {}
    params = {}
    for part in _split_params(str(text)):
        if "=" not in part:
            raise ValueError(f"Bad parameter '{part}' - use key=value")
        k, v = (s.strip() for s in part.split("=", 1))
        if re.fullmatch(r"\d{1,2}:\d{2}", v):          # times like 09:45 stay strings
            params[k] = v
        else:
            val = yaml.safe_load(v)
            params[k] = val if val is not None else v
    return params


def _read_sets(ws, kind: str) -> list[dict]:
    sets: dict[str, list] = {}
    for row in ws.iter_rows(min_row=5, max_col=4, values_only=True):
        name, active, typ, params = row
        if not name or not typ or str(active or "Yes").strip().lower() != "yes":
            continue
        rule = {"type": str(typ).strip(), **parse_params(params)}
        sets.setdefault(str(name).strip(), []).append(rule)
    key = "conditions" if kind == "entries" else "rules"
    return [{"name": n, key: rules} for n, rules in sets.items()]


def read_runner(path: str | Path) -> dict:
    """Runner.xlsx -> config dict. Tickers = ALL or a typed list; other settings use runner.DEFAULTS."""
    wb = load_workbook(path, data_only=True, read_only=False)
    values = _settings_values(wb["Settings"])
    tfs = [k[3:] for k, v in values.items() if k.startswith("tf:") and str(v or "").strip().lower() == "yes"]
    raw = str(values.get("tickers") or "ALL").strip()
    tickers = "ALL" if raw.upper() == "ALL" else [t.strip().upper() for t in re.split(r"[,;\s]+", raw) if t.strip()]
    cfg = {"tickers": tickers or "ALL", "run_name": values.get("run_name") or None,
           "use_exits": str(values.get("use_exits") or "Yes").strip().lower() != "no"}
    for k in ("start", "end"):
        v = values.get(k)
        if isinstance(v, datetime):
            v = v.date()
        cfg[k] = str(v) if v else None
    if not tfs:
        raise ValueError("No timeframe set to Yes on the Settings sheet")
    cfg["timeframes"] = tfs
    cfg["entries"] = _read_sets(wb["Entries"], "entries")
    cfg["exits"] = _read_sets(wb["Exits"], "exits")
    return cfg
