"""
One-time Dhan credential setup.
Run: python setup_dhan_keys.py
"""
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))

from core.secrets import (
    save_client_id, save_access_token,
    save_data_api_key, save_data_api_secret,
    get_client_id, get_access_token, get_data_api_key,
)
from core.api_dhan import DhanAPI, check_token_health


def prompt(msg, secret=False):
    val = input(f"\n{msg}\n> ").strip()
    if not val:
        print("Skipped.")
    return val


print("=" * 55)
print("  Dhan API Key Setup")
print("=" * 55)

# ── Trading API ──────────────────────────────────────────────
print("\n[Trading API]  Dhan app → My Profile → API → Trading API")
cid = prompt("Client ID (e.g. 1234567):")
if cid:
    save_client_id(cid)
    print(f"  Saved client_id: {cid}")

tok = prompt("Access Token (long JWT starting with eyJ...):")
if tok:
    save_access_token(tok)
    print(f"  Saved access_token: {tok[:20]}…")

# ── Data API ─────────────────────────────────────────────────
print("\n[Data API]  Dhan app → My Profile → API → Data APIs")
dk = prompt("Data API Key:")
if dk:
    save_data_api_key(dk)
    print(f"  Saved data_api_key: {dk[:12]}…")

ds = prompt("Data API Secret (leave blank if none):")
if ds:
    save_data_api_secret(ds)
    print(f"  Saved data_api_secret: {ds[:8]}…")

# ── Verify ───────────────────────────────────────────────────
print("\n[Verifying...]")
check_token_health()

api = DhanAPI()
result = api.test_data_api()
if result["ok"]:
    print(f"  Data API OK — {result['message']}")
    print("\n  SUCCESS. Dhan live data active. Dhan is the ONLY data source.")
    print("  Verify bars:  python -c \"from core.api_dhan import dhan_daily; print(dhan_daily('RELIANCE',30).tail())\"")
else:
    print(f"  Data API FAIL — {result['message']}")
    print("  Check: subscription active at dhan.co/api → Data APIs section.")
    print("  NOTE: yfinance was permanently removed — there is NO fallback.")
    print("  Data calls return empty until Dhan works. Fix before trading.")

print()
