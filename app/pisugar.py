"""Read battery state from the PiSugar power manager.

pisugar-server (from pisugar-power-manager) listens on TCP 127.0.0.1:8423 and
answers simple text commands like "get battery". If it isn't installed or the
board isn't present, everything here degrades to None and the dashboard just
hides the battery tile.
"""

import socket

HOST = "127.0.0.1"
PORT = 8423
TIMEOUT = 2.0


def _ask(command):
    """Send one command, return the reply value string, or None on any failure."""
    try:
        with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            sock.sendall((command + "\n").encode("ascii"))
            reply = sock.recv(256).decode("ascii", "replace").strip()
    except OSError:
        return None
    # Replies look like "battery: 84.25". Errors contain "Invalid" or similar.
    if ":" not in reply:
        return None
    name, _, value = reply.partition(":")
    if name.strip() != command.split()[-1]:
        return None
    return value.strip()


def read_battery():
    """Return {"percent": float, "charging": bool|None, "plugged": bool|None}
    or None when no PiSugar is reachable."""
    raw = _ask("get battery")
    if raw is None:
        return None
    try:
        percent = float(raw)
    except ValueError:
        return None

    def as_bool(value):
        if value is None:
            return None
        return value.lower() == "true"

    return {
        "percent": max(0.0, min(100.0, percent)),
        "charging": as_bool(_ask("get battery_charging")),
        "plugged": as_bool(_ask("get battery_power_plugged")),
    }
