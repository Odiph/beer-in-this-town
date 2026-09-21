"""Commands built from a city or list name are run verbatim by agents.

A name is free text. Nothing in it may reach a shell as syntax, in any of the
three shells the same command is shown for.
"""
import pytest

from beer_in_this_town.config import arg_text, cli_arg

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name, expected", [
    ("Tel Aviv", '"Tel Aviv"'),
    ("תל אביב", '"תל אביב"'),
    ("O'Brien's", "\"O'Brien's\""),
    ('x"; rm -rf ~ #', '"x rm -rf ~ #"'),
    ("$(whoami)", '"(whoami)"'),
    ("a`b`", '"ab"'),
    ("Bars & Pubs | more", '"Bars  Pubs  more"'),
    ("%PATH%", '"PATH"'),
    ("line1\nline2", '"line1line2"'),
])
def test_names_lose_shell_syntax(name, expected):
    assert cli_arg(name) == expected


def test_a_windows_path_keeps_its_backslashes():
    path = r"C:\projects\beer\data\tel-aviv\3_venues.csv"
    assert cli_arg(path) == f'"{path}"'


def test_a_trailing_backslash_cannot_escape_the_closing_quote():
    assert cli_arg("dir\\") == '"dir"'


def test_arg_text_is_the_unquoted_form():
    assert arg_text('  "Tel Aviv"  ') == "Tel Aviv"
