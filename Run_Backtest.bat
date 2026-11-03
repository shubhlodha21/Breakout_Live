@echo off
REM Saves Runner.xlsx (if open in Excel), runs the backtest it defines, then opens the output folder.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0save_runner.ps1" "%~dp0Runner.xlsx"
echo Reading Runner.xlsx ...
python run_backtest.py --excel Runner.xlsx --open
echo.
pause
