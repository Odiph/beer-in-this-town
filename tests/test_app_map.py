"""Reading the Untappd app's map screen out of a `uiautomator` dump.

Measured 2026-09-21 against the real app in BlueStacks. Three facts shape
this module, and each one was expensive to learn:

1. **The pins are accessible nodes.** Every marker is a `View` whose
   `content-desc` is the venue name, with `bounds` giving its position. The
   *list* view beside it is a truncated subset -- 10 rows against 58 pins in
   the same viewport -- so the pins are the data and the list is a trap.
2. **A search returns a result set; the map draws the part of it inside the
   viewport.** Panning re-draws, it does not re-query. So a pin count falling
   after a pan means clipping, not a smaller city.
3. **The result set is capped at 60**, measured across seven cities on three
   continents: Portland 60, Reykjavik 60, Berlin 58, Tel Aviv 58, Jerusalem
   45, Haifa 37, Eilat 16. A cell near the cap is truncated and must be
   subdivided; one well under it is probably complete. Reykjavik has 130k
   people and still hits it, so subdivision is the normal path.

Everything here is a pure function over a dump, so it is testable without a
device and costs no tokens at runtime -- which is the point. What made this
expensive by hand was a model reading screenshots; every judgement that
needed is an assertion below.

No device, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_map import (
    RESULT_SET_CAP,
    TRUNCATION_THRESHOLD,
    WrongScreen,
    categories_in,
    fit_transform,
    is_truncated,
    pin_displacement,
    pins_in,
    require_map_screen,
)

# A cut-down but faithful dump of Untappd 5.0.3's map (captured 2026-11-14):
# the real chrome nodes, three pins, the venue card. Bounds and descriptions
# are verbatim in shape. 5.x pins carry no trailing full stop; one here does,
# because 4.x pins did and the parser still has to cope with either.
MAP_XML = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy>
 <node class="android.widget.Button" content-desc="Haifa, Israel" bounds="[18,36][429,102]"/>
 <node class="android.widget.Button" content-desc="Filter" bounds="[738,36][804,102]"/>
 <node class="android.widget.Button" content-desc="Switch to list view" bounds="[816,36][882,102]"/>
 <node class="android.widget.Button" content-desc="Places" bounds="[0,117][450,186]"/>
 <node class="android.widget.Button" content-desc="Producers" bounds="[450,117][900,186]"/>
 <node class="android.view.View" content-desc="Google Map" bounds="[0,189][900,1600]"/>
 <node class="android.view.View" content-desc="Lauter" bounds="[449,860][503,922]"/>
 <node class="android.view.View" content-desc="Schnitt Brewing Company." bounds="[445,849][499,911]"/>
 <node class="android.view.View" content-desc="Ursa - אורסה" bounds="[294,994][348,1056]"/>
 <node class="android.widget.Button" content-desc="Reset location" bounds="[822,201][888,267]"/>
 <node class="android.view.ViewGroup" content-desc="Lauter, 10 HaArba'a Street Tel Aviv, Category: Bar, Beer Store" bounds="[24,1258][876,1409]"/>
 <node class="android.widget.Button" content-desc="View Menu" bounds="[42,1422][858,1471]"/>
 <node class="android.widget.Button" content-desc="Back to Discover" bounds="[322,1504][579,1588]"/>
</hierarchy>"""

# The 5.x category list: `"Bar, 21"`, no trailing comma.
FILTER_XML = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy>
 <node class="android.widget.Button" content-desc="Deselect All" bounds="[36,207][870,240]"/>
 <node class="android.widget.CheckBox" content-desc="Beer Bar, 2" checked="false" bounds="[18,252][882,327]"/>
 <node class="android.widget.CheckBox" content-desc="Bar, 6" checked="true" bounds="[18,327][882,402]"/>
 <node class="android.widget.CheckBox" content-desc="Hotel, 4" checked="false" bounds="[18,402][882,477]"/>
 <node class="android.widget.CheckBox" content-desc="Israeli Restaurant" checked="false" bounds="[18,1527][882,1600]"/>
