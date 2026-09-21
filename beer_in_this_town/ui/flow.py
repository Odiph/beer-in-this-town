"""The half of the walkthrough the dashboard explains rather than does.

The v0.2 flow has three stretches no button here can do for a person: setting
up the emulator, running the build commands, and putting the result into a
Google Maps list. The user's rule is "automate nothing new", so for each of
those this module produces instructions, copyable commands and whatever live
status can be read from disk -- never a subprocess, never a browser.

Everything here is pure data for the page. It reads files under `data/` and
nothing else, so it is safe to call on every poll.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

# One folder rule for the CLI and the dashboard, or they look in different
# places for the same city.
city_slug = config.city_slug

CLI = "beertown"
PIN_BUDGET_PER_DAY = 100


def _quote(value: str) -> str:
    """A city or list name, safe inside the double quotes of a shown command.

    Only for display: nothing here executes it. Double quotes are dropped
    rather than escaped, because the escape differs between PowerShell, cmd
    and bash and a person copies this into whichever they have open.
    """
    return config.arg_text(value)


def data_dir_for(city: str, root: Path | None = None) -> Path:
    return (root or config.DATA_DIR) / city_slug(city)


def rel(path: Path) -> str:
    """The path as a person types it from the project folder."""
    try:
        return path.relative_to(config.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------
# Counting what a stage wrote. Cached by (mtime, size): the page polls every
# second and a sweep CSV does not change that often.
# --------------------------------------------------------------------------
_COUNTS: dict[str, tuple[tuple[float, int], int | None]] = {}


def _count(path: Path) -> int | None:
    """Rows in a CSV, or placemarks in a KML. None when unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    sig = (st.st_mtime, st.st_size)
    cached = _COUNTS.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    try:
        if path.suffix == ".kml":
            n = path.read_text(encoding="utf-8", errors="replace").count(
                "<Placemark")
        else:
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                n = max(sum(1 for _ in csv.reader(f)) - 1, 0)
    except OSError as exc:
        log.warning("[ui] could not count %s: %s", path, exc)
        n = None
    _COUNTS[key] = (sig, n)
    return n


# --------------------------------------------------------------------------
# Step: emulator setup
# --------------------------------------------------------------------------
ADB_SERIAL = "127.0.0.1:5555"

EMULATOR_STEPS = (
    {"title": "Install BlueStacks 5",
     "body": "BlueStacks is a free Android emulator for Windows and macOS. "
             "Install version 5 and start it once so it finishes setting up.",
     "links": [{"label": "Download BlueStacks",
                "url": "https://www.bluestacks.com/download.html"}]},
    {"title": "Set the display to 900 x 1600, portrait",
     "body": "BlueStacks: Settings -> Display -> Display orientation: "
             "Portrait, Display resolution: 900 x 1600. Save and let it "
             "restart. Why: the sweep taps and swipes the Untappd map at "
             "positions measured on exactly this screen. Any other size and "
             "it misses the map and pans the wrong way."},
    {"title": "Turn on Android Debug Bridge (adb)",
     "body": "BlueStacks: Settings -> Advanced -> Android Debug Bridge (ADB): "
             "on. It shows an address such as 127.0.0.1:5555 -- note the "
             "port. adb is how this tool reads the screen and moves the map."},
    {"title": "Install adb on this computer",
     "body": "Download Android SDK Platform-Tools, unzip it, and add that "
             "folder to your PATH. Open a new terminal and connect to "
             "BlueStacks (use the port you noted if it is not 5555):",
     "cmd": f"adb connect {ADB_SERIAL}",
     "links": [{"label": "Download platform-tools",
                "url": "https://developer.android.com/tools/releases/"
                       "platform-tools"}]},
    {"title": "Install Untappd from the Play Store",
     "body": "Inside BlueStacks open the Play Store (sign in with any Google "
             "account), search for Untappd and install it."},
    {"title": "Sign in to Untappd in the app",
     "body": "Open Untappd in BlueStacks and sign in. The map the sweep reads "
             "is only shown to a signed-in user. A free account is enough.",
     "links": [{"label": "Create an Untappd account",
                "url": "https://untappd.com/signup"}]},
)

EMULATOR_NOTE = (
    "Press Check again after each change. If your port is not 5555, set "
    "BEERTOWN_ADB_SERIAL to the address BlueStacks shows (for example "
    "127.0.0.1:5565) before starting this dashboard."
)


def emulator_card(result: dict | None) -> dict:
    """Instructions, plus the last check's rows if one has run."""
    return {
        "steps": list(EMULATOR_STEPS),
        "note": EMULATOR_NOTE,
        "checked": result is not None,
        "ready": bool(result and result.get("ready")),
        "checks": list((result or {}).get("checks", [])),
        "at": (result or {}).get("at"),
        "error": (result or {}).get("error", ""),
        "doctor": f"{CLI} doctor --json",
    }


# --------------------------------------------------------------------------
# Step: accounts
# --------------------------------------------------------------------------
ACCOUNT_WHY = (
    {"key": "google", "label": "Google",
     "why": "The destination. Your map ends up as a saved list in Google "
            "Maps, and pin/notes save into it through this Chrome profile."},
    {"key": "untappd", "label": "Untappd (the website)",
     "why": "The stats. Each venue the sweep finds is matched to its Untappd "
            "venue page for check-ins, rating and exact coordinates. This is "
            "separate from signing in inside the app on the emulator."},
)


