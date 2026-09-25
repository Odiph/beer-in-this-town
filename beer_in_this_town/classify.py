"""Candidate venue heuristics. Every threshold here is an unvalidated guess.

Nothing in this module is wired into `run`, and that is deliberate. A
classifier merged without measurement produces plausible output, is wrong at an
unknown rate, and nothing raises -- the exact failure `corpus_quality_gate`,
the raising selectors and the `parse_count("136 Monthly")` test all exist to
prevent, reintroduced in a new place.

So this module exists to be *measured*. `measure.py` samples from the buckets
it assigns and scores them against human labels; only once the error rates are
known is it worth deciding whether any of this belongs in the export path.

Three questions, from three issues:

  * what kind of venue is this      (#6) -- from the category text only
  * is this somebody's home         (#8) -- from the unique/total shape
  * has it closed                   (#7) -- from monthly activity

The ordering between them is not arbitrary. A private space wrongly kept puts
someone's front door on a shared map; a real venue wrongly dropped costs one
bar to re-add from a visible list. The costly error is decided first.

What a craft-beer venue is (v0.2 -- the one definition)
-------------------------------------------------------
`filter` and the app's category panel (`app_categories`) both read the
vocabulary below; nothing else in the package decides what a beer venue is.
Categories are matched as whole comma-separated parts, case- and
accent-folded, so `Hotel Bar` is a bar and `Hotel` is not.

  KEEP, specific -- brewery, brewpub, microbrewery, taproom, cidery, meadery;
      beer bar, craft beer bar, beer garden/biergarten, beer hall; bottle
      shop, beer store, liquor store. Wins over everything else on the line:
      a taproom that also serves food is a taproom.
  EXCLUDE -- supermarket, grocery, convenience store, gas station, highway or
      road, winery, vineyard, wine bar, wine shop, cocktail bar, distillery,
      beer festival (an event, not a place). Wins over the generic keeps, so
      `Wine Bar, Bar` is excluded and `Wine Bar, Beer Bar` is kept.
  KEEP, generic -- bar, pub, irish pub, gastropub, dive bar, sports bar, hotel
      bar, lounge. Untappd presence with beer check-ins is the signal; a
      bare `Bar` is where plenty of serious craft venues are filed.
  EXCLUDE, restaurant-only -- every part is food (restaurant, diner, cafe...).
  EXCLUDE, no beer category -- a category line that says none of the above.
  KEEP, uncategorised -- no category at all. That is an unresolved sweep row:
      it already passed the app's drinking filter, and dropping it would
      turn "we could not look it up" into "it is not a bar".

Before any of that, a private-looking venue (`looks_private`) is excluded:
someone's front door on a shared map is the costly error. After it,
`looks_closed` and a Places closed status only *flag* a venue -- a false
closure deletes a real bar, so closing is the user's decision.

`classify()` below is the older measurement-only candidate and keeps its own
three-outcome verdict for `label`/`score`; `craft_beer_decision()` is what
`filter` runs.
"""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .models import Venue

log = logging.getLogger(__name__)


class Kind(StrEnum):
    """What sort of place this is, where the category text says so plainly."""

    BREWERY = "brewery"
    BOTTLE_SHOP = "bottle_shop"
    CRAFT_BEER_BAR = "craft_beer_bar"
    NON_BEER = "non_beer"
    UNSETTLED = "unsettled"


class Verdict(StrEnum):
    KEEP = "keep"
    DROP = "drop"
    REVIEW = "review"


# Matched against the whole category line, most specific first. Only phrases
# that are unambiguous in Untappd's own data belong here.
#
# A bare "Bar" is deliberately absent. Untappd records it for plenty of serious
# craft venues, so a rule mapping bar -> general_bar would silently discard
# exactly what this tool exists to find. Same for "Pub" and "Cocktail Bar".
KIND_PHRASES: tuple[tuple[Kind, tuple[str, ...]], ...] = (
    (Kind.BREWERY, ("brewery", "brewpub", "brew pub", "taproom", "tap room",
                    "cidery", "meadery", "microbrewery")),
    (Kind.BOTTLE_SHOP, ("bottle shop", "bottleshop", "liquor store",
                        "beer store", "off licence", "off-licence",
                        "off license")),
    (Kind.CRAFT_BEER_BAR, ("craft beer", "beer bar", "beer garden",
                           "beer hall", "bottle share")),
    (Kind.NON_BEER, ("cafe", "coffee", "tea house", "bakery", "juice",
                     "convenience store", "grocery", "supermarket")),
)

