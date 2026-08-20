# run_weekly.ps1 -- wrapper for Windows Task Scheduler.
# Logs every run and preserves the exit code so a failed scrape shows as a
# failed task rather than a silent no-op.
$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir  = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp   = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LogFile = Join-Path $LogDir "run_$Stamp.log"

Set-Location $Root
& (Join-Path $Root ".venv\Scripts\python.exe") -m untappd_maps run `
    --query "singapore" --count 100 --title "Singapore Bars" --no-upload `
    *>&1 | Tee-Object -FilePath $LogFile

$code = $LASTEXITCODE

# Keep the 20 most recent logs.
Get-ChildItem $LogDir -Filter "run_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 20 |
    Remove-Item -Force

exit $code
