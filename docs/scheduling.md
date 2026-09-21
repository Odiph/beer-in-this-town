# Scheduling

Two jobs are worth running unattended, for different reasons:

| Script | Cadence | Why |
|---|---|---|
| `run_catchup.ps1` | nightly | Drains a pin/notes backlog that the write guardrails deliberately spread over several days. |
| `run_weekly.ps1` | weekly | Refreshes the data, so the check-in numbers and the new-venue diff stay current. |

Both wrappers log to `logs/`, prune their own old logs, and preserve the exit
code so a failed run shows up as a failed task rather than a silent no-op.

Nothing here needs a scheduler. Running either script by hand does the same
thing; the schedule only saves you remembering.

## Finishing a big list over several nights

The write guardrails cap how much can be done per day, so a hundred venues is
deliberately more than one session. `run_catchup.ps1` handles that: it pins
whatever is still missing, then annotates whatever is already pinned, and both
halves trim themselves to the remaining budget. Run it nightly and the backlog
drains on its own; once everything is done it exits in seconds having done
nothing.

It deliberately passes no `--limit` and raises no cap. The guardrails decide
how much work is safe tonight, not the schedule.

### Windows

```powershell
$script = Join-Path $PWD "run_catchup.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
Register-ScheduledTask -TaskName "beer-in-this-town catchup" -Force `
    -Action $action -Trigger (New-ScheduledTaskTrigger -Daily -At "01:15") `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

`-StartWhenAvailable` matters: the machine is usually asleep at 01:15, and
without it a missed run is simply skipped.

To check on it, or remove it:

```powershell
Get-ScheduledTask -TaskName "beer-in-this-town catchup" | Get-ScheduledTaskInfo
Unregister-ScheduledTask -TaskName "beer-in-this-town catchup"
```

A `LastTaskResult` of `267011` means the task has not run yet, not that it
failed.

### Linux and macOS

The `.ps1` wrappers are Windows-shaped. Elsewhere, call the two commands
directly — they are the whole of what `run_catchup.ps1` does:

```bash
cd /path/to/beer-in-this-town
CSV=data/seed_singapore_top100_2026-08-20.csv
LIST="Singapore Bars"

.venv/bin/beertown pin   --csv "$CSV" --list "$LIST"
.venv/bin/beertown notes --csv "$CSV" --list "$LIST"
```

Save that as `run_catchup.sh`, make it executable, and schedule it:

```cron
15 1 * * * cd /path/to/beer-in-this-town && ./run_catchup.sh >> logs/catchup.log 2>&1
```

`cron` has no equivalent of `StartWhenAvailable` — a machine asleep at 01:15
simply misses the run. `anacron`, or a systemd timer with `Persistent=true`,
covers that if it matters.

Both commands drive a real, non-headless Chrome window, so they need a session
that can open one. On a headless box that means `xvfb-run`, and it is worth
asking whether unattended UI automation is what you want at all — see
[Where the lines are](../README.md#where-the-lines-are).

## Keeping the data current

`run_weekly.ps1` re-runs the collection with the query and count baked into the
script, so the CSV, the KML and the new-venue diff stay current. Edit the
script to change the city.

### Windows

```powershell
schtasks /Create /TN "beer-in-this-town weekly" `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\run_weekly.ps1" `
  /SC WEEKLY /D SUN /ST 03:00 /RL LIMITED /F
```

Tick "Run task as soon as possible after a scheduled start is missed" and
"Start only if network is available".

### Linux and macOS

```cron
0 3 * * 0 cd /path/to/beer-in-this-town && .venv/bin/beertown run --query singapore --count 100 --no-upload >> logs/weekly.log 2>&1
```

`run` only reads. It is the half of this project that touches no account and
writes nothing to Google, so it is the safe one to leave on a timer.