# --------------------------------------------------------------------------
# Step: build the map
# --------------------------------------------------------------------------
STAGES = (
    ("sweep", "Sweep the app map", "1_sweep.csv",
     "Pans the Untappd app's map across the city on the emulator and records "
     "every venue marker, then places them using OpenStreetMap. The slow one; "
     "if it stops, the same command resumes."),
    ("enrich", "Enrich from venue pages", "2_enriched.csv",
     "Matches each name to its Untappd venue page for stats and coordinates, "
     "keeping a match only if the page is within 1 km of where the map "
     "showed it."),
    ("filter", "Keep the beer venues", "3_venues.csv",
     "Keeps bars, breweries and taprooms. Everything dropped goes to "
     "3_excluded.csv with the reason, so you can check it."),
    ("export", "Export map files", "venues.kml",
     "Writes the venues as KML, GPX and GeoJSON: a backup, and a way to open "
     "them in map apps other than Google Maps."),
)

SWEEP_PRECONDITION = (
    "Before each sweep: BlueStacks running, the Untappd app open on "
    "Discover -> View Map, and nothing covering the map."
)
SWEEP_OPTIONS = (
    "Options: --here centres on the emulator's own location instead of "
    "searching the city by name; --min-depth / --max-depth (default 1 and 3) "
    "set how finely the map is split."
)
AGENT_NOTE = (
    "A coding agent can run these same commands with --json added; each then "
    "prints one JSON result it can read (see AGENTS.md)."
)


def _stage_command(key: str, city: str) -> str:
    cmd = f'{CLI} {key} --city "{_quote(city)}"'
    if key == "export":
        cmd += " --format kml,gpx,geojson"
    return cmd


def build_card(city: str | None, root: Path | None = None) -> dict:
    """Each stage for this city: command, explanation, done + count."""
    if not city:
        return {"city": None, "stages": []}
    folder = data_dir_for(city, root)
    stages = []
    for key, title, filename, explain in STAGES:
        path = folder / filename
        done = path.exists()
        row = {
            "key": key, "title": title, "explain": explain,
            "command": _stage_command(key, city),
            "file": rel(path), "done": done,
            "count": _count(path) if done else None,
        }
        if key == "sweep":
            row["precondition"] = SWEEP_PRECONDITION
            row["options"] = SWEEP_OPTIONS
        if key == "filter":
            excluded = folder / "3_excluded.csv"
            row["excluded"] = _count(excluded) if excluded.exists() else None
        if key == "export":
            row["also"] = [rel(folder / f"venues.{ext}")
                           for ext in ("gpx", "geojson")
                           if (folder / f"venues.{ext}").exists()]
        stages.append(row)
    return {
        "city": city, "slug": city_slug(city), "folder": rel(folder),
        "stages": stages, "agent_note": AGENT_NOTE,
        "next": next((s["key"] for s in stages if not s["done"]), None),
    }


def venues_ready(city: str | None, root: Path | None = None) -> bool:
    """Has `filter` written the file `pin` reads?"""
    return bool(city) and (data_dir_for(city, root) / "3_venues.csv").exists()


def swept(city: str | None, root: Path | None = None) -> bool:
    """Has the sweep written its file? After that the emulator is done with."""
    return bool(city) and (data_dir_for(city, root) / "1_sweep.csv").exists()


# --------------------------------------------------------------------------
# Step: Google Maps
# --------------------------------------------------------------------------
LIST_TOKEN = "{list}"

TOS = (
    "pin and notes work by driving Google Maps in a Chrome window, the way "
    "you would by hand. Automating Google Maps is against Google's Terms of "
    "Service, and the account at risk is yours. Running them is your choice: "
    "nothing on this page runs them, and an agent following AGENTS.md will "
    "not run them unless you tell it to."
)


def maps_card(city: str | None, list_default: str | None,
              root: Path | None = None) -> dict:
    """How to create the list, then the exact pin and notes commands."""
    if not city:
        return {"city": None}
    csv_path = rel(data_dir_for(city, root) / "3_venues.csv")
    name = _quote(list_default or f"{city.title()} Bars")

    def cmd(verb: str, limit: bool) -> str:
        text = f'{CLI} {verb} --csv {csv_path} --list "{LIST_TOKEN}"'
        return text + (" --limit 3" if limit else "")

    templates = {
        "pin_trial": cmd("pin", True),
        "pin_rest": cmd("pin", False),
        "notes_trial": cmd("notes", True),
        "notes_rest": cmd("notes", False),
    }
    return {
        "city": city, "csv": csv_path,
        "csv_ready": venues_ready(city, root),
        "list_default": name,
        "templates": templates,
        "commands": {k: v.replace(LIST_TOKEN, name)
                     for k, v in templates.items()},
        "tos": TOS,
        "budget": PIN_BUDGET_PER_DAY,
        "closures": f"{CLI} closures --csv {csv_path}",
    }
