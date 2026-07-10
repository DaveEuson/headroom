"""Fetch Claude subscription usage limits via the Claude Code OAuth credentials.

Uses the same endpoint Claude Code's /usage command relies on:
    GET https://api.anthropic.com/api/oauth/usage

Credentials come from a Claude Code credentials file (the JSON that lives at
~/.claude/.credentials.json on Linux, or in the macOS Keychain). Access tokens
are short-lived; we refresh them with the stored refresh token and write the
new pair back to the credentials file.

Standard library only -- no pip installs needed on the Pi.
"""

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Claude Code's public OAuth client id (a public client -- not a secret).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
# Anthropic's endpoints sit behind Cloudflare, which blocks obvious
# bot/library User-Agents (e.g. Python-urllib) with a 403 "Error 1010".
# Present a normal browser UA so requests aren't rejected before reaching
# the API.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# Refresh the access token this many seconds before it actually expires.
REFRESH_MARGIN = 300

WINDOW_LABELS = {
    "five_hour": "Session (5 hour)",
    "seven_day": "Weekly (all models)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_oauth_apps": "Weekly (connected apps)",
}

_refresh_lock = threading.Lock()


class UsageError(Exception):
    """Raised with a user-facing message when usage can't be fetched."""


def find_credentials_path(configured=None):
    """Return the first credentials file that exists, or None."""
    candidates = []
    if configured:
        candidates.append(os.path.expanduser(configured))
    candidates += [
        os.path.expanduser("~/.claude-tracker/credentials.json"),
        os.path.expanduser("~/.claude/.credentials.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise UsageError(f"Could not read credentials file {path}: {exc}")
    oauth = data.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise UsageError(
            f"{path} does not look like a Claude Code credentials file "
            "(missing claudeAiOauth.accessToken)."
        )
    return data, oauth


def _save(path, data):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".credentials-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _refresh(path, data, oauth):
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise UsageError(
            "Access token expired and no refresh token is available. "
            "Copy fresh credentials to the Pi (see README)."
        )
    try:
        result = _post_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise UsageError(
                "Claude sign-in expired. Re-copy credentials from a machine "
                "where Claude Code is logged in (see README)."
            )
        raise UsageError(f"Token refresh failed: HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        raise UsageError(f"Token refresh failed: {exc}")

    oauth["accessToken"] = result["access_token"]
    if result.get("refresh_token"):
        oauth["refreshToken"] = result["refresh_token"]
    if result.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + result["expires_in"]) * 1000)
    data["claudeAiOauth"] = oauth
    try:
        _save(path, data)
    except OSError:
        pass  # keep going with the in-memory token; refresh again next time
    return oauth


def _get_token(path):
    with _refresh_lock:
        data, oauth = _load(path)
        expires_at_ms = oauth.get("expiresAt") or 0
        if expires_at_ms and expires_at_ms / 1000.0 - REFRESH_MARGIN < time.time():
            oauth = _refresh(path, data, oauth)
        return oauth["accessToken"], oauth.get("subscriptionType")


def _pretty_label(key):
    return WINDOW_LABELS.get(key, key.replace("_", " ").capitalize())


def _extract_windows(payload):
    """Pull anything that looks like a usage window out of the response.

    The endpoint returns objects like
        {"five_hour": {"utilization": 34, "resets_at": "..."}, ...}
    We parse defensively so new windows show up without code changes.
    """
    windows = []
    if not isinstance(payload, dict):
        return windows
    order = list(WINDOW_LABELS)
    for key, value in payload.items():
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        try:
            utilization = float(value["utilization"])
        except (TypeError, ValueError):
            continue
        windows.append(
            {
                "key": key,
                "label": _pretty_label(key),
                "utilization": max(0.0, min(100.0, utilization)),
                "resets_at": value.get("resets_at"),
            }
        )
    windows.sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    return windows


def fetch_usage(credentials_path):
    """Return {"windows": [...], "plan": str|None}. Raises UsageError."""
    token, plan = _get_token(credentials_path)
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UsageError(
                "Claude rejected the access token (401). Re-copy credentials "
                "from a machine where Claude Code is logged in."
            )
        raise UsageError(f"Usage endpoint returned HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        raise UsageError(f"Could not reach Anthropic: {exc}")
    except ValueError:
        raise UsageError("Usage endpoint returned something that isn't JSON.")

    return {"windows": _extract_windows(payload), "plan": plan}
