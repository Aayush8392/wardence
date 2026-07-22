"""
JWT helpers for the Operator API. Roles: admin, demo-trigger, viewer.

SECRET_KEY lives in p3_trust_action/jwt_secret.txt (gitignored, generated
once). Admin tokens are never issued via a login endpoint -- admin is
you only, never distributed (see wardence_context.md) -- you mint your
own token locally with mint_token.py.
"""

import datetime
from pathlib import Path

import jwt

SECRET_KEY_PATH = Path(__file__).parent / "jwt_secret.txt"
ALGORITHM = "HS256"

ROLES = {"admin", "demo-trigger", "viewer"}


def _load_secret() -> str:
    if not SECRET_KEY_PATH.exists():
        raise RuntimeError(
            f"{SECRET_KEY_PATH} not found -- run mint_token.py once to generate it"
        )
    return SECRET_KEY_PATH.read_text().strip()


def create_token(role: str, expires_hours: int = 24, username: str | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role '{role}', must be one of {ROLES}")
    payload = {
        "role": role,
        "username": username,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=expires_hours),
        "iat": datetime.datetime.now(datetime.timezone.utc),
    }
    return jwt.encode(payload, _load_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError on invalid/expired tokens -- caller handles it."""
    return jwt.decode(token, _load_secret(), algorithms=[ALGORITHM])
