"""
The only place that talks to the broker.

    browser --(session cookie, TLS)--> THIS BFF --(JWT, private net)--> broker

Two different credentials leave this module, and confusing them is the classic
way to get this wrong:

  * control plane (/keys, /usage) - acts as a *user*. In hs256 mode we mint a
    short-lived JWT per call; in dev mode we send X-Dev-User. Either way the
    broker decides which user it is, from something we hand it.
  * data plane (/v1/*) - acts as a *program*, with an API key issued by the
    control plane. See session.Session.api_key.

The broker has no CORS middleware on purpose: the browser must never call it
directly. Everything goes through here.
"""
import datetime as dt
import logging
from typing import Any, AsyncIterator

import httpx
import jwt
from fastapi import HTTPException, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)

AUTH_MODES = ("dev", "hs256")

_client: httpx.AsyncClient | None = None


def validate_config(settings: Settings) -> None:
    """Fail at startup, not on the first click."""
    if settings.broker_auth_mode not in AUTH_MODES:
        raise ValueError(
            f"BROKER_AUTH_MODE must be one of {AUTH_MODES}, "
            f"got {settings.broker_auth_mode!r}. (The broker also supports "
            f"'oidc'; in that mode the BFF forwards the IdP's token instead "
            f"of minting one — see docs/AUTH.md.)"
        )
    if settings.broker_auth_mode == "hs256" and not settings.auth_jwt_secret:
        raise ValueError("BROKER_AUTH_MODE=hs256 requires AUTH_JWT_SECRET")
    if settings.broker_auth_mode == "dev":
        log.warning(
            "BROKER_AUTH_MODE=dev: the broker is told which user we are via "
            "an unverified header. Only safe on a laptop, and only while the "
            "broker also runs AUTH_MODE=dev."
        )


async def connect(settings: Settings) -> None:
    global _client
    _client = httpx.AsyncClient(
        base_url=settings.broker_url.rstrip("/"),
        timeout=settings.broker_timeout_s,
    )


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("broker client not connected")
    return _client


def mint_control_token(user_id: str, settings: Settings) -> str:
    """A JWT the broker will verify — Option A in docs/AUTH.md.

    Short-lived on purpose: the broker requires `exp`, and a leaked token is
    then only useful for minutes. `sub` is the user the broker will bill.
    """
    now = dt.datetime.now(dt.timezone.utc)
    claims = {
        "sub": user_id,
        "iat": now,
        "exp": now + dt.timedelta(seconds=settings.auth_jwt_ttl_s),
    }
    if settings.auth_jwt_issuer:
        claims["iss"] = settings.auth_jwt_issuer
    if settings.auth_jwt_audience:
        claims["aud"] = settings.auth_jwt_audience
    return jwt.encode(claims, settings.auth_jwt_secret, algorithm="HS256")


def control_headers(user_id: str, settings: Settings) -> dict[str, str]:
    if settings.broker_auth_mode == "hs256":
        return {"Authorization": f"Bearer {mint_control_token(user_id, settings)}"}
    return {"X-Dev-User": user_id}


def _fail(exc: Exception) -> HTTPException:
    """The broker is unreachable. Say so plainly, without leaking its address."""
    log.warning("broker unreachable: %s", exc)
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        "the broker is unreachable — is it running?",
    )


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:  # noqa: BLE001 - not every error body is JSON
        detail = resp.text[:500]
    raise HTTPException(resp.status_code, detail or resp.reason_phrase)


# --- control plane: acts as a user ----------------------------------------

async def control(
    method: str,
    path: str,
    user_id: str,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Any:
    settings = settings or get_settings()
    try:
        resp = await client().request(
            method, path, headers=control_headers(user_id, settings), **kwargs
        )
    except httpx.RequestError as exc:
        raise _fail(exc) from exc
    _raise_for_status(resp)
    if resp.status_code == status.HTTP_204_NO_CONTENT or not resp.content:
        return None
    return resp.json()


# --- data plane: acts as an API key ---------------------------------------

async def data(method: str, path: str, api_key: str, **kwargs: Any) -> Any:
    try:
        resp = await client().request(
            method, path, headers={"Authorization": f"Bearer {api_key}"}, **kwargs
        )
    except httpx.RequestError as exc:
        raise _fail(exc) from exc
    _raise_for_status(resp)
    return resp.json()


async def stream_chat(
    payload: dict, api_key: str
) -> AsyncIterator[bytes]:
    """Relay the broker's SSE stream to the browser byte for byte.

    No buffering and no re-framing: the browser sees exactly the OpenAI event
    stream the broker produced, so the page's parser is the standard one.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }
    try:
        async with client().stream(
            "POST", "/v1/chat/completions", json=payload, headers=headers
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                try:
                    import json as _json

                    detail = _json.loads(body).get("detail", body)
                except Exception:  # noqa: BLE001
                    detail = body[:500]
                raise HTTPException(resp.status_code, detail)
            async for chunk in resp.aiter_raw():
                yield chunk
    except httpx.RequestError as exc:
        raise _fail(exc) from exc
