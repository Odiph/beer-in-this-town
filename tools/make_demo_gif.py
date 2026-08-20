"""Render the README demo GIF: Untappd venue data becoming pins on a map.

The GIF shows the journey the tool performs — a venue listing and its check-in
stats on one side, the same venues pinned on Google Maps on the other — rather
than a terminal session. The terminal is how you drive it; the map is the point
of it.

All three source frames are real screenshots, not mockups:

    docs/_untappd_list.png   Untappd venue search results
    docs/_untappd_stats.png  the Venue Stats block for one venue
    docs/_map_raw.png        the resulting Google Maps saved list

Regenerate with:

    python tools/make_demo_gif.py

Source images are cropped before they are committed so that no personal data
is published: no account avatar, and none of the "Loyal Patrons" profile photos
that sit directly beneath the stats block on Untappd.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DOCS = Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "demo.gif"

W, H = 880, 494
BG = (250, 250, 249)
INK = (24, 24, 27)
MUTED = (113, 113, 122)
AMBER = (255, 193, 7)      # untappd yellow
TEAL = (0, 137, 123)       # maps saved-pin teal
CARD = (255, 255, 255)
EDGE = (228, 228, 231)

STEP_1 = "Untappd knows which bars people actually go back to"
STEP_2 = "every venue, with its check-in counts"
STEP_3 = "...on the map you actually navigate with"
CAPTION = "100 venues  ->  one saved list, each pin carrying its stats"


def font(size: int, bold: bool = False):
    names = (["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = font(25, bold=True)
F_BODY = font(17)
F_SMALL = font(15)
F_NOTE = font(16, bold=True)


def fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    scale = min(box_w / img.width, box_h / img.height)
    return img.resize((int(img.width * scale), int(img.height * scale)),
                      Image.LANCZOS)


def card(base: Image.Image, img: Image.Image, x: int, y: int) -> None:
    """Paste with a light border so screenshots read as distinct panels."""
    d = ImageDraw.Draw(base)
    d.rectangle([x - 1, y - 1, x + img.width, y + img.height], outline=EDGE, width=2)
    base.paste(img, (x, y))


def frame(step: int, fade: float = 1.0) -> Image.Image:
    """step 1 = untappd, 2 = stats highlighted, 3 = map, 4 = note callout."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 6], fill=AMBER if step < 3 else TEAL)

    if step <= 2:
        d.text((40, 34), STEP_1, font=F_TITLE, fill=INK)
        d.text((40, 70), STEP_2, font=F_BODY, fill=MUTED)
        listing = fit(Image.open(DOCS / "_untappd_list.png"), 520, 400)
        card(img, listing, 40, 112)

        if step == 2:
            stats = fit(Image.open(DOCS / "_untappd_stats.png"), 400, 240)
            sx, sy = 590, 190
            d.rectangle([sx - 14, sy - 14, sx + stats.width + 14,
                         sy + stats.height + 14], fill=CARD, outline=AMBER, width=3)
            img.paste(stats, (sx, sy))
            d.text((sx - 6, sy + stats.height + 30),
                   "check-ins  |  unique beers  |  this month",
                   font=F_SMALL, fill=MUTED)
    else:
        d.text((40, 34), STEP_3, font=F_TITLE, fill=INK)
        d.text((40, 70), CAPTION, font=F_BODY, fill=MUTED)
        mp = fit(Image.open(DOCS / "_map_raw.png"), 920, 400)
        card(img, mp, (W - mp.width) // 2, 112)

        if step == 4:
            note = "Untappd #1 | 20,259 check-ins | 2,451 unique | 136/month"
            bw = d.textlength(note, font=F_NOTE) + 44
            bx, by = (W - bw) // 2, H - 96
            d.rounded_rectangle([bx, by, bx + bw, by + 46], radius=10,
                                fill=CARD, outline=TEAL, width=3)
            d.text((bx + 22, by + 13), note, font=F_NOTE, fill=INK)
            d.text((bx, by + 56), "written into each place's note",
                   font=F_SMALL, fill=MUTED)

    if fade < 1.0:
        img = Image.blend(Image.new("RGB", (W, H), BG), img, fade)
    return img


def build() -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []

    def hold(step: int, ms: int) -> None:
        frames.append(frame(step))
        durations.append(ms)

    def crossfade(step: int, steps: int = 3) -> None:
        for i in range(1, steps + 1):
            frames.append(frame(step, fade=i / steps))
            durations.append(45)

    hold(1, 1500)
    crossfade(2)
    hold(2, 2400)
    crossfade(3)
    hold(3, 1600)
    crossfade(4)
    hold(4, 3200)

    # Quantise to a shared 128-colour palette: screenshots of maps carry far
    # more colour than a GIF needs, and an unoptimised file is several MB.
    palette = frames[-1].quantize(colors=96, method=Image.MEDIANCUT)
    frames = [f.quantize(colors=96, palette=palette, dither=Image.NONE)
              for f in frames]
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"{OUT} — {len(frames)} frames, {OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    build()
