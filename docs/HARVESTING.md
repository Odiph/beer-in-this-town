# Harvesting venues from the Untappd app's map

How the geographic harvest works, what was measured to establish it, and
which of the obvious approaches are traps. Everything here was measured
against the live app in BlueStacks on 2026-09-21; where a number is a guess
or an extrapolation, it says so.

This document exists because most of the cost of building this was not
writing the code. It was learning which of five plausible-looking surfaces
actually contains the data, and discovering that four of them return numbers
that look like answers and are not.

---

## 1. Why the app at all

`untappd.com` has **no public geographic venue search**. Its `/search`
endpoint matches venue *names* and ignores `lat`, `lng` and `radius`
outright: passing coordinates changes nothing, and the parameters are not
merely defaulted, they are unread. The only geo-parameterised endpoint on the
website is `/nearby/nearby_events_markup`, which is events-only and returned
nothing in every city tested.

So a query like `q="Tel Aviv"` returns venues whose *name* contains "Tel
Aviv". A real corpus built that way was train stations, hotels, a pita place
and a light-rail platform, while missing `Lauter` two streets from the
centre.

The app's map has a real geographic search. This is the only known route to
one.

---

## 2. The model of the surface

Four facts, and almost every confusing observation follows from them:

1. **A search produces a result set.** Typing a city into the map's search
   box, or tapping `Refresh search`, creates one.
2. **The result set is capped at about 60 venues.**
3. **The map draws only the part of the result set inside the viewport.**
4. **Panning re-draws; it does not re-query.**

Consequences worth stating, because each one cost time:

- A pin count falling after a pan is *clipping*, not a smaller city.
- A cell that is panned into but not re-searched shows a slice of its
  parent's result set. It finds nothing new, which looks exactly like a
  saturated cell.
- Zooming into a dense area surfaces venues the wider view never showed --
  not because zoom reveals more, but because a *new search* at that zoom has
  a fresh 60 slots to spend over a smaller area.

---

## 3. Where the data actually is

### The pins are accessible nodes — this is the whole trick

Every marker on the map is a `View` whose `content-desc` is the venue name,
with `bounds` giving its position. **One `uiautomator dump` enumerates the
entire viewport**, with names and positions, at no cost: no scrolling, no
pagination, no tapping, no screenshots, no model.

```
<node class="android.view.View" content-desc="Lauter." bounds="[449,860][503,922]"/>
```

The trailing full stop is not part of the name and breaks every join
downstream if left on.

### The list view is a trap

The map has a list view beside it. In one measured viewport the **list
showed 10 rows while the map held 58 pins**, and the 10 were a subset of the
58. Its counts are inconsistent (8, 28, 10 across viewports) for reasons
never established, and it does not matter, because the pins are strictly
better.

### "Nearby Venues" is a trap

Discover → Nearby Venues is headed **Verified Venues** and is limited to
venues with menu data. At its maximum 75-mile radius around Tel Aviv it
returns **8 venues total**, reaching Haifa and Be'er Sheva -- straining
across an entire country to fill one screen.

### The card carousel does not exist

A bottom card shows the selected venue. It looks swipeable and is not: no
node on the map screen has `scrollable="true"`. A sequence of 26 "cards"
collected by swiping turned out to be the *map panning*, each pan selecting a
different pin.

### The app has no check-in totals

The app's venue screen has `RECENT ACTIVITY` (individual check-ins), menus,
events and the beer list. **No aggregate totals anywhere.** The signed-in
*web* venue page has them.

This forces the architecture: **the app is the geographic census, the web
scrape is the statistics.** They are not interchangeable.

> Web venue stats are login-gated (`Log In to view Venue Stats`), *except* on
> verified venues. A single anonymous test against `Lauter` therefore passes
> and a 28-venue sample then returns nothing.

---

## 4. The cap, and the saturation rule

Measured across seven cities on three continents:

| City | Unique pins | At cap |
|---|---|---|
| Portland | **60** | yes |
| Reykjavík | **60** | yes |
| Berlin | 58 | yes |
| Tel Aviv | 58 | yes |
| Jerusalem | 45 | no |
| Haifa | 37 | no |
| Eilat | 16 | no |

Portland and Reykjavík both landing on exactly 60 makes it the server's
limit. The 58s are almost certainly 60 raw results with two duplicate names
collapsed by the dedup in `pins_in`.

**Reykjavík has 130,000 people and still hits the ceiling.** Subdivision is
the normal path for any real city, not an edge case for dense ones.

### The rule

```
harvest(cell):
    search(cell)                     # Refresh search -- not optional
    pins = dump_pins()               # one uiautomator dump
    if len(pins) >= TRUNCATION_THRESHOLD:  subdivide into 4
    else:                                  accept as complete
```

