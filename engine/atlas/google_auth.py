"""Google OAuth (desktop loopback flow) for Gmail + Calendar — all local.

Setup (one-time, by the user):
  1. In Google Cloud Console, create an OAuth client of type "Desktop app".
  2. Download the client JSON and save it to GOOGLE_CREDENTIALS
     (~/Library/Application Support/Atlas/google_credentials.json), or point
     ATLAS_GOOGLE_CREDENTIALS at it.
  3. Hit "Connect Google" in Atlas → a browser opens for consent → token is cached.

Nothing here talks to Google until the user explicitly connects. The google-* libs
are an optional extra; without them everything degrades to a "not installed" status.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import GOOGLE_CREDENTIALS, GOOGLE_TOKEN

log = logging.getLogger("atlas.google")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def libs_installed() -> bool:
    try:
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401

        return True
    except ImportError:
        return False


def _load_credentials():
    """Return valid Credentials or None (refreshing if needed)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not GOOGLE_TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            GOOGLE_TOKEN.write_text(creds.to_json())
        except Exception as e:
            log.warning("token refresh failed: %s", e)
            return None
    return creds if creds and creds.valid else None


def status() -> dict:
    if not libs_installed():
        return {"state": "libs_missing", "detail": "Run: uv sync --extra google"}
    if not GOOGLE_CREDENTIALS.exists():
        return {"state": "no_credentials", "detail": f"Drop a Desktop OAuth client JSON at {GOOGLE_CREDENTIALS}"}
    creds = _load_credentials()
    if creds is None:
        return {"state": "not_connected", "detail": "Click Connect Google to authorize."}
    return {"state": "connected", "detail": "Authorized", "email": _account_email(creds)}


def _account_email(creds) -> str | None:
    try:
        from googleapiclient.discovery import build

        info = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
        return info.get("email")
    except Exception:
        return None


def connect() -> dict:
    """Run the desktop OAuth flow (blocking — call from a threadpool). Opens a browser."""
    if not libs_installed():
        raise RuntimeError("Google libraries not installed. Run: uv sync --extra google")
    if not GOOGLE_CREDENTIALS.exists():
        raise RuntimeError(f"Missing OAuth client JSON at {GOOGLE_CREDENTIALS}")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    GOOGLE_TOKEN.write_text(creds.to_json())
    return status()


def disconnect() -> dict:
    if GOOGLE_TOKEN.exists():
        GOOGLE_TOKEN.unlink()
    return {"state": "not_connected"}


def service(api: str, version: str):
    """Build an authorized Google API client, or raise if not connected."""
    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("Google not connected")
    from googleapiclient.discovery import build

    return build(api, version, credentials=creds, cache_discovery=False)
