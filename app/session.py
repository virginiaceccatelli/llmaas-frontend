"""
Browser sessions.

The session cookie is the ONLY credential the browser ever holds. It is
HttpOnly, so JavaScript cannot read it, and it carries nothing but an opaque
id: the user id, the CSRF token and the session's API key live server-side.

Two stores:

  "redis"  - correct. Sessions survive a restart, and every worker and replica
             sees the same session. This is the only valid choice in
             production, and the only one where more than one uvicorn worker
             behaves.
  "memory" - LOCAL DEV ONLY. A dict in this process: every restart signs all
             users out, and with N workers a user's requests land on a worker
             that has never heard of them.

Chosen by whether REDIS_URL is set.

A session holds a PLAINTEXT API KEY (see Session.api_key). In the memory store
that key never leaves the process; in Redis it is at rest in a second system.
That is the trade for sessions that survive a restart, and it is why
auto-minted keys now carry an expires_at — see apikeys.ensure. Give Redis a
password and keep it on the private network.
"""
import hmac
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from hashlib import sha256

from fastapi import Depends, HTTPException, Request, Response, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)

KEY_PREFIX = "llmaas:sess:"


@dataclass
class Session:
    sid: str
    user_id: str
    csrf: str
    expires_at: float
    # Data-plane credential for this session. Minted lazily on the first chat
    # request and revoked at logout. The broker stores only its SHA-256 and
    # will never show it to us again, so this is the only copy.
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


_sessions: dict[str, Session] = {}       # the "memory" store
_redis = None                            # redis.asyncio.Redis when configured
_secret: str = ""
_ttl_s: int = 8 * 3600


def configure(settings: Settings) -> None:
    """Fix the cookie-signing secret at startup."""
    global _secret, _ttl_s
    _ttl_s = settings.session_ttl_s
    if settings.session_secret:
        _secret = settings.session_secret
        return
    _secret = secrets.token_urlsafe(32)
    log.warning(
        "SESSION_SECRET is unset: generated a random one. Every restart "
        "invalidates all sessions, and multiple workers will not agree. "
        "Set it in .env before deploying."
    )


async def connect(settings: Settings) -> None:
    """Attach the Redis store, if REDIS_URL is set. Fails loudly if it cannot.

    A silent fall back to the dict would be worse than not starting: sessions
    would appear to work and then behave differently under load.
    """
    global _redis
    if not settings.redis_url:
        log.warning(
            "REDIS_URL is unset: sessions are held in this process. Every "
            "restart signs all users out, and running more than one worker "
            "breaks sign-in. Local development only."
        )
        return
    import redis.asyncio as redis  # noqa: PLC0415 - optional in dev

    _redis = redis.from_url(settings.redis_url, decode_responses=True)
    await _redis.ping()
    log.info("session store: redis")


async def disconnect() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


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


async def save(sess: Session) -> None:
    """Persist a session after it changes.

    With the dict store this is a no-op — the object IS the stored state. With
    Redis it is not: anything that mutates a Session (apikeys.ensure, use_key,
    release, forget) must call this or the change is lost on the next request.
    """
    if _redis is None:
        _sessions[sess.sid] = sess
        return
    ttl = max(1, int(sess.expires_at - time.time()))
    await _redis.setex(KEY_PREFIX + sess.sid, ttl, json.dumps(asdict(sess)))


async def create(user_id: str, response: Response, settings: Settings) -> Session:
    _reap()
    sess = Session(
        sid=secrets.token_urlsafe(32),
        user_id=user_id,
        csrf=secrets.token_urlsafe(32),
        expires_at=time.time() + settings.session_ttl_s,
    )
    await save(sess)
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


async def destroy(sess: Session, response: Response, settings: Settings) -> None:
    if _redis is not None:
        await _redis.delete(KEY_PREFIX + sess.sid)
    else:
        _sessions.pop(sess.sid, None)
    response.delete_cookie(settings.session_cookie, path="/")


async def lookup(request: Request, settings: Settings) -> Session | None:
    sid = _sid_from_cookie(request.cookies.get(settings.session_cookie))
    if sid is None:
        return None

    if _redis is not None:
        raw = await _redis.get(KEY_PREFIX + sid)
        if raw is None:
            return None
        try:
            sess = Session(**json.loads(raw))
        except (TypeError, ValueError):
            # A session written by an older version of this dataclass. Drop it
            # rather than 500 — the user simply signs in again.
            log.info("discarding unreadable session %s", sid[:8])
            await _redis.delete(KEY_PREFIX + sid)
            return None
    else:
        sess = _sessions.get(sid)
        if sess is None:
            return None

    if sess.expired:
        if _redis is not None:
            await _redis.delete(KEY_PREFIX + sid)
        else:
            _sessions.pop(sid, None)
        return None
    return sess


def _reap() -> None:
    """Only the memory store needs this; Redis expires keys itself."""
    if _redis is not None:
        return
    for sid in [s for s, v in _sessions.items() if v.expired]:
        _sessions.pop(sid, None)


# --- FastAPI dependencies --------------------------------------------------

async def require_session(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Session:
    """401 for API callers. The pages redirect to /login themselves."""
    sess = await lookup(request, settings)
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    return sess


async def require_csrf(
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