`RESULT_SET_CAP = 60` is the observed limit. `TRUNCATION_THRESHOLD = 55`
sits **below** it deliberately: pins dedup by name, so a full set of 60 with
six repeats yields 54 unique, and a rule of `>= 60` would call that cell
complete and lose everything behind it. Erring low costs a few extra cells;
erring high loses venues invisibly.

Zero pins is **not** a truncation. An empty area is a real answer about a
real place, and treating it as "subdivide further" recurses forever over
open countryside.

### Saturation was never reached

| depth | cells | venues | still truncated |
|---|---|---|---|
| 0 | 1 | 58 | yes |
| 1 | 5 | 93 | 5 of 5 |
| 2 | 20 | 126 | 11 of 20 |

At depth 2 a cell is roughly 2.3 × 3.4 km and still returns a capped result
set. **Enumeration may not be available through this surface at any
practical depth.** That is not a defect to fix; it is a property to state
honestly in the output.

---

## 5. Filtering beats digging

Untappd's index is not a drinking index. A Tel Aviv viewport spent its slots
on `Ramat Gan National Park`, `Crowne Plaza`, `Expo Tel Aviv` and
`Yad-Eliyahu Arena`. **Only 6 of the 17 categories a Tel Aviv search returns
are drinking places.**

This cannot be fixed after harvesting: **pins carry a name and a position
and nothing else.** The category lives only in the filter panel, so the
filter must be set *before* the search that fills the cap.

Measured, same `"Tel Aviv"` search:

| | pins |
|---|---|
| unfiltered | 58 (parks, hotels, arenas among them) |
| filtered to drinking categories | 55 |
| **new, never shown unfiltered** | **18** |

The filter applies to the **query**, not to an already-capped set -- the cap
refills with drinking venues once the junk is off. Recovered venues included
`Kuli Alma`, `Milk & Honey Distillery`, `Chouffeland`, `Teder.fm`,
`Rosa Parks` and `Wine Point`.

**Cost comparison:** depth-1 subdivision finds 35 new venues for 5× the
work. Filtering finds 18 for one filter setup on one cell. It is the
cheapest coverage available and it compounds with depth rather than
competing.

Matching is on whole comma-separated phrases, not substrings, which is what
lets `Hotel Bar` be kept while `Hotel` is dropped.

---

## 6. The cap selects by popularity

The engine does not return an arbitrary 60. It returns, approximately, the
most-checked-in 60.

Samples drawn at random from each tier's full pool **before any check-in data
was seen**, counts read from venue pages:

| | n | median check-ins | min | ≥200 check-ins |
|---|---|---|---|---|
| **shallow** (one search) | 14 | 582 | **212** | **14/14 = 100%** |
| **deep** (only via subdivision) | 14 | 142 | 29 | 4/14 = 29% |

**A shallow venue beats a deep one in 86% of pairs** (50% = no relationship).

Two of the four deep venues clearing 200 were **a supermarket chain** and **a
highway** -- places people check in beer, not places to drink it. Real deep
yield is **2/14 (14%)**.

### What this means for depth

| | cells | time | usable venues |
|---|---|---|---|
| depth 0 | 1 | ~15 s | ~58 |
| depth 2 | 20 | ~340 s | ~70 |

**20× the work for ~20% more usable venues.** Stopping shallow is a
defensible product decision, not a compromise -- *provided the output says it
stopped shallow*. `SweepResult` carries `truncated_cells` and
`hit_depth_limit` for exactly that.

### What is not understood

Pin order leads with **verified venues** (`Lauter`, `Ursa`, `Berlin
Florentin`, `Schnitt` opened the Tel Aviv result). Beyond that block the
order is unexplained: tested against check-in counts (62% concordant) and
distance from the search centre (38%). Neither is a rule. **Do not truncate
on tail order.**

---

## 7. Coordinates from pins

The map is locally linear, so pin positions convert to degrees with one
regression per axis:

```
lng = 0.00010872 * x_centre + 34.732837
lat = -0.00009222 * y_bottom + 32.154392
```

Longitude residuals ±6 m; latitude carries a constant +11–16 m offset from
using the bottom of `bounds` as the marker tip. At that zoom **1 px ≈ 10.2
m**, so a 900×1324 map area covers roughly 9.2 × 13.5 km.

**The scale is zoom-dependent — re-fit per zoom level.** It can be
self-calibrated without any reference corpus: pan a known number of pixels
and measure how far the pins moved.

**Overlapping markers are drawn displaced.** `Lauter` sits 11 px from
`Schnitt` on the same street and fitted 111 m from its true position, while
every other pin was within 16 m. Treat pin coordinates as good to ~10 m in
open ground and ~100 m in a dense cluster: fine for a radius filter, not for
an address.

---

## 8. Panning

`input swipe` pans the map deterministically -- across 27 pins, displacement
stdev was **2.67 px** horizontally and **0.31 px** vertically. It flings,
though, and the overshoot scales with duration:

| duration | requested | actual | ratio |
|---|---|---|---|
| 400 ms | +300 px | +381 px | 1.27 |
| 900 ms | +300 px | +317 px | 1.06 |
| 1600 ms | +300 px | +286 px | 0.95 |

