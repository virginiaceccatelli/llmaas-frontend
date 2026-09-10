"""
Browser sessions.

The session cookie is the ONLY credential the browser ever holds. It is
HttpOnly, so JavaScript cannot read it, and it carries nothing but an opaque
id: the user id, the CSRF token and the session's API key live in this
process, never in the browser.

The store is in-memory, which is honest for a PoC and wrong for production:
sessions vanish on restart and are not shared between workers or replicas.
EXTEND: back it with Redis (the broker VM already runs one) keyed by sid,
which is a drop-in replacement for the `_sessions` dict below.
"""
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256

from fastapi import Depends, HTTPException, Request, Response, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass
class Session:
    sid: str
    user_id: str
    csrf: str
    expires_at: float
    # Data-plane credential for this session. Minted lazily on the first chat
    # request and revoked at logout. Plaintext, in memory only — the broker
    # stores just its SHA-256 and will never show it to us again.
    api_key: str | None = None
    api_key_id: str | None = None
    api_key_prefix: str | None = None
    # True when the user pasted their own key instead of us minting one; we
    # must not revoke a key we did not create.
    api_key_is_ours: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


_sessions: dict[str, Session] = {}
_secret: str = ""


def configure(settings: Settings) -> None:
    """Fix the cookie-signing secret at startup."""
    global _secret
    if settings.session_secret:
        _secret = settings.session_secret
        return
    _secret = secrets.token_urlsafe(32)
    log.warning(
        "SESSION_SECRET is unset: generated a random one. Every restart "
        "invalidates all sessions, and multiple workers will not agree. "
        "Set it in .env before deploying."
    )


def _sign(sid: str) -> str:
    return sha256(f"{_secret}:{sid}".encode()).hexdigest()[:32]


def _cookie_value(sid: str) -> str:
    return f"{sid}.{_sign(sid)}"


def _sid_from_cookie(raw: str | None) -> str | None:
    """Return the sid only if the signature checks out."""
    if not raw or "." not in raw:
        return None
    sid, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(sid)):
        return None
    return sid


def create(user_id: str, response: Response, settings: Settings) -> Session:
    _reap()
    sess = Session(
        sid=secrets.token_urlsafe(32),
        user_id=user_id,
        csrf=secrets.token_urlsafe(32),
        expires_at=time.time() + settings.session_ttl_s,
    )
    _sessions[sess.sid] = sess
    response.set_cookie(
        settings.session_cookie,
        _cookie_value(sess.sid),
        max_age=settings.session_ttl_s,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return sess


def destroy(sess: Session, response: Response, settings: Settings) -> None:
    _sessions.pop(sess.sid, None)
    response.delete_cookie(settings.session_cookie, path="/")


def lookup(request: Request, settings: Settings) -> Session | None:
    sid = _sid_from_cookie(request.cookies.get(settings.session_cookie))
    if sid is None:
        return None
    sess = _sessions.get(sid)
    if sess is None:
        return None
    if sess.expired:
        _sessions.pop(sid, None)
        return None
    return sess


def _reap() -> None:
    for sid in [s for s, v in _sessions.items() if v.expired]:
        _sessions.pop(sid, None)


# --- FastAPI dependencies --------------------------------------------------

def require_session(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Session:
    """401 for API callers. The pages redirect to /login themselves."""
    sess = lookup(request, settings)
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    return sess


def require_csrf(
    request: Request,
    sess: Session = Depends(require_session),
) -> Session:
    """Double-submit CSRF check on every state-changing API call.

    SameSite=Lax already blocks cross-site POSTs from a form, but not every
    browser in the wild honours it, and it says nothing about same-site
    subdomains. The token is handed to the page by GET /api/me and echoed in
    the X-CSRF-Token header.
    """
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(token, sess.csrf):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad or missing CSRF token")
    return sess
