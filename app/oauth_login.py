"""Phone-friendly Claude sign-in (OAuth 2.0 authorization code + PKCE).

Implements the same flow Claude Code uses, in the "paste the code" variant,
so a user can sign in from any device (their phone) and finish setup with no
computer and no CLI. Standard library only.

Flow:
  1. start_login() -> (authorize_url, verifier). Show the URL as a button.
  2. User opens it, signs in at claude.ai, approves, and is shown a code
     formatted as "<code>#<state>". They copy it.
  3. exchange_code(pasted, verifier) -> oauth dict with access/refresh tokens.
  4. save_credentials(oauth, path) writes the Claude Code credentials format
     that anthropic_usage.py already reads.
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"


class LoginError(Exception):
    """Raised with a user-facing message when sign-in can't complete."""


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start_login():
    """Return (authorize_url, verifier). Keep the verifier for exchange_code."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params), verifier


def exchange_code(pasted, verifier):
    """Exchange the pasted 'code#state' for tokens; return the oauth dict."""
    text = (pasted or "").strip()
    if not text:
        raise LoginError("No code was entered.")
    code, _, state = text.partition("#")
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code.strip(),
        "state": state.strip() or verifier,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
    # RFC 6749 token requests are form-encoded (this is what Claude Code sends).
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
        except Exception:
            pass
        print(f"oauth token exchange failed: HTTP {exc.code} {detail}",
              file=sys.stderr)
        if exc.code in (400, 401, 403):
            msg = ("That code didn't work. Sign-in codes are single-use and "
                   "expire fast — tap 'Sign in with Claude' again for a fresh "
                   "one, approve, and paste it right away.")
            if detail:
                msg += f"  (Anthropic said: {detail})"
            raise LoginError(msg)
        raise LoginError(f"Sign-in failed: HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        raise LoginError(f"Couldn't reach Anthropic to finish sign-in: {exc}")
    except ValueError:
        raise LoginError("Anthropic returned an unexpected response.")

    if not result.get("access_token"):
        raise LoginError("Sign-in response was missing the access token.")

    account = result.get("account") or {}
    return {
        "accessToken": result["access_token"],
        "refreshToken": result.get("refresh_token"),
        "expiresAt": int((time.time() + result.get("expires_in", 3600)) * 1000),
        "scopes": SCOPES.split(),
        "subscriptionType": (
            result.get("subscription_type")
            or account.get("subscription_type")
        ),
    }


def save_credentials(oauth, path):
    """Write {"claudeAiOauth": ...} to path with private (0600) permissions."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"claudeAiOauth": oauth}, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
