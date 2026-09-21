"""What the wizard's city step tells a person. No network.
"""
from __future__ import annotations

import pytest

# --- what the wizard tells a person about the query ----------------------

@pytest.mark.unit
def test_the_city_step_explains_what_the_query_decides():
    """The query picks the venues, the diff baseline and the Maps list name.

    A person typing into that box can see none of that, and two of the three
    only bite later -- a fresh baseline reports every venue as new, and a
    derived list name is what `pin` will look for.
    """
    from beer_in_this_town.ui.checks import OK, Check, next_step

    # Built rather than detected: keying this off the real machine made it
    # skip everywhere, including CI, which is not a test.
    rows = (
        Check("chrome", "Chrome", OK, "Version 151", verified=True),
        Check("playwright", "Browser automation", OK, "ready", verified=True),
        Check("emulator", "Android emulator", OK, "ready", verified=True),
        Check("google", "Google account", OK, "signed in", verified=True),
        Check("untappd", "Untappd account", OK, "signed in", verified=True),
    )
    step = next_step(rows)

    assert step.key == "city"
    assert step.notes, "the step that asks for a city explains nothing about it"


@pytest.mark.unit
def test_the_notes_describe_mechanics_not_taste():
    """Every line has to be a consequence someone can check in the code.

    Advice about what makes a good night out ages badly and nobody can verify
    it; "this names the diff baseline" is either true or a bug.
    """
    from beer_in_this_town.ui.checks import CITY_NOTES

    headings = [h for h, _ in CITY_NOTES]
    assert len(headings) == len(set(headings))
    bodies = " ".join(b for _, b in CITY_NOTES)
    # v0.2: the city names the data folder, centres the sweep, and suggests
    # the list name. Web search and the diff baseline are gone.
    for claim in ("data/", "sweep", "Bars"):
        assert claim.lower() in bodies.lower() or \
            any(claim.lower() in h.lower() for h in headings), \
            f"the notes never mention {claim}"
