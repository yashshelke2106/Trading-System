"""
Credential store for Dhan API.

Hierarchy (first hit wins on read):

  1. OS keyring  (Windows Credential Manager via `keyring` package)
  2. dhan_token.txt + DHAN_CLIENT_ID env var  (legacy compat)
  3. config.DHAN_CLIENT_ID / config.DHAN_ACCESS_TOKEN

Writes go to keyring by default. Plaintext disk mirroring is opt-in via
DHAN_ALLOW_PLAINTEXT_TOKEN_MIRROR=1 for legacy scripts that still require it.

client_id is treated as **permanent**: once saved it stays until cleared.
access_token is rotated daily (Dhan tokens expire ~24h). UI lets user paste
a fresh JWT and call save_token() to update both keyring and disk.

Why keyring first:
  - keyring: encrypted store, no plain disk leak.
  - dhan_token.txt: backward compat read path for older environments.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# keyring is optional — fall back to disk if not installed
try:
    import keyring as _keyring  # type: ignore
    _KEYRING_OK = True
except ImportError:
    _keyring = None
    _KEYRING_OK = False

_SERVICE = "trading_system.dhan"
_KEY_CLIENT = "client_id"
_KEY_TOKEN = "access_token"
_ALLOW_PLAINTEXT_TOKEN_MIRROR = (
    os.getenv("DHAN_ALLOW_PLAINTEXT_TOKEN_MIRROR", "").strip().lower()
    in {"1", "true", "yes"}
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE      = os.path.join(_PROJECT_ROOT, "dhan_token.txt")
DATA_TOKEN_FILE = os.path.join(_PROJECT_ROOT, "dhan_data_token.txt")
CLIENT_FILE     = os.path.join(_PROJECT_ROOT, ".dhan_client_id")


# ─────────────────────────────────────────────────────────────────────────────
# Low-level keyring wrappers
# ─────────────────────────────────────────────────────────────────────────────

def _kr_get(key: str) -> Optional[str]:
    if not _KEYRING_OK:
        return None
    try:
        return _keyring.get_password(_SERVICE, key)
    except Exception:
        return None


def _kr_set(key: str, value: str) -> bool:
    if not _KEYRING_OK:
        return False
    try:
        # Delete first — Windows Credential Manager sometimes won't update existing entry
        try:
            _keyring.delete_password(_SERVICE, key)
        except Exception:
            pass
        _keyring.set_password(_SERVICE, key, value)
        return True
    except Exception:
        return False


def _kr_del(key: str) -> bool:
    if not _KEYRING_OK:
        return False
    try:
        _keyring.delete_password(_SERVICE, key)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Client ID — permanent
# ─────────────────────────────────────────────────────────────────────────────

def get_client_id() -> str:
    """Resolve client_id. Keyring → CLIENT_FILE → config fallback."""
    cid = _kr_get(_KEY_CLIENT)
    if cid:
        return cid
    if os.path.exists(CLIENT_FILE):
        try:
            cid = open(CLIENT_FILE).read().strip()
            if cid:
                # opportunistically migrate to keyring
                _kr_set(_KEY_CLIENT, cid)
                return cid
        except Exception:
            pass
    try:
        import config  # local fallback
        return getattr(config, "DHAN_CLIENT_ID", "") or ""
    except Exception:
        return ""


def save_client_id(client_id: str) -> None:
    """Persist permanently. Writes to keyring + .dhan_client_id."""
    client_id = client_id.strip()
    if not client_id:
        raise ValueError("client_id cannot be empty")
    _kr_set(_KEY_CLIENT, client_id)
    try:
        with open(CLIENT_FILE, "w") as f:
            f.write(client_id)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Access token — rotated
# ─────────────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Resolve token. File takes priority if it differs from keyring (user may have rotated it)."""
    file_tok = None
    if os.path.exists(TOKEN_FILE):
        try:
            file_tok = open(TOKEN_FILE).read().strip() or None
        except Exception:
            pass

    kr_tok = _kr_get(_KEY_TOKEN)

    # If file exists and differs from keyring, file is the fresh rotation — sync keyring.
    if file_tok and file_tok != kr_tok:
        _kr_set(_KEY_TOKEN, file_tok)
        return file_tok

    if kr_tok:
        return kr_tok

    if file_tok:
        return file_tok

    try:
        import config
        return getattr(config, "DHAN_ACCESS_TOKEN", "") or ""
    except Exception:
        return ""


