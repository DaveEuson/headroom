#!/usr/bin/env python3
"""Standalone Whisplay LCD bring-up test — isolates backlight from drawing.

The tracker service grabs the same GPIO/SPI, so STOP it first:

    sudo systemctl stop claude-tracker
    python3 scripts/lcd-test.py

Watch the physical screen and read the prompts — it tells you what it's
doing at each step. Ctrl+C to quit early.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import display  # noqa: E402
from PIL import Image  # noqa: E402


def backlight_test():
    from gpiozero import DigitalOutputDevice
    print("\n== Backlight test (raw GPIO%d) ==" % display.BL_PIN)
    bl = DigitalOutputDevice(display.BL_PIN)  # default: on()=HIGH, off()=LOW
    try:
        print("  Pin HIGH for 4s — look for ANY glow/light on the screen...")
        bl.on()
        time.sleep(4)
        print("  Pin LOW for 4s — look again...")
        bl.off()
        time.sleep(4)
    finally:
        bl.close()
    print("  --> Note which one lit the backlight: HIGH, LOW, or NEITHER.")


def color_test():
    print("\n== Panel + color fill test ==")
    try:
        panel = display.ST7789()
    except Exception as exc:
        print("  ST7789 init FAILED: %s: %s" % (type(exc).__name__, exc))
        return
    print("  Panel initialized. Filling solid colors (2.5s each)...")
    colors = [
        ("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)),
        ("BLUE", (0, 0, 255)), ("WHITE", (255, 255, 255)),
        ("ORANGE", (217, 119, 87)),
    ]
    for name, rgb in colors:
        print("   -> %s" % name)
        try:
            panel.show(Image.new("RGB", (display.WIDTH, display.HEIGHT), rgb))
        except Exception as exc:
            print("      show() FAILED: %s: %s" % (type(exc).__name__, exc))
            return
        time.sleep(2.5)
    print("  Done. Did the colors appear? Were they the RIGHT colors?")


if __name__ == "__main__":
    print("Whisplay LCD bring-up test. Make sure the tracker is stopped:")
    print("  sudo systemctl stop claude-tracker")
    backlight_test()
    color_test()
    print("\nTest complete. Tell Claude: which backlight level lit up, and "
          "whether the color fills showed (and if the colors were correct).")
