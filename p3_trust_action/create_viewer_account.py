"""
One-off bootstrap: creates the single, permanent, shared read-only
`viewer` account.

Unlike create_admin_account.py, this isn't solving a chicken-and-egg
problem -- it's just the simplest way to seed one fixed account whose
credentials the frontend's "View Demo" button embeds directly (2026-08-06
design decision, see wardence_frontend.md): a viewer session grants no
write access at all (every /trigger, /promote, /demote, /accounts
endpoint requires require_role("admin", "demo-trigger") or
require_role("admin") specifically -- "viewer" is never in either list),
so there's nothing to protect by keeping these credentials secret. No
TOTP (that's admin-only, per accounts.create_account), no expiry
(permanent is fine here for the same reason the existing permanent
demo-trigger account is fine: it's revocable, not that it's secret).

Usage:
    python3 p3_trust_action/create_viewer_account.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import accounts  # noqa: E402

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

VIEWER_USERNAME = "test"
VIEWER_PASSWORD = "test"

conn = sqlite3.connect(DB_PATH)
accounts.ensure_accounts_tables(conn)

if accounts.get_account(conn, VIEWER_USERNAME) is not None:
    print(f"Account '{VIEWER_USERNAME}' already exists -- refusing to overwrite. "
          f"Use the POST /accounts/{{username}}/password endpoint via a logged-in "
          f"admin session to change its password instead.")
    conn.close()
    sys.exit(1)

accounts.create_account(
    conn, VIEWER_USERNAME, VIEWER_PASSWORD, role="viewer", expires_hours=None, totp_secret=None
)
conn.close()

print("\nShared viewer account created.")
print(f"Username: {VIEWER_USERNAME}")
print(f"Password: {VIEWER_PASSWORD}")
print(
    "\nThese are meant to be embedded directly in the frontend's 'View Demo' "
    "button (auto-fills + submits /login silently) -- not secret, since this "
    "role has no write access to protect."
)
