"""Render the README demo GIF from a scripted terminal session.

Every line of output here is copied from a real run (see logs/), not invented.
Regenerate with:

    python tools/make_demo_gif.py

Kept in the repo so the demo can be rebuilt when the CLI changes, rather than
being an opaque binary nobody can update.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "demo.gif"

WIDTH, HEIGHT = 900, 520
PAD = 22
LINE_H = 21
FONT_SIZE = 15

BG = (13, 17, 23)          # github dark
CHROME = (22, 27, 34)
PROMPT = (126, 231, 135)   # green
CMD = (230, 237, 243)
DIM = (139, 148, 158)
OK = (86, 211, 100)
WARN = (227, 179, 65)
INFO = (121, 192, 255)
ACCENT = (255, 166, 87)

# (text, colour, delay_frames). A command line is typed out; output appears.
SCRIPT: list[tuple[str, tuple[int, int, int], str]] = [
    ("$ beertown status", CMD, "type"),
    ("", DIM, "out"),
    ("[OK] status", OK, "out"),
    ("  logged_in: True", DIM, "out"),
    ("  pin_progress: 62 saved, 5 failed, 0 not found", DIM, "out"),
    ("", DIM, "out"),
    ("Next:", CMD, "out"),
    ("  beertown notes --list \"Singapore Bars\"", INFO, "out"),
    ("", DIM, "out"),
    ("$ beertown run --query singapore --count 100", CMD, "type"),
    ("", DIM, "out"),
    ("  Search page 1: 20 venues", DIM, "out"),
    ("  offset=25 -> 45 total venues", DIM, "out"),
    ("  offset=50 -> 70 total venues", DIM, "out"),
    ("  offset=75 -> 100 total venues", DIM, "out"),
    ("  [ 42/100] Reddot Brewhouse", DIM, "out"),
    ("  [ 87/100] BunkerBunker", DIM, "out"),
    ("  Parse quality: 100/100 venues with full stats (100%)", OK, "out"),
    ("  Wrote 100 rows  -> data/venues_singapore.csv", OK, "out"),
    ("  Wrote 96 placemarks -> data/venues_singapore.kml", OK, "out"),
    ("", DIM, "out"),
    ("$ beertown pin --list \"Singapore Bars\"", CMD, "type"),
    ("", DIM, "out"),
    ("  Pre-flight OK: signed in, list 'Singapore Bars' exists", OK, "out"),
    ("  [  1/60] Elixir Code", DIM, "out"),
    ("    saved (verified: 'Singapore Bars')", OK, "out"),
    ("  [  2/60] Druggists", DIM, "out"),
    ("    saved (verified: 'Singapore Bars')", OK, "out"),
    ("  [ 16/60] Ziggy Zaggy   ... taking a 90s break", WARN, "out"),
    ("  Done. 62 saved, 5 failed, 0 not found.", OK, "out"),
    ("", DIM, "out"),
    ("$ beertown notes --list \"Singapore Bars\"", CMD, "type"),
    ("", DIM, "out"),
    ("    note written: Untappd #45 | 2,628 check-ins |", ACCENT, "out"),
    ("                  807 unique | 10/month | as of 2026-08-20", ACCENT, "out"),
    ("", DIM, "out"),
    ("  every write verified - rate-limited - resumable", DIM, "out"),
]

TITLE = "beer in this town"


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_frame(lines: list[tuple[str, tuple[int, int, int]]],
               partial: str | None, font, title_font) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    # window chrome
    d.rectangle([0, 0, WIDTH, 34], fill=CHROME)
    for i, colour in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([PAD + i * 20, 12, PAD + i * 20 + 11, 23], fill=colour)
    d.text((WIDTH // 2 - 60, 9), TITLE, font=title_font, fill=DIM)

    y = 48
    visible = lines[-(HEIGHT - 70) // LINE_H:]
    for text, colour in visible:
        if text.startswith("$ "):
            d.text((PAD, y), "$", font=font, fill=PROMPT)
            d.text((PAD + 12, y), text[2:], font=font, fill=colour)
        else:
            d.text((PAD, y), text, font=font, fill=colour)
        y += LINE_H

    if partial is not None:
        d.text((PAD, y), "$", font=font, fill=PROMPT)
        d.text((PAD + 12, y), partial, font=font, fill=CMD)
        w = d.textlength(partial, font=font)
        d.rectangle([PAD + 14 + w, y + 2, PAD + 22 + w, y + LINE_H - 3],
                    fill=CMD)
    return img


def build() -> None:
    font, title_font = load_font(FONT_SIZE), load_font(13)
    frames: list[Image.Image] = []
    durations: list[int] = []
    shown: list[tuple[str, tuple[int, int, int]]] = []

    for text, colour, mode in SCRIPT:
        if mode == "type":
            body = text[2:]
            for i in range(0, len(body) + 1, 2):  # 2 chars per frame
                frames.append(draw_frame(shown, body[:i], font, title_font))
                durations.append(35)
            shown.append((text, colour))
            frames.append(draw_frame(shown, None, font, title_font))
            durations.append(420)  # beat before output
        else:
            shown.append((text, colour))
            frames.append(draw_frame(shown, None, font, title_font))
            durations.append(90 if text.strip() else 40)

    frames[-1] = draw_frame(shown, None, font, title_font)
    durations[-1] = 2600  # hold the last frame

    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        OUT, save_all=True, append_images=frames[1:], duration=durations,
        loop=0, optimize=True,
    )
    kb = OUT.stat().st_size / 1024
    print(f"{OUT} — {len(frames)} frames, {kb:.0f} KB")


if __name__ == "__main__":
    build()
