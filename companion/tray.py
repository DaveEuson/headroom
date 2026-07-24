#!/usr/bin/env python3
"""Headroom tray — the companion as a menubar / system-tray app.

Sits quietly in your menubar (macOS) or system tray (Windows/Linux): it finds
your Headroom board, feeds it your Claude usage, and gives you one-click
**Pair** (make the board self-contained) and **Open board page**. The icon's
colour tells you at a glance whether it's feeding (green), searching (amber), or
stuck (red).

All the Claude-usage logic is reused from companion.py — this file is just the
tray shell.

Run from source:
    pip install pystray pillow certifi        # + pyobjc-framework-Cocoa on macOS
    python tray.py
"""

import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import companion  # noqa: E402  (reuse discover/pair/feed logic)

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    sys.stderr.write(
        "The tray app needs two small libraries. Install them and re-run:\n"
        "  pip install pystray pillow certifi"
        + ("  pyobjc-framework-Cocoa\n" if sys.platform == "darwin" else "\n"))
    sys.exit(1)

INTERVAL = 120  # seconds between feeds

# Shared state. Python's GIL makes these simple dict updates safe enough here.
# url may be pinned up front via the HEADROOM_PI env var or a saved config, so
# a fussy network (VPNs, work laptops) doesn't have to be auto-discovered.
state = {"color": "amber", "status": "Starting…", "url": None,
         "feeding": True, "fixed": False}


def initial_url():
    u = os.environ.get("HEADROOM_PI", "").strip()
    if not u:
        try:
            u = (companion.load_config().get("pi") or "").split(",")[0].strip()
        except Exception:  # noqa: BLE001
            u = ""
    return u.rstrip("/") or None

COLORS = {"green": (94, 170, 100), "amber": (230, 164, 23),
          "red": (221, 77, 77), "grey": (140, 140, 140)}


def make_icon(color):
    """A little gauge glyph tinted by state."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, 58, 58], radius=16, fill=(38, 38, 36, 255))
    c = COLORS.get(color, COLORS["grey"])
    d.ellipse([17, 17, 47, 47], outline=c, width=5)
    d.ellipse([28, 28, 36, 36], fill=c)
    return img


def feed_once(url):
    """One poll+push. Returns (color, status text)."""
    try:
        live = companion.get_live_windows()
    except companion.LiveUnavailable:
        return "amber", "Usage temporarily unreadable"
    if not live:
        return "red", "No Claude login on this computer"
    windows, plan = live
    payload = {"windows": windows, "plan": plan, "source": "live"}
    try:
        res = companion.push(url, "", payload)
    except Exception:  # noqa: BLE001 - any network error means "board unreachable"
        return "red", "Can't reach the board"
    if res.get("ok"):
        summary = ", ".join(f"{w['label'].split(' (')[0]} {w['utilization']:.0f}%"
                            for w in windows[:3])
        return "green", "Feeding · " + summary
    return "amber", "Board rejected: " + str(res.get("error"))


def refresh(icon):
    icon.icon = make_icon(state["color"])
    icon.update_menu()


def worker(icon):
    while True:
        if not state["feeding"]:
            state.update(color="grey", status="Paused")
            refresh(icon)
            time.sleep(2)
            continue
        if not state["url"]:
            state.update(color="amber", status="Looking for your board…")
            refresh(icon)
            state["url"] = companion.discover_pi()
            if not state["url"]:
                state.update(color="red", status="No board found on this network")
                refresh(icon)
                time.sleep(15)
                continue
        color, status = feed_once(state["url"])
        if color == "green":
            companion.save_pi(state["url"])            # remember it for next time
        elif color == "red" and "reach" in status and not state["fixed"]:
            state["url"] = None                        # lost it -> rediscover
        state.update(color=color, status=status)
        refresh(icon)
        time.sleep(INTERVAL)


# ---------------------------------------------------------------- menu actions

def do_pair(icon, item):
    def run():
        url = state["url"] or companion.discover_pi()
        if not url:
            state["status"] = "Pair failed: board not found"
        else:
            ok = companion.pair_device(url)
            state["status"] = ("Paired — the board runs on its own now"
                               if ok else "Pair failed (is a Claude login here?)")
        icon.update_menu()
    threading.Thread(target=run, daemon=True).start()


def do_open(icon, item):
    if state["url"]:
        webbrowser.open(state["url"])


def toggle_feeding(icon, item):
    state["feeding"] = not state["feeding"]


def build_menu():
    return pystray.Menu(
        pystray.MenuItem(lambda *a: state["status"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Feeding", toggle_feeding,
                         checked=lambda item: state["feeding"]),
        pystray.MenuItem("Pair board (run without this computer)", do_pair),
        pystray.MenuItem("Open board page", do_open,
                         enabled=lambda item: bool(state["url"])),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: icon.stop()),
    )


def main():
    pinned = initial_url()
    if pinned:
        state["url"] = pinned
        state["fixed"] = True
    icon = pystray.Icon("Headroom", make_icon("amber"), "Headroom", build_menu())
    threading.Thread(target=worker, args=(icon,), daemon=True).start()
    icon.run()   # blocks on the main thread (required on macOS)


if __name__ == "__main__":
    main()
