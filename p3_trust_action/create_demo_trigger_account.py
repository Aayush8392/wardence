"""
One-off script: creates the ONE PERMANENT demo-trigger account described in
wardence_frontend.md's Auth Model section (the project owner's own testing
account -- not the same as a time-limited account handed to someone else).

Unlike create_admin_account.py (which writes directly to the DB to solve
the very first chicken-and-egg problem), this script goes through the real
running API -- POST /login as admin, then POST /accounts -- since a
demo-trigger account has no bootstrap problem: an admin account already
exists by the time this is needed.

Usage:
    python3 p3_trust_action/create_demo_trigger_account.py <demo_trigger_username>
    (you'll be prompted for the ADMIN username/password/TOTP code to
    authenticate, then for the NEW demo-trigger account's password)

Requires operator_api.py to already be running (default
http://localhost:8002 -- override with OPERATOR_API_URL env var).
"""

import getpass
import os
import sys

import requests

BASE_URL = os.environ.get("OPERATOR_API_URL", "http://localhost:8002")

if len(sys.argv) != 2:
    print("Usage: python3 create_demo_trigger_account.py <demo_trigger_username>")
    sys.exit(1)

new_username = sys.argv[1]

print("Authenticate as admin to create the new account:")
admin_username = input("Admin username: ")
admin_password = getpass.getpass("Admin password: ")
admin_totp = input("Admin TOTP code (6 digits): ")

login_resp = requests.post(
    f"{BASE_URL}/login",
    json={"username": admin_username, "password": admin_password, "totp_code": admin_totp},
    timeout=10,
)
if login_resp.status_code != 200:
    print(f"Admin login failed: {login_resp.status_code} {login_resp.text}")
    sys.exit(1)

admin_token = login_resp.json()["token"]

new_password = getpass.getpass(f"\nPassword for new demo-trigger account '{new_username}': ")
new_password_confirm = getpass.getpass("Confirm password: ")
if new_password != new_password_confirm:
    print("Passwords did not match.")
    sys.exit(1)

# expires_hours=None -> permanent, matching the locked design ("one
# permanent demo-trigger account, for the project owner's own testing --
# acceptable specifically because it's revocable, not because it's low
# stakes"). Any FUTURE demo-trigger account handed to someone else should
# instead be created via POST /accounts directly with a real expires_hours
# (24h default per the locked design), not via this script.
create_resp = requests.post(
    f"{BASE_URL}/accounts",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={"username": new_username, "password": new_password, "role": "demo-trigger", "expires_hours": None},
    timeout=10,
)
if create_resp.status_code != 200:
    print(f"Account creation failed: {create_resp.status_code} {create_resp.text}")
    sys.exit(1)

print(f"\nDemo-trigger account '{new_username}' created (permanent).")
print("Log in via POST /login with this username/password -- no TOTP needed (admin-only requirement).")
