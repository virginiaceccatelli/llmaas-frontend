"""
Key management and usage: a thin, authenticated pass-through to the broker's
control plane. The browser never learns the broker's address, and cannot call
it directly even if it did — there is no CORS there, by design.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import apikeys, broker
from ..config import Settings, get_settings
from ..session import Session, require_csrf, require_session

router = APIRouter(prefix="/api", tags=["keys"])


class CreateKeyRequest(BaseModel):
    label: str = Field(default="default", max_length=64)


@router.get("/keys")
async def list_keys(
    sess: Session = Depends(require_session),
    settings: Settings = Depends(get_settings),
):
    return await broker.control("GET", "/keys", sess.user_id, settings)


@router.post("/keys", status_code=201)
async def create_key(
    body: CreateKeyRequest,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    # The plaintext key is in this response and nowhere else, ever again. We
    # hand it straight to the browser and keep no copy.
    return await broker.control(
        "POST", "/keys", sess.user_id, settings, json=body.model_dump()
    )


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: str,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    result = await broker.control(
        "DELETE", f"/keys/{key_id}", sess.user_id, settings
    )
    await apikeys.forget(sess, key_id)
    return result


@router.get("/usage")
async def usage(
    sess: Session = Depends(require_session),
    settings: Settings = Depends(get_settings),
):
    return await broker.control("GET", "/usage", sess.user_id, settings)


# --- conversations: chat history, one per user ----------------------------
# A thin pass-through, exactly like /api/keys above. The isolation is the
# broker's: every query there is scoped by the `sub` of the JWT we mint, so a
# user cannot read or delete another's history even by guessing an id.
#
# This replaced localStorage, which was per-BROWSER: two people signing in to
# the same browser shared one history, and one person on two devices had two.


class ConversationBody(BaseModel):
    title: str = Field(default="New chat", max_length=200)
    messages: list[dict] = Field(default_factory=list, max_length=400)


@router.get("/conversations")
async def list_conversations(
    sess: Session = Depends(require_session),
    settings: Settings = Depends(get_settings),
):
    return await broker.control("GET", "/conversations", sess.user_id, settings)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    sess: Session = Depends(require_session),
    settings: Settings = Depends(get_settings),
):
    return await broker.control(
        "GET", f"/conversations/{conversation_id}", sess.user_id, settings
    )


@router.put("/conversations/{conversation_id}")
async def put_conversation(
    conversation_id: str,
    body: ConversationBody,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    return await broker.control(
        "PUT", f"/conversations/{conversation_id}", sess.user_id, settings,
        json=body.model_dump(),
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    return await broker.control(
        "DELETE", f"/conversations/{conversation_id}", sess.user_id, settings
    )
