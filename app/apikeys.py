"""
The session's data-plane credential.

The control plane (a logged-in human) and the data plane (a program with an
API key) are separate systems in the broker, and the chat page needs both: it
is driven by a logged-in human, but /v1/chat/completions only accepts a key.

So the BFF mints one key per browser session on first use and revokes it at
logout. The plaintext lives in the session for its lifetime and is never sent
to the browser — the broker shows a key exactly once.

A user may instead paste a key they already hold (`use_key`); we then never
revoke it, because we did not create it.

TWO THINGS TO KNOW IF YOU EDIT THIS FILE:

1. Every function that changes the session must `await session.save(sess)`.
   With the old in-process dict the Session object *was* the stored state and
   mutation was enough. With Redis it is not: an unsaved change is silently
   lost on the next request.

2. Minted keys carry an expiry. Revoking at logout is best-effort — if this
   process restarts while sessions are live, nothing ever revokes them and
   before `expires_in_s` they stayed valid forever, accumulating in the
   broker's `api_keys` table with nothing tracking them.
"""
import logging

from fastapi import HTTPException, status

from . import broker, session
from .config import Settings
from .session import Session

log = logging.getLogger(__name__)

# Margin on top of the session TTL, so a key cannot expire out from under a
# session that is still valid.
KEY_TTL_MARGIN_S = 600


async def ensure(sess: Session, settings: Settings) -> str:
    """Return this session's API key, minting one if needed."""
    if sess.api_key:
        return sess.api_key

    if not settings.auto_mint_session_key:
        raise HTTPException(
            status.HTTP_412_PRECONDITION_FAILED,
            "no API key for this session — paste one on the Keys page "
            "(AUTO_MINT_SESSION_KEY is off)",
        )

    created = await broker.control(
        "POST", "/keys", sess.user_id, settings,
        json={
            "label": settings.session_key_label,
            # The backstop for the revoke-at-logout that may never happen.
            "expires_in_s": settings.session_ttl_s + KEY_TTL_MARGIN_S,
        },
    )
    sess.api_key = created["api_key"]
    sess.api_key_id = created["id"]
    sess.api_key_prefix = created["prefix"]
    sess.api_key_is_ours = True
    await session.save(sess)
    log.info("minted session key %s for %s (expires %s)",
             created["prefix"], sess.user_id, created.get("expires_at"))
    return sess.api_key


async def use_key(sess: Session, api_key: str) -> None:
    """Adopt a key the user pasted, replacing (but not revoking) any minted one."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no key given")
    sess.api_key = api_key
    sess.api_key_id = None
    # Show enough to recognise it, never the whole key.
    sess.api_key_prefix = api_key[:13]
    sess.api_key_is_ours = False
    await session.save(sess)


async def release(sess: Session, settings: Settings) -> None:
    """Revoke the key we minted for this session. Never one the user pasted."""
    if not (sess.api_key_is_ours and sess.api_key_id):
        sess.api_key = None
        await session.save(sess)
        return
    try:
        await broker.control(
            "DELETE", f"/keys/{sess.api_key_id}", sess.user_id, settings
        )
        log.info("revoked session key %s", sess.api_key_prefix)
    except Exception as exc:  # noqa: BLE001 - logout must never fail on this
        log.warning("could not revoke session key on logout: %s", exc)
    sess.api_key = None
    sess.api_key_id = None
    sess.api_key_prefix = None
    sess.api_key_is_ours = False
    await session.save(sess)


async def forget(sess: Session, key_id: str) -> None:
    """Called when the user revokes a key from the Keys page: if it was the
    one this session was chatting with, stop using it."""
    if sess.api_key_id and sess.api_key_id == key_id:
        sess.api_key = None
        sess.api_key_id = None
        sess.api_key_prefix = None
        sess.api_key_is_ours = False
        await session.save(sess)
