"""
Who may sign in.

Deliberately small. The broker's `users` table is thin on purpose — identity
belongs here (or in an IdP), and the broker only ever asks "which user is this
request for?". Two modes:

  * a users file exists -> password login, hashed with scrypt
  * no users file       -> passwordless dev sign-in, if ALLOW_PASSWORDLESS

Passwords are low-entropy, so unlike API keys they need a *slow* hash. scrypt
is in the standard library, which keeps this dependency-free; argon2id via
`argon2-cffi` is the better choice if you are willing to add the dependency.

EXTEND before real users: email verification, password reset, account lockout
after repeated failures, and MFA. That is real work — or skip all of it by
moving to AUTH_MODE=oidc and letting Authentik/Keycloak own the problem
(docs/AUTH.md, Option B).
"""
import base64
import hmac
import json
import logging
import re
import secrets
from hashlib import scrypt
from pathlib import Path

from fastapi import HTTPException, status

from .config import Settings

log = logging.getLogger(__name__)

# Mirrors what the broker will store as users.id, and what ends up in the JWT
# `sub`. Kept tight so a user id can never be confused for anything else.
USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "$".join(
        [
            "scrypt",
            str(_N),
            str(_R),
            str(_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except Exception:  # noqa: BLE001 - a malformed record is a failed login
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


def load(settings: Settings) -> dict[str, dict]:
    f = Path(settings.users_file)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{settings.users_file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{settings.users_file} must be a JSON object")
    return data


def passwordless(settings: Settings) -> bool:
    """True when anyone may sign in as any user id, with no password."""
    return not load(settings) and settings.allow_passwordless


def describe(settings: Settings) -> str:
    known = load(settings)
    if known:
        return f"password login, {len(known)} user(s) in {settings.users_file}"
    if settings.allow_passwordless:
        return "PASSWORDLESS dev sign-in (no users file)"
    return "login disabled: no users file and ALLOW_PASSWORDLESS=false"


def authenticate(user_id: str, password: str, settings: Settings) -> str:
    user_id = (user_id or "").strip()
    if not USER_ID_RE.match(user_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "user id must be 1-64 chars: letters, digits, and . _ - @",
        )

    known = load(settings)

    if not known:
        if not settings.allow_passwordless:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "no users are configured — run: python scripts/users.py add <id>",
            )
        log.warning("passwordless sign-in as %r (dev mode)", user_id)
        return user_id

    record = known.get(user_id)
    # Hash even when the user does not exist, so the response time does not
    # tell an attacker which ids are real.
    encoded = (record or {}).get("password_hash") or hash_password(secrets.token_hex())
    if not verify_password(password or "", encoded) or record is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return user_id
