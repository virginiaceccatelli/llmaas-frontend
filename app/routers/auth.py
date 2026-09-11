"""Sign in, sign out, and "who am I" — the browser-facing half of auth."""
import logging

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .. import apikeys, broker, session, users
from ..config import Settings, get_settings
from ..session import Session, require_csrf

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    user_id: str = Field(max_length=64)
    password: str = Field(default="", max_length=256)


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
):
    user_id = users.authenticate(body.user_id, body.password, settings)
    sess = await session.create(user_id, response, settings)
    log.info("signed in: %s", user_id)
    return {"user_id": sess.user_id, "csrf": sess.csrf}


@router.post("/logout")
async def logout(
    response: Response,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    # Revoke the key we minted for this session before dropping it, otherwise
    # it would linger in the database, valid and unreachable.
    await apikeys.release(sess, settings)
    await session.destroy(sess, response, settings)
    return {"status": "signed out"}


@router.get("/me")
async def me(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """The page calls this on load: it decides sign-in state and supplies the
    CSRF token for every later mutating call."""
    sess = await session.lookup(request, settings)
    if sess is None:
        return {"signed_in": False, "passwordless": users.passwordless(settings)}
    return {
        "signed_in": True,
        "user_id": sess.user_id,
        "csrf": sess.csrf,
        "session_key_prefix": sess.api_key_prefix,
        "session_key_is_ours": sess.api_key_is_ours,
        "broker_auth_mode": settings.broker_auth_mode,
    }


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)):
    """Ours, plus whatever the broker says about itself. Unauthenticated on
    purpose — it is the first thing to check when the UI misbehaves."""
    out: dict = {"frontend": "ok", "broker_url": settings.broker_url}
    try:
        resp = await broker.client().get("/ready")
        out["broker"] = resp.json()
    except httpx.RequestError as exc:
        out["broker"] = {"status": "unreachable", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        out["broker"] = {"status": "error", "error": str(exc)}
    return out