**Do not trust a constant.** Pan, dump, measure the mean displacement of
pins present in both dumps, and correct. That is closed-loop, costs two
dumps, and self-calibrates across zoom levels and devices.

Two things absorb a swipe and leave the map still, both found the hard way:

- **A marker under the finger.** Dragging a pin pans nothing.
- **The venue card**, which overlays `y 1242–1492` and looks like map. A pan
  starting there drags the card; the map moves by exactly `(0,0)`.

Pan origins are therefore chosen as the point furthest from every pin, above
`CARD_TOP`.

---

## 9. Failure modes, and why the guards exist

**Every one of these returned a plausible number rather than an error.** That
is the defining property of this surface: it almost never fails loudly, so
the guards have to manufacture the loudness.

| Failure | How it presented |
|---|---|
| dead scroll | 48 identical nodes for 14 passes -- read as "finished list" |
| wrong app | 5 nodes from the BlueStacks launcher -- read as a harvest |
| no settle time | fewer pins mid-redraw -- read as "cell complete" |
| truncation tested on a panned map | 56 of 58 -- read as "cell complete" |
| double-tap landing on a marker | 58 pins, 0 new, six zoom levels running |
| pan starting on the venue card | map moved `(0,0)`, twice |
| `DeadPan` quorum too low | a working pan called dead (false positive) |

The guards that resulted:

- `require_map_screen` -- refuses the list view *and* the launcher.
- `ensure_map_screen` -- guards the **navigation**, not just the harvest;
  recovers by relaunching, because pressing Back emptied the app twice.
- `pin_displacement` + `DeadPan` -- needs a quorum of 6 shared pins before
  accusing, and tolerates 3 stalls before aborting (circuit-breaker shape,
  as in `guardrails.py`).
- Progress assertions -- a flat count between passes is a dead scroll, never
  a short list.
- Jittered 5–8 s settles -- a fixed interval is a tell, and this drives a
  real account.

**A count only means something alongside the thing that produced it:** a
scroll that moved, the right app in front, a fresh query for *this* viewport.

---

## 10. Modules

| module | does |
|---|---|
| `app_map.py` | pure functions over a dump: pins, category counts, truncation rule, pixel→latlng, screen guard |
| `app_sweep.py` | recursive subdivision, navigation guards, category filter driver, per-city resume journal |
| `app_categories.py` | which categories are drinking places; plans the taps that set the filter |
| `adb_device.py` | the transport; raises rather than returning empty |

Everything except `adb_device` is a pure function over a dump, so the whole
method is testable without hardware and costs nothing at runtime.

Envelope codes: `overpass_unavailable`, `adb_unavailable`,
`app_screen_unexpected`, `app_pan_failed`. All the new exceptions subclass
`RuntimeError`, so each needs its handler ordered before any
`except RuntimeError`.

---

## 11. Filtered sweeps, measured

**`Refresh search` clears the category filter.** Measured directly: 9
categories checked before pressing it, all 17 after. So a filtered cell must
run the filter pass *instead of* the refresh -- `SHOW RESULTS` is itself a
search of the current viewport.

This was not obvious and cost a whole run. Filtering worked; refreshing
worked; doing both in the obvious order left the sweep unfiltered **from its
first cell**, and the output was plausible enough that only the venue names
gave it away. A count would not have.

| | cells | venues | junk-looking | time |
|---|---|---|---|---|
| **filtered depth-1** | 4 | **84** | **2** | 507 s |
| unfiltered depth-2 | 20 | 126 | 17 | 341 s |

**17 of the filtered venues are absent from the unfiltered depth-2 set** --
`Oak & Ash Taproom`, `Mike's Place`, `Beer Stop`, `Wine Point`, `Mano Vino`,
`Kermeet Bar`, `Hinnawi Wine & More`. Filtering does not merely clean the
list; it reaches venues subdivision alone never surfaces, because the
unfiltered cap spends its slots on parks and hotels instead.

**But filtering is slow**: the filter pass costs roughly 90 s per cell at the
conservative settle times, so per second the unfiltered sweep currently
yields more. The filter pass is mostly waiting, not tapping, so this is a
tuning problem rather than a structural one.

## 12. Open questions

1. **Tune the filter pass.** ~90 s per cell is mostly settle time. The panel
   is local (no network), so its settles can be far shorter than a search's.
2. **City name → which cells.** A `Cell` is currently a hand-written bounding
   box. What decides the box for "Tel Aviv", and the starting zoom?
3. **City name → which cells.** Currently a `Cell` is passed in by hand. What
   decides the bounding box for "Tel Aviv", and the starting zoom?
4. **The coordinate transform** is fitted at one zoom in one city.
5. **Pin ordering** beyond the verified block.
6. **OSM/Overpass is parked** for now; `overpass.py` is built and tested but
   out of the current line of work.
