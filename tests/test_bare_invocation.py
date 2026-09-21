"""Typing the program's name is the first thing a new user does.

Answering it with an argparse error is the worst first impression a tool can
give when its entire first-run story is a dashboard that explains itself. So
a bare `beertown` opens that dashboard.

The guards are the interesting half. `ui` blocks forever and AGENTS.md tells
an agent not to run it, so the convenience must not become a way to hang one:
`--json` asks for the machine contract, a non-tty means nobody can close a
browser window, and anything that looks like a real request still gets the
answer it asked for.

No network, no browser.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.cli import wants_dashboard


@pytest.mark.unit
@pytest.mark.parametrize("raw", [[], ["-v"], ["--verbose"]])
def test_a_bare_invocation_at_a_terminal_opens_the_dashboard(raw):
    assert wants_dashboard(raw, isatty=True) is True


@pytest.mark.unit
def test_json_never_opens_a_blocking_server():
    """An agent asking for the contract must get an envelope, not a hang."""
    assert wants_dashboard(["--json"], isatty=True) is False
    assert wants_dashboard(["-v", "--json"], isatty=True) is False


@pytest.mark.unit
def test_piped_output_never_opens_a_blocking_server():
    """No tty means automation: a CI step, a subprocess, a captured pipe.

    None of them can close a browser window, and all of them would sit there
    until something killed them.
    """
    assert wants_dashboard([], isatty=False) is False


@pytest.mark.unit
@pytest.mark.parametrize("raw", [["status"], ["run", "--no-upload"], ["ui"]])
def test_a_real_command_is_still_the_command(raw):
    assert wants_dashboard(raw, isatty=True) is False


@pytest.mark.unit
def test_a_mistyped_subcommand_still_gets_its_error():
    """Swallowing a typo into the dashboard would hide it.

    Someone who typed `beertown stats` wants to know they meant `status`, not
    to be handed a browser window and left wondering what happened.
    """
    assert wants_dashboard(["stats"], isatty=True) is False


@pytest.mark.unit
def test_help_is_still_help():
    for flag in ("-h", "--help"):
        assert wants_dashboard([flag], isatty=True) is False


@pytest.mark.unit
def test_main_routes_a_bare_call_to_ui(monkeypatch):
    """The wiring, not just the predicate. A helper nothing calls is a no-op."""
    import beer_in_this_town.cli as cli

    seen = {}
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "cmd_ui",
                        lambda s, port, open_browser, detach=False: seen.update(
                            port=port, open_browser=open_browser, detach=detach)
                        or cli.Envelope(command="ui", ok=True))

    assert cli.main([]) == 0
    assert seen["open_browser"] is True, "the browser was not opened for the user"
    assert seen["detach"] is False, "a person at a terminal wants the foreground server"


@pytest.mark.unit
def test_main_leaves_json_alone(monkeypatch, capsys):
    """The agent path: an envelope with a code, and no server."""
    import beer_in_this_town.cli as cli

    started = []
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "cmd_ui",
                        lambda *a, **kw: started.append(1))

    with pytest.raises(SystemExit):
        cli.main(["--json"])
    assert started == [], "an agent asking for --json was handed a server"
