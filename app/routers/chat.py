"""
The chat page's back end: the data plane, reached with this session's API key.

Nothing here interprets the model's output. The request body is validated just
enough to be safe and forwarded; the response — including the SSE stream — is
relayed untouched, so this stays correct when the broker gains new parameters
and when Envoy AI Gateway takes over /v1/*.
"""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import apikeys, broker
from ..config import Settings, get_settings
from ..session import Session, require_csrf, require_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class Message(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(max_length=100_000)


class ChatRequest(BaseModel):
    model: str = Field(max_length=128)
    messages: list[Message] = Field(min_length=1, max_length=200)
    stream: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32_000)


class UseKeyRequest(BaseModel):
    api_key: str = Field(max_length=256)


@router.get("/models")
async def list_models(
    sess: Session = Depends(require_session),
    settings: Settings = Depends(get_settings),
):
    api_key = await apikeys.ensure(sess, settings)
    return await broker.data("GET", "/v1/models", api_key)


@router.post("/chat")
async def chat(
    body: ChatRequest,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    api_key = await apikeys.ensure(sess, settings)
    payload = body.model_dump(exclude_none=True)

    if not body.stream:
        return await broker.data(
            "POST", "/v1/chat/completions", api_key, json=payload
        )

    # Ask the broker's upstream to report token usage in the final chunk, so
    # the page can show what the turn cost without a second round trip.
    payload["stream_options"] = {"include_usage": True}
    return StreamingResponse(
        broker.stream_chat(payload, api_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/session-key")
async def set_session_key(
    body: UseKeyRequest,
    sess: Session = Depends(require_csrf),
):
    """Chat with a key the user already holds instead of a minted one."""
    await apikeys.use_key(sess, body.api_key)
    return {"status": "ok", "prefix": sess.api_key_prefix, "minted": False}


@router.delete("/session-key")
async def clear_session_key(
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    """Drop this session's key (revoking it if we minted it). The next chat
    request mints a fresh one."""
    await apikeys.release(sess, settings)
    return {"status": "cleared"}