# --- Unvalidated thresholds. #10 exists to put numbers on these. ----------

# A household is a few people checking in many times. Both halves matter: low
# unique alone is also what a brand-new venue looks like.
PRIVATE_MAX_UNIQUE = 3
PRIVATE_MIN_TOTAL = 20

# A venue with a substantial history and no current activity. Kept off small
# totals, where zero monthly means "new or quiet", not "gone".
CLOSED_MAX_MONTHLY = 0
CLOSED_MIN_TOTAL = 50


@dataclass(frozen=True)
class Classification:
    """One venue's candidate verdict, and why."""

    kind: Kind
    verdict: Verdict
    reason: str

    @property
    def bucket(self) -> str:
        """The stratum `measure.py` samples from.

        Sampling has to happen per *decision*, not per kind: the rare buckets
        are where the costly errors live, and a random sample would barely
        touch them.
        """
        return f"{self.verdict.value}:{self.reason.split(':')[0]}"


def _fold(text: str) -> str:
    """Lowercase and strip diacritics, so accented categories still match."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def venue_kind(category: str | None) -> Kind:
    """Read the kind out of a category line, or decline to.

    The line carries several comma-separated categories in practice
    ("American Restaurant, Beer Bar, Dive Bar, Bar"), so this searches the
    whole line rather than reading the first entry. A beer signal anywhere in
    it outranks a food signal, because a taproom that also serves food is a
    taproom.
    """
    if not category:
        return Kind.UNSETTLED
    folded = _fold(category)
    for kind, phrases in KIND_PHRASES:
        if any(phrase in folded for phrase in phrases):
            return kind
    return Kind.UNSETTLED


def looks_private(v: Venue) -> bool:
    """Does this look like somebody's home rather than a public place?

    The discriminating shape is many check-ins concentrated in very few
    people. Requiring both halves is what keeps a brand-new venue -- low
    unique, low total -- out of the bucket.

    Known to be a candidate generator rather than a decider. The shape has at
    least three causes and this cannot separate them: a home, a hotel room,
    and one regular at a thoroughly public venue. The seed corpus has
    `Hong Lee Coffeeshop` at 2,807 check-ins over 2 unique visitors against a
    median of 4.1 -- a kopitiam, not a flat. #20 proposes the Places lookup
    that tells them apart; until then nothing here should decide alone.
    """
    if v.unique is None or v.total is None:
        return False
    return v.unique <= PRIVATE_MAX_UNIQUE and v.total >= PRIVATE_MIN_TOTAL


STALE_DAYS = 365


def looks_closed(v: Venue, today: date | None = None) -> bool:
    """Substantial history, no current activity.

    With a last check-in date (#21) that is the direct observation: nothing
    in STALE_DAYS reads as gone, anything newer as alive -- which separates
    the quiet-but-open bar and the seasonal one that `monthly == 0` lumps in
    with the dead. Without a date, the older rule: history, but nothing this
    month. Suggestive, never conclusive either way.
    """
    if v.last_checkin:
        try:
            last = date.fromisoformat(v.last_checkin)
        except ValueError:
            last = None
        if last is not None:
            return ((today or date.today()) - last).days > STALE_DAYS
    if v.monthly is None or v.total is None:
        return False
    return v.monthly <= CLOSED_MAX_MONTHLY and v.total >= CLOSED_MIN_TOTAL


def classify(v: Venue) -> Classification:
    """Assign a candidate verdict. Unvalidated -- see this module's docstring."""
    kind = venue_kind(v.ref.category)

    # Order is the asymmetry: a private space wrongly kept is someone's front
    # door on a shared map, so it is decided before anything else can keep it.
    if looks_private(v):
        return Classification(kind, Verdict.DROP,
                              f"private: {v.unique} unique over {v.total} check-ins")
    if looks_closed(v):
        return Classification(kind, Verdict.DROP,
                              f"closed: {v.total} check-ins, none this month")
    if kind is Kind.NON_BEER:
        return Classification(kind, Verdict.DROP,
                              "non_beer: category is not a beer venue")
    if kind is Kind.UNSETTLED:
        # Neither kept nor dropped. #6 is explicit that this is a third
        # outcome with the reason attached, not a quiet rejection.
        return Classification(kind, Verdict.REVIEW,
                              f"unsettled: category {v.ref.category!r} decides nothing")
    return Classification(kind, Verdict.KEEP, f"{kind.value}: category is explicit")


# --- the craft-beer definition (see the module docstring) ----------------

BREWERY_CATEGORIES = frozenset({
    "brewery", "brewpub", "brew pub", "microbrewery", "taproom", "tap room",
    "cidery", "meadery",
})
BOTTLE_SHOP_CATEGORIES = frozenset({
    "bottle shop", "bottleshop", "beer store", "liquor store",
    "off licence", "off-licence", "off license",
})
BEER_BAR_CATEGORIES = frozenset({
    "beer bar", "craft beer bar", "craft beer", "beer garden", "biergarten",
    "beer hall",
})
GENERIC_BAR_CATEGORIES = frozenset({
    "bar", "pub", "irish pub", "gastropub", "dive bar", "sports bar",
    "hotel bar", "lounge",
})
# category part -> the reason written to 3_excluded.csv
EXCLUDED_CATEGORIES: dict[str, str] = {
    "supermarket": "supermarket", "grocery store": "supermarket",
    "grocery": "supermarket", "convenience store": "supermarket",
    "gas station": "gas station", "petrol station": "gas station",
    "highway or road": "highway/road", "highway": "highway/road",
    "road": "highway/road", "street": "highway/road",
    "winery": "winery", "vineyard": "winery",
    "wine bar": "wine bar", "wine shop": "wine bar",
    "cocktail bar": "cocktail bar", "distillery": "distillery",
    "beer festival": "event, not a venue",
}
# Every category the tool wants on the map. `app_categories` toggles exactly
# these on in the app's filter panel, so collection and filtering agree.
KEEP_CATEGORIES = (BREWERY_CATEGORIES | BOTTLE_SHOP_CATEGORIES
                   | BEER_BAR_CATEGORIES | GENERIC_BAR_CATEGORIES)

_FOOD_WORDS = ("restaurant", "diner", "cafe", "coffee", "bakery", "pizza",
               "burger", "steakhouse", "bistro", "sandwich", "food", "grill",
               "eatery", "kitchen", "joint", "noodle", "sushi", "bbq")


def category_parts(category: str | None) -> list[str]:
    """`"American Restaurant, Beer Bar"` -> `["american restaurant", "beer bar"]`."""
    if not category:
        return []
    return [" ".join(_fold(p).split()) for p in category.split(",") if p.strip()]


@dataclass(frozen=True)
class Decision:
    """`filter`'s verdict on one venue. `flag` never removes a venue."""

    keep: bool
    kind: str      # brewery|bottle_shop|beer_bar|bar|uncategorised|excluded
    reason: str
    flag: str = ""  # "" | possibly_closed | closed


def closure_flag(v: Venue) -> str:
    """Closed per Places, or suspected closed per `looks_closed`, or ""."""
    if v.is_closed:
        return "closed"
    if looks_closed(v):
        return "possibly_closed"
    return ""


def craft_beer_decision(v: Venue) -> Decision:
    """Is this a craft-beer venue? The one definition; see the docstring."""
    flag = closure_flag(v)
    if looks_private(v):
        return Decision(False, "excluded",
                        f"private: {v.unique} unique over {v.total} check-ins",
                        flag)
    parts = category_parts(v.ref.category)
    if not parts:
        return Decision(True, "uncategorised",
                        "no category: kept, it passed the app's drinking filter",
                        flag)
    for kind, vocab in (("brewery", BREWERY_CATEGORIES),
                        ("bottle_shop", BOTTLE_SHOP_CATEGORIES),
                        ("beer_bar", BEER_BAR_CATEGORIES)):
        hit = next((p for p in parts if p in vocab), None)
        if hit:
            return Decision(True, kind, f"category: {hit}", flag)
    excluded = next((p for p in parts if p in EXCLUDED_CATEGORIES), None)
    if excluded:
        return Decision(False, "excluded", EXCLUDED_CATEGORIES[excluded], flag)
    generic = next((p for p in parts if p in GENERIC_BAR_CATEGORIES), None)
    if generic:
        return Decision(True, "bar", f"category: {generic}", flag)
    if all(any(w in p for w in _FOOD_WORDS) for p in parts):
        return Decision(False, "excluded", "restaurant-only", flag)
    return Decision(False, "excluded",
                    f"no beer category: {v.ref.category}", flag)
