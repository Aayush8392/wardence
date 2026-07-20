"""
Generate the JWT signing secret (first run only) and mint a token for a
given role. Admin tokens are minted here, locally, by you -- there is no
login endpoint for admin (never distributed, see wardence_context.md).

Usage:
    python3 p3_trust_action/mint_token.py admin
    python3 p3_trust_action/mint_token.py demo-trigger --hours 1
    python3 p3_trust_action/mint_token.py viewer
"""

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auth import SECRET_KEY_PATH, create_token  # noqa: E402

if not SECRET_KEY_PATH.exists():
    SECRET_KEY_PATH.write_text(secrets.token_hex(32))
    print(f"Generated new signing secret at {SECRET_KEY_PATH}")

parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["admin", "demo-trigger", "viewer"])
parser.add_argument("--hours", type=int, default=24)
args = parser.parse_args()

token = create_token(args.role, expires_hours=args.hours)
print(token)