</hierarchy>"""


# --- reading pins ---------------------------------------------------------

def test_pins_are_read_with_their_positions():
    pins = pins_in(MAP_XML)
    assert [p.name for p in pins] == ["Lauter", "Schnitt Brewing Company", "Ursa - אורסה"]
    lauter = pins[0]
    assert lauter.x == 476  # centre of [449..503]
    assert lauter.y == 922  # the marker tip is the BOTTOM of the bounds


def test_the_trailing_full_stop_is_stripped():
    """Every pin's content-desc ends in a period. It is not part of the name
    and would break every join downstream."""
    assert all(not p.name.endswith(".") for p in pins_in(MAP_XML))


def test_map_chrome_is_not_a_venue():
    """`Google Map`, `Reset location`, `Places` and friends are all nodes with
    a content-desc. Without an exclusion list the sweep harvests the toolbar."""
    names = {p.name for p in pins_in(MAP_XML)}
    for chrome in ("Google Map", "Reset location", "Places", "Producers",
                   "Filter", "Switch to list view", "View Menu",
                   "Back to Discover", "Haifa, Israel"):
        assert chrome not in names


def test_the_bottom_card_is_not_a_pin():
    """The selected venue also appears as a card with the same name plus its
    address. Counting it doubles one arbitrary venue per dump."""
    assert sum(1 for p in pins_in(MAP_XML) if p.name == "Lauter") == 1


def test_duplicate_pins_are_collapsed():
    xml = MAP_XML.replace(
        '<node class="android.view.View" content-desc="Lauter." bounds="[449,860][503,922]"/>',
        '<node class="android.view.View" content-desc="Lauter." bounds="[449,860][503,922]"/>'
        '<node class="android.view.View" content-desc="Lauter." bounds="[449,860][503,922]"/>')
    assert [p.name for p in pins_in(xml)].count("Lauter") == 1


# --- the truncation rule --------------------------------------------------

def test_a_cell_at_the_cap_is_truncated():
    """Measured across seven cities: Portland 60, Reykjavik 60, Berlin 58,
    Tel Aviv 58, Jerusalem 45, Haifa 37, Eilat 16. At the cap, assume there
    is more and subdivide."""
    assert is_truncated(RESULT_SET_CAP) is True
    assert is_truncated(RESULT_SET_CAP + 5) is True
    assert is_truncated(58) is True
    assert is_truncated(45) is False   # Jerusalem, genuinely complete
    assert is_truncated(37) is False   # Haifa
    assert is_truncated(16) is False   # Eilat


def test_the_threshold_sits_below_the_cap_to_survive_dedup():
    """Pins are deduplicated by name, so a full result set of 60 with six
    repeats yields 54 unique. A rule of `>= cap` would call that complete and
    lose everything behind it."""
    assert TRUNCATION_THRESHOLD < RESULT_SET_CAP
    assert is_truncated(RESULT_SET_CAP - 4) is True


def test_the_cap_is_not_a_guess_about_emptiness():
    """Zero pins is a real answer about a real place, not a truncation."""
    assert is_truncated(0) is False


# --- categories -----------------------------------------------------------

def test_category_counts_are_read_with_their_checked_state():
    cats = categories_in(FILTER_XML)
    assert cats["Bar"].count == 6
    assert cats["Bar"].checked is True
    assert cats["Hotel"].checked is False


@pytest.mark.parametrize("desc", ["Beer Bar, 2", "Beer Bar, 2, "])
def test_a_category_row_parses_with_or_without_the_trailing_comma(desc):
    """Untappd 5.x dropped the trailing comma 4.x printed (`"Beer Bar, 2, "`).
    A parser that required it read every 5.x row as count-less, so the filter
    could not tell a populated category from a clipped one."""
    xml = FILTER_XML.replace('content-desc="Beer Bar, 2"', f'content-desc="{desc}"')
    cat = categories_in(xml)["Beer Bar"]
    assert cat.count == 2


def test_a_compound_category_name_keeps_its_commas():
    """A row such as `"Bar, Diner, 3"` is one category with a comma in its
    name; only the final number is the count."""
    xml = FILTER_XML.replace('content-desc="Hotel, 4"', 'content-desc="Bar, Diner, 3"')
    cats = categories_in(xml)
    assert cats["Bar, Diner"].count == 3


def test_a_category_with_no_count_is_still_reported():
    """The last row is clipped by the screen edge and its count never renders.
    Dropping it would silently shrink the census; `None` says 'unknown'."""
    assert categories_in(FILTER_XML)["Israeli Restaurant"].count is None


# --- geometry -------------------------------------------------------------

def test_transform_recovers_coordinates_from_pin_positions():
    """Fitted on the live app to +-6 m in longitude. The map is locally
    linear, so two axes of simple regression are enough.

    Fitted on two well-separated venues. `Lauter` is deliberately excluded --
    see the next test for why."""
    known = [("Schnitt Brewing Company", 472, 911, 32.07033, 34.78411),
             ("Ursa - אורסה", 321, 1056, 32.05690, 34.76777)]
    t = fit_transform([(x, y, lat, lng) for _, x, y, lat, lng in known])
    for _, x, y, lat, lng in known:
        got_lat, got_lng = t.to_latlng(x, y)
        assert abs(got_lng - lng) < 0.0005
        assert abs(got_lat - lat) < 0.0005


def test_overlapping_markers_are_drawn_displaced():
    """Measured on the live app: `Lauter` sits 11 px from `Schnitt` on the
    same street, and the map nudges coincident markers apart so both stay
    clickable. Its fitted position came out 111 m from its real one while
    every other pin was within 16 m.

    So pin coordinates are good to roughly 10 m in open ground and can be
    100 m out in a dense cluster. Fine for a radius filter; not an address.
    """
    t = fit_transform([(472, 911, 32.07033, 34.78411),
                       (321, 1056, 32.05690, 34.76777)])
    lat, _lng = t.to_latlng(476, 922)  # Lauter, as drawn
    assert abs(lat - 32.07037) > 0.0003  # ~33 m out, and really ~111 m


def test_transform_needs_at_least_two_points():
    with pytest.raises(ValueError):
        fit_transform([(476, 922, 32.07, 34.78)])


def test_displacement_is_measured_from_pins_present_in_both_dumps():
    """Panning is deterministic to ~3 px but flings: a 400 ms swipe of 300 px
    moved 381. So the sweep pans, measures, and corrects rather than trusting
    a constant."""
    before = pins_in(MAP_XML)
    after = pins_in(MAP_XML.replace("[449,860][503,922]", "[149,860][203,922]")
                           .replace("[445,849][499,911]", "[145,849][199,911]")
                           .replace("[294,994][348,1056]", "[-6,994][48,1056]"))
    dx, dy, n = pin_displacement(before, after)
    assert n == 3
    assert dx == pytest.approx(-300.0)
    assert dy == pytest.approx(0.0)


def test_displacement_reports_nothing_when_no_pin_survives_the_pan():
    """A pan far enough to share no pins cannot be measured. Returning 0
    would read as 'the map did not move', which is the opposite of true."""
    dx, dy, n = pin_displacement(pins_in(MAP_XML), [])
    assert n == 0
    assert (dx, dy) == (None, None)


# --- the screen guard -----------------------------------------------------

def test_a_dump_of_the_wrong_screen_is_refused():
    """A harvest of the BlueStacks launcher returned five nodes and reported
    them as data. The guard is what makes that loud."""
    with pytest.raises(WrongScreen):
        require_map_screen("<?xml version='1.0'?><hierarchy>"
                           "<node class='android.widget.TextView'"
                           " text='POPULAR GAMES TO PLAY' bounds='[0,0][9,9]'/>"
                           "</hierarchy>")


def test_the_real_map_screen_passes_the_guard():
    require_map_screen(MAP_XML)


def test_the_list_view_is_not_the_map_view():
    """Both belong to the app and both carry venues. Harvesting the list
    believing it is the map is how you get 10 venues and call it 58."""
    list_xml = MAP_XML.replace('content-desc="Switch to list view"',
                               'content-desc="Switch to map view"')
    list_xml = list_xml.replace('content-desc="Google Map"', 'content-desc="unused"')
    with pytest.raises(WrongScreen):
        require_map_screen(list_xml)
