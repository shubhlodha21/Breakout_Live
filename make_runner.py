"""Create (or re-create) the Excel control panel Runner.xlsx.

    python make_runner.py            # refuses to overwrite an existing Runner.xlsx
    python make_runner.py --force    # overwrite (your edits in it are lost)

Re-run after adding new entry/exit types so their dropdowns and Help rows appear.
"""
import argparse
from pathlib import Path

from backtester.excel_runner import build_template

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "Runner.xlsx"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists() and not a.force:
        raise SystemExit(f"{out} exists - use --force to overwrite")
    build_template(out)
    print(f"Created {out}")
