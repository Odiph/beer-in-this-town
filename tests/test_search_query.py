"""The search URL has to carry the query the user actually typed.

The browser path built it with an f-string, so a query containing `&` started
a new parameter and one containing `#` turned the rest into a fragment. Both
searched for something shorter than what was asked for, returned results, and
gave no sign anything was wrong -- the failure mode this project keeps calling
out: confidently wrong beats absent, in the wrong direction.

That path is not a corner case. Untappd moved search to Algolia, so the HTTP
path raises `ClientRenderedSearch` and the browser path is what runs.

No network.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from beer_in_this_town.config import SEARCH_URL
from beer_in_this_town.scrape import search_url_for


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "london",
    "new york",
    "rock & roll",          # the & used to start a new parameter
    "café #1",         # the # used to start a fragment
    "st. john's",
    "são paulo",
    "a+b",                  # + is a space once encoded
    "100% brewing",         # a bare % is not a valid escape
])
def test_the_query_survives_the_url(query):
    url = search_url_for(query)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.fragment == "", "part of the query became a fragment"
    assert params["q"] == [query], "the query changed on its way into the URL"
    assert params["type"] == ["venues"]


@pytest.mark.unit
def test_it_is_still_untappds_venue_search():
    url = search_url_for("london")
    assert url.startswith(SEARCH_URL + "?")


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
        Check("google", "Google account", OK, "signed in", verified=True),
        Check("untappd", "Untappd account", OK, "signed in", verified=True),
    )
    step = next_step(rows)

    assert step.key == "run"
    assert step.notes, "the step that asks for a city explains nothing about it"


@pytest.mark.unit
def test_the_notes_describe_mechanics_not_taste():
    """Every line has to be a consequence someone can check in the code.

    Advice about what makes a good night out ages badly and nobody can verify
    it; "this names the diff baseline" is either true or a bug.
    """
    from beer_in_this_town.ui.checks import SEARCH_NOTES

    headings = [h for h, _ in SEARCH_NOTES]
    assert len(headings) == len(set(headings))
    bodies = " ".join(b for _, b in SEARCH_NOTES)
    for claim in ("venue search", "diff", "Bars"):
        assert claim.lower() in bodies.lower() or \
            any(claim.lower() in h.lower() for h in headings), \
            f"the notes never mention {claim}"