def save_access_token(token: str) -> None:
    """Persist new token to keyring and file so both stay in sync."""
    token = token.strip()
    if not token:
        raise ValueError("token cannot be empty")
    _kr_set(_KEY_TOKEN, token)
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(token)
    except Exception:
        pass


def clear_access_token() -> None:
    _kr_del(_KEY_TOKEN)
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Data API credentials — API Key + API Secret (Dhan Data APIs, permanent)
# ─────────────────────────────────────────────────────────────────────────────
# Dhan Data API uses API Key (as Access-Token) + API Secret — NOT a rotating JWT.
# Get from: Dhan app → Account → API → Data APIs section.

_KEY_DATA_TOKEN  = "data_access_token"   # kept for backward compat
_KEY_DATA_APIKEY = "data_api_key"
_KEY_DATA_SECRET = "data_api_secret"

DATA_SECRET_FILE  = os.path.join(_PROJECT_ROOT, ".dhan_data_secret")
DATA_APIKEY_FILE  = os.path.join(_PROJECT_ROOT, ".dhan_data_apikey")


def get_data_api_key() -> str:
    """Dhan Data API Key (permanent credential, used as Access-Token header)."""
    k = _kr_get(_KEY_DATA_APIKEY)
    if k:
        return k
    # Always-write file fallback (same pattern as client_id)
    if os.path.exists(DATA_APIKEY_FILE):
        try:
            val = open(DATA_APIKEY_FILE).read().strip()
            if val:
                _kr_set(_KEY_DATA_APIKEY, val)  # migrate to keyring
                return val
        except Exception:
            pass
    # Legacy: dhan_data_token.txt (non-JWT values treated as API key)
    if os.path.exists(DATA_TOKEN_FILE):
        try:
            val = open(DATA_TOKEN_FILE).read().strip()
            if val and not val.startswith("eyJ"):
                return val
        except Exception:
            pass
    return ""


def get_data_api_secret() -> str:
    """Dhan Data API Secret. Keyring → .dhan_data_secret file fallback."""
    s = _kr_get(_KEY_DATA_SECRET)
    if s:
        return s
    if os.path.exists(DATA_SECRET_FILE):
        try:
            s = open(DATA_SECRET_FILE).read().strip()
            if s:
                _kr_set(_KEY_DATA_SECRET, s)  # migrate to keyring if available
                return s
        except Exception:
            pass
    return ""


def save_data_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("API key cannot be empty")
    _kr_set(_KEY_DATA_APIKEY, key)
    # Always write to .dhan_data_apikey file (keyring-failure fallback)
    try:
        with open(DATA_APIKEY_FILE, "w") as f:
            f.write(key)
    except Exception:
        pass
    if _ALLOW_PLAINTEXT_TOKEN_MIRROR:
        try:
            with open(DATA_TOKEN_FILE, "w") as f:
                f.write(key)
        except Exception:
            pass


def save_data_api_secret(secret: str) -> None:
    """Persist Data API secret. Plaintext file mirroring is opt-in for legacy flows."""
    secret = secret.strip()
    if not secret:
        raise ValueError("API secret cannot be empty")
    _kr_set(_KEY_DATA_SECRET, secret)
    if _ALLOW_PLAINTEXT_TOKEN_MIRROR:
        try:
            with open(DATA_SECRET_FILE, "w") as f:
                f.write(secret)
        except Exception:
            pass


def clear_data_credentials() -> None:
    _kr_del(_KEY_DATA_APIKEY)
    _kr_del(_KEY_DATA_SECRET)
    _kr_del(_KEY_DATA_TOKEN)
    try:
        for path in (DATA_TOKEN_FILE, DATA_SECRET_FILE, DATA_APIKEY_FILE):
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# indianapi.in — REST stock-data API (stock.indianapi.in), single API key
# Auth: header `X-Api-Key: <key>`. Permanent credential (no rotation).
# ─────────────────────────────────────────────────────────────────────────────

