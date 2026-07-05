#!/usr/bin/env python3
"""Slice a mascot character sheet into individual transparent sprites.

Usage:
    python3 scripts/slice-mascot.py sheet.png [out_dir]

Removes the solid background (flood fill from the corners, so interior
light colors survive), finds each separate drawing, and writes them as
region-01.png, region-02.png, ... in reading order. Rename the ones you
want to happy.png / chill.png / worried.png / panic.png / sleep.png and
drop them in app/web/img/pip/ -- the dashboard and the HAT LCD pick them
up automatically on the next restart (delete them to go back to the
built-in vector Pip).
"""

import os
import sys

from PIL import Image, ImageDraw

THRESHOLD = 48          # how close to the corner color counts as background
MIN_SIZE = 40           # ignore specks smaller than this many pixels across
GAP = 12                # empty pixels that separate two sprites


def remove_background(img):
    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        try:
            ImageDraw.floodfill(img, corner, (0, 0, 0, 0), thresh=THRESHOLD)
        except (ValueError, RecursionError):
            pass
    del draw
    return img


def bands(profile, gap):
    """Split a 1-D occupancy profile into (start, end) runs."""
    runs, start, empty = [], None, 0
    for i, filled in enumerate(profile):
        if filled:
            if start is None:
                start = i
            empty = 0
        elif start is not None:
            empty += 1
            if empty >= gap:
                runs.append((start, i - empty + 1))
                start, empty = None, 0
    if start is not None:
        runs.append((start, len(profile)))
    return runs


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sheet_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "sliced"
    os.makedirs(out_dir, exist_ok=True)

    img = remove_background(Image.open(sheet_path))
    alpha = img.getchannel("A")
    w, h = img.size
    data = alpha.load()

    cols = [any(data[x, y] > 16 for y in range(h)) for x in range(w)]
    count = 0
    for x0, x1 in bands(cols, GAP):
        rows = [
            any(data[x, y] > 16 for x in range(x0, x1)) for y in range(h)
        ]
        for y0, y1 in bands(rows, GAP):
            if x1 - x0 < MIN_SIZE or y1 - y0 < MIN_SIZE:
                continue
            sprite = img.crop((x0, y0, x1, y1))
            sprite = sprite.crop(sprite.getbbox() or (0, 0, 1, 1))
            count += 1
            path = os.path.join(out_dir, f"region-{count:02d}.png")
            sprite.save(path)
            print(f"{path}  ({sprite.width}x{sprite.height})")
    if not count:
        print("No sprites found -- is the background a flat light color?")
    else:
        print(f"\n{count} sprites. Rename the keepers to happy.png, "
              "chill.png, worried.png, panic.png, sleep.png and move them "
              "to app/web/img/pip/")


if __name__ == "__main__":
    main()
