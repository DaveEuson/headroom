"""Out-of-credits / restored notifications: a beep on the Whisplay speaker
and/or a push to your phone (ntfy or Pushover). Stdlib only; every path is
guarded so a missing speaker or network never disrupts the tracker.
"""

import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import wave

MESSAGES = {
    "out": ("Claude session limit reached",
            "You're out of session usage — it will reset soon."),
    "restored": ("Claude usage restored",
                 "Your Claude session has reset — you're good to go."),
    "test": ("ClaudeTracker test alert",
             "Alerts are working — you'll be pinged when you run out or "
             "recover."),
}


def send_test(config):
    """Fire a test alert now and report what happened, so a user can verify
    their setup. Push runs synchronously so we can surface success/failure."""
    result = {}
    if config.get("audio_alerts"):
        try:
            threading.Thread(target=_play_tone, args=("test",),
                             daemon=True).start()
            result["audio"] = "playing"
        except Exception as exc:
            result["audio"] = f"failed: {exc}"
    else:
        result["audio"] = "off"
    service = (config.get("push_service") or "none").lower()
    if service == "none":
        result["push"] = "off"
    else:
        try:
            _send_push(config, "test")
            result["push"] = f"sent via {service}"
        except Exception as exc:
            result["push"] = f"{service} failed: {exc}"
    return result


def alert(config, event):
    """Fire audio + push for 'out'/'restored' in the background (non-blocking)."""
    if event not in MESSAGES:
        return
    threading.Thread(target=_alert, args=(dict(config), event),
                     daemon=True).start()


def _alert(config, event):
    if config.get("audio_alerts"):
        try:
            _play_tone(event)
        except Exception as exc:              # no speaker / no aplay / etc.
            print(f"audio alert skipped: {exc}", file=sys.stderr)
    try:
        _send_push(config, event)
    except Exception as exc:
        print(f"push alert failed: {exc}", file=sys.stderr)


# ---- push (ntfy / Pushover) ------------------------------------------------

def _send_push(config, event):
    service = (config.get("push_service") or "none").lower()
    title, body = MESSAGES[event]
    if service == "ntfy":
        topic = (config.get("ntfy_topic") or "").strip()
        if not topic:
            return
        req = urllib.request.Request(
            "https://ntfy.sh/" + urllib.parse.quote(topic),
            data=body.encode("utf-8"),
            headers={"Title": title,
                     "Priority": "high" if event == "out" else "default",
                     "Tags": "warning" if event == "out" else "white_check_mark"})
        urllib.request.urlopen(req, timeout=10).read()
    elif service == "pushover":
        token = (config.get("pushover_token") or "").strip()
        user = (config.get("pushover_user") or "").strip()
        if not (token and user):
            return
        data = urllib.parse.urlencode({
            "token": token, "user": user, "title": title, "message": body,
            "priority": 1 if event == "out" else 0,
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request("https://api.pushover.net/1/messages.json",
                                   data=data), timeout=10).read()


# ---- audio (a short tone through the Whisplay's WM8960) ---------------------

def _play_tone(event):
    # "out": a descending two-note beep; "restored": ascending.
    seq = [(660, 0.15), (440, 0.28)] if event == "out" \
        else [(440, 0.15), (660, 0.28)]
    path = os.path.join(tempfile.gettempdir(), f"claudetracker_{event}.wav")
    if not os.path.isfile(path):
        _write_tone(path, seq)
    subprocess.run(["aplay", "-q", path], timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_tone(path, notes, rate=22050):
    """Render a little sequence of sine notes to a mono 16-bit WAV."""
    frames = bytearray()
    for freq, dur in notes:
        n = int(rate * dur)
        for i in range(n):
            t = i / rate
            env = min(1.0, t / 0.01, (dur - t) / 0.02)   # fade in/out, no click
            frames += struct.pack("<h", int(32767 * 0.3 * env
                                            * math.sin(2 * math.pi * freq * t)))
        frames += struct.pack("<h", 0) * int(rate * 0.05)  # short gap
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
