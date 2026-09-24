"""Which note box on a Google Maps place belongs to the target list.

Found live 2026-09-24: Maps no longer shows a note field on the place page.
The note sits under the "Saved in" row, which starts collapsed, and there is
one box per list the place is saved in -- The Rake showed a box for "London
Bars Test" (private) and one for "London MTP25" (shared, 37 places). `notes`
timed out looking for a field that was folded away, and had it found one it
would have typed into the first box, whichever list that was. A note written
into a shared list publishes the stats to everyone on it.
"""
import pytest

from beer_in_this_town import notes

TEST = "Saved in\nLondon Bars Test\nPrivate · 33 places\nAdd a note"
SHARED = "Saved in\nLondon MTP25\nShared · 37 places\nAdd a note"


@pytest.mark.unit
@pytest.mark.parametrize("block, name", [
    (TEST, "London Bars Test"),
    (SHARED, "London MTP25"),
    ("Saved in London Bars Test Private · 33 places Add a note",
     "London Bars Test"),
    ("Saved in Want to go Private · 1 place", "Want to go"),
    ("Saved in\nBeer & Bars\nPublic · 2 places", "Beer & Bars"),
    ("Add a note", None),
    # Verbatim from the live page, 2026-09-24: innerText joins the label and
    # the list's link with no space, and carries the icon glyphs.
    ("\ue896\n\nSaved inLondon Bars Test\n\nPrivate · 33 places\n\ue5ce"
     "\nAdd a note", "London Bars Test"),
    ("\ue896\n\nSaved inLondon MTP25\n\nShared · 37 places\nAdd a note",
     "London MTP25"),
])
def test_the_list_a_note_box_belongs_to_is_read_from_its_block(block, name):
    assert notes.note_block_list(block) == name


@pytest.mark.unit
@pytest.mark.parametrize("blocks, index", [
    ([TEST, SHARED], 0),
    ([SHARED, TEST], 1),   # order on the page decides nothing
])
def test_the_target_lists_box_is_picked_wherever_it_is(blocks, index):
    assert notes.pick_note_box(blocks, "London Bars Test") == index


@pytest.mark.unit
def test_no_box_for_the_target_list_is_no_box_at_all():
    # Never fall back to "the first box": that is the shared list's.
    assert notes.pick_note_box([SHARED], "London Bars Test") is None
    assert notes.pick_note_box([], "London Bars Test") is None


@pytest.mark.unit
def test_a_list_whose_name_contains_the_target_is_not_the_target():
    london_bars = "Saved in\nLondon Bars\nPrivate · 3 places\nAdd a note"
    assert notes.pick_note_box([london_bars], "London Bars Test") is None
    assert notes.pick_note_box([TEST], "London Bars") is None


@pytest.mark.unit
def test_two_boxes_claiming_the_list_is_ambiguous_not_a_guess():
    assert notes.pick_note_box([TEST, TEST], "London Bars Test") is None


class FakeBox:
    def __init__(self, page, i):
        self.page, self.i = page, i

    def input_value(self, timeout=None):
        return self.page.values[self.i]


class FakeBoxes:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len(self.page.blocks) if self.page.expanded else 0

    def nth(self, i):
        return FakeBox(self.page, i)


class FakeToggle:
    def __init__(self, page):
        self.page = page

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def get_attribute(self, name):
        return "true" if self.page.expanded else "false"

    def click(self, timeout=None):
        self.page.clicks += 1
        self.page.expanded = not self.page.expanded


class FakePage:
    def __init__(self, blocks, values, expanded=False):
        self.blocks, self.values, self.expanded = blocks, values, expanded
        self.clicks = 0
        self.url = "https://www.google.com/maps/place/The+Rake"

    def locator(self, selector):
        if selector == notes.LISTS_TOGGLE:
            return FakeToggle(self)
        return FakeBoxes(self)

    def evaluate(self, script, arg=None):
        return list(self.blocks) if self.expanded else []

    def wait_for_timeout(self, ms):
        pass


@pytest.mark.unit
def test_the_note_is_read_from_the_target_lists_box_after_expanding():
    page = FakePage([SHARED, TEST], ["shared list note", "our note"])
    assert notes._read_note(page, "London Bars Test") == "our note"
    assert page.clicks == 1, "the collapsed Saved-in row was not opened"


@pytest.mark.unit
def test_an_open_row_is_not_toggled_shut():
    page = FakePage([TEST], ["our note"], expanded=True)
    assert notes._read_note(page, "London Bars Test") == "our note"
    assert page.clicks == 0


@pytest.mark.unit
def test_no_box_for_the_list_reads_as_unknown_not_empty():
    # None, not "": an empty note would be "overwrite it"; unknown is "stop".
    page = FakePage([SHARED], ["shared list note"])
    assert notes._read_note(page, "London Bars Test") is None


@pytest.mark.unit
def test_writing_refuses_when_the_list_has_no_box():
    page = FakePage([SHARED], [""])
    with pytest.raises(notes.NoteBoxMissing):
        notes._write_note(page, "71,162 check-ins", "London Bars Test")


@pytest.mark.unit
@pytest.mark.parametrize("existing, action", [
    ("71,162 check-ins", "ok"),       # already there: no write
    ("", "write"),                    # the list's box, empty
    ("old stats", "write"),           # the list's box, stale
    (None, "no-box"),                 # no box for the list: never a write
])
def test_what_to_do_with_a_place_depends_on_its_own_box(existing, action):
    assert notes.note_action(existing, "71,162 check-ins") == action


@pytest.mark.unit
def test_a_place_with_no_note_box_fails_the_run_and_says_why(tmp_path,
                                                             monkeypatch):
    from beer_in_this_town import cli, config

    csv_path = tmp_path / "3_venues.csv"
    csv_path.write_text("name,address,city,total,unique,monthly\n"
                        "The Rake,14 Winchester Walk,London,71162,11112,345\n",
                        encoding="utf-8")
    monkeypatch.setattr(cli, "add_notes", lambda *a, **k: {
        "The Rake | 14 Winchester Walk": "no-note-box"})
    env = cli.cmd_notes(config.Settings(), str(csv_path), "London Bars Test",
                        limit=1, region="London", min_gap=5.0, max_gap=11.0)
    assert env.ok is False
    assert env.data["no_note_box"] == 1 and env.data["written"] == 0
    assert any("note box" in w for w in env.warnings)