_KEY_INDIANAPI = "indianapi_key"
INDIANAPI_KEY_FILE = os.path.join(_PROJECT_ROOT, ".indianapi_key")


def get_indianapi_key() -> str:
    """indianapi.in API key. Keyring → .indianapi_key file → env fallback."""
    k = _kr_get(_KEY_INDIANAPI)
    if k:
        return k
    if os.path.exists(INDIANAPI_KEY_FILE):
        try:
            val = open(INDIANAPI_KEY_FILE).read().strip()
            if val:
                _kr_set(_KEY_INDIANAPI, val)  # migrate to keyring
                return val
        except Exception:
            pass
    return os.getenv("INDIANAPI_KEY", "").strip()


def save_indianapi_key(key: str) -> None:
    """Persist indianapi.in key to keyring + .indianapi_key file."""
    key = key.strip()
    if not key:
        raise ValueError("indianapi key cannot be empty")
    _kr_set(_KEY_INDIANAPI, key)
    try:
        with open(INDIANAPI_KEY_FILE, "w") as f:
            f.write(key)
    except Exception:
        pass


def clear_indianapi_key() -> None:
    _kr_del(_KEY_INDIANAPI)
    try:
        if os.path.exists(INDIANAPI_KEY_FILE):
            os.remove(INDIANAPI_KEY_FILE)
    except Exception:
        pass


def get_data_token() -> str:
    """Resolve token for Dhan Data API session.
    Priority: Data API JWT → trading access token (fallback).
    Bad/short keys (< 30 chars) are rejected so we fall back to trading JWT.
    """
    key = get_data_api_key()
    # Real Dhan Data API tokens are JWTs (~300 chars). Anything shorter is
    # likely an app_id, partial paste, or stale placeholder — fall back.
    if key and len(key) >= 30 and key.startswith("eyJ"):
        return key
    return get_access_token()


def save_data_token(token: str) -> None:
    """Legacy compat — saves as API key."""
    save_data_api_key(token)


def clear_data_token() -> None:
    clear_data_credentials()


def data_token_health() -> "TokenHealth":
    """Health check for data API credentials."""
    key = get_data_api_key()
    if key:
        if key.startswith("eyJ"):
            return token_health(key)  # it's a JWT, decode normally
        return TokenHealth(True, None, None, False,
                           f"API Key set ({key[:8]}…)")
    return TokenHealth(False, None, None, True, "No Data API key set")


# ─────────────────────────────────────────────────────────────────────────────
# Token health (JWT exp decode, no external lib)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TokenHealth:
    valid: bool
    expires_at: Optional[datetime]
    hours_left: Optional[float]
    needs_refresh: bool
    message: str


def token_health(token: Optional[str] = None) -> TokenHealth:
    t = token if token is not None else get_access_token()
    if not t:
        return TokenHealth(False, None, None, True, "No token configured")

    try:
        parts = t.split(".")
        if len(parts) != 3:
            return TokenHealth(False, None, None, True, "Token is not a JWT")
        payload = parts[1] + "=="
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp = decoded.get("exp")
        if not exp:
            return TokenHealth(True, None, None, False, "Token decoded, no exp")
        exp_dt = datetime.utcfromtimestamp(exp)
        delta = exp_dt - datetime.utcnow()
        hrs = delta.total_seconds() / 3600.0
        if hrs <= 0:
            return TokenHealth(False, exp_dt, hrs, True,
                               f"EXPIRED {abs(hrs):.1f}h ago")
        if hrs <= 6:
            return TokenHealth(True, exp_dt, hrs, True,
                               f"Expires in {hrs:.1f}h — refresh soon")
        return TokenHealth(True, exp_dt, hrs, False,
                           f"Valid for {hrs:.1f}h (until {exp_dt:%Y-%m-%d %H:%M} UTC)")
    except Exception as e:
        return TokenHealth(False, None, None, True, f"Decode error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI helper
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"keyring backend available: {_KEYRING_OK}")
    print(f"client_id: {get_client_id() or '(not set)'}")
    h = token_health()
    print(f"token: {h.message}")
