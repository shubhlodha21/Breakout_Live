# Saves Runner.xlsx if it is open in Excel, so the backtest always reads the latest settings.
# Run with Windows PowerShell (powershell.exe) - GetActiveObject is not available in PowerShell 7.
param([string]$Path)
$saved = $false
try {
    $xl = [Runtime.InteropServices.Marshal]::GetActiveObject("Excel.Application")
    foreach ($wb in $xl.Workbooks) {
        if ($wb.FullName -ieq $Path) {
            if (-not $wb.Saved) { $wb.Save() }
            $saved = $true
            Write-Host "Runner.xlsx is open in Excel - saved it before running."
        }
    }
} catch {
    # Excel not running - nothing to save
}

# Excel keeps a "~$Runner.xlsx" lock file while the workbook is open
$lock = Join-Path (Split-Path $Path) ("~$" + (Split-Path $Path -Leaf))
if (-not $saved -and (Test-Path -LiteralPath $lock)) {
    Write-Host ""
    Write-Host "  WARNING: Runner.xlsx is open in Excel and could not be saved automatically." -ForegroundColor Yellow
    Write-Host "  Unsaved changes will NOT be used. Press Ctrl+S in Excel, then run again." -ForegroundColor Yellow
    Write-Host ""
}
