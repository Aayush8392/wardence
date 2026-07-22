"""
One-off bootstrap: creates the FIRST admin account directly in the DB.

Chicken-and-egg problem this solves: every other admin account-management
endpoint (POST /accounts, etc.) requires an admin JWT to call -- but there
is no admin account yet on a fresh DB to get that JWT from. This script
inserts the first one directly, locally, the same way mint_token.py used
to hand you your very first admin bearer token.

After this runs once, use the real /login flow (username + password +
TOTP code) to authenticate as admin going forward -- this script is not
meant to be run repeatedly for the same account (re-running with the
same username will fail with "already exists"; use the /accounts/
{username}/password endpoint via a logged-in admin session to rotate the
password instead).

Usage:
    python3 p3_trust_action/create_admin_account.py <username>
    (you'll be prompted for a password)
"""

import getpass
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import accounts  # noqa: E402

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

if len(sys.argv) != 2:
    print("Usage: python3 create_admin_account.py <username>")
    sys.exit(1)

username = sys.argv[1]
password = getpass.getpass("Password for the new admin account: ")
password_confirm = getpass.getpass("Confirm password: ")
if password != password_confirm:
    print("Passwords did not match.")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
accounts.ensure_accounts_tables(conn)

if accounts.get_account(conn, username) is not None:
    print(f"Account '{username}' already exists -- refusing to overwrite. "
          f"Use the /accounts/{{username}}/password endpoint via a logged-in "
          f"admin session to change its password instead.")
    conn.close()
    sys.exit(1)

totp_secret = accounts.generate_totp_secret()
accounts.create_account(
    conn, username, password, role="admin", expires_hours=None, totp_secret=totp_secret
)
conn.close()

provisioning_uri = accounts.totp_provisioning_uri(username, totp_secret)

print("\nAdmin account created.")
print(f"Username: {username}")
print(f"\nTOTP secret (save this now -- shown only once): {totp_secret}")
print(
    "\nMost authenticator apps (Google Authenticator, Authy, etc.) let you add this "
    "account either by scanning a QR code or by manually typing the secret above --"
    " manual entry is simplest here since this is a terminal, not a browser. If you'd "
    "rather scan a QR code, paste the URI below into any QR code generator:"
)
print(provisioning_uri)
print("\nOnce added to your authenticator app, log in via POST /login with username, "
      "password, and the 6-digit code the app shows (it refreshes every 30s).")
