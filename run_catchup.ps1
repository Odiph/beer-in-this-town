# run_catchup.ps1 -- finish the backlog, a nightly bite at a time.
#
# Runs pin (places not yet in the list) then notes (stats onto places that are).
# Both commands are self-limiting: they read the rolling 24h write ledger and
# trim themselves to whatever budget is left, so this is safe to run nightly.
# Once everything is pinned and noted, both exit in seconds having done nothing.
#
# Deliberately NOT passing --limit or raising any cap: the guardrails decide how
# much work is safe tonight, not the schedule.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py   = Join-Path $Root ".venv\Scripts\python.exe"
$Csv  = Join-Path $Root "data\seed_singapore_top100_2026-08-20.csv"
$List = "Singapore Bars"

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("catchup_" + (Get-Date -Format "yyyy-MM-dd_HHmm") + ".log")

Set-Location $Root

"=== catchup started $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Tee-Object -FilePath $Log

# 1. Pin whatever is still missing from the list.
& $Py -m beer_in_this_town pin --csv $Csv --list $List *>&1 |
    Tee-Object -FilePath $Log -Append
$pinExit = $LASTEXITCODE

# 2. Then annotate. If the ledger is spent, this stops itself and resumes
#    tomorrow -- that is the intended behaviour, not a failure.
& $Py -m beer_in_this_town notes --csv $Csv --list $List *>&1 |
    Tee-Object -FilePath $Log -Append
$noteExit = $LASTEXITCODE

& $Py -m beer_in_this_town status *>&1 | Tee-Object -FilePath $Log -Append

# Keep the 30 most recent logs.
Get-ChildItem $LogDir -Filter "catchup_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force

# A guardrail stopping the run early is a success, not a failure: exit 0 unless
# both halves actually errored.
if ($pinExit -ne 0 -and $noteExit -ne 0) { exit 1 } else { exit 0 }
