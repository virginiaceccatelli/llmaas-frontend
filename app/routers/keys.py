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
    apikeys.forget(sess, key_id)
    return result


@router.get("/usage")
async def usage(
    sess: Session = Depends(require_session),
    settings: Settings = Depends(get_settings),
):
    return await broker.control("GET", "/usage", sess.user_id, settings)
