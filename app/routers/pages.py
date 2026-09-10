"""
The pages themselves. Three real URLs, not a hash-router: /, /keys and /usage
each resolve on their own, so a reload or a bookmark works.

Anything that needs a session redirects to /login?next=..., which is the one
place sign-in state changes what you get back.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import session
from ..config import Settings, get_settings

router = APIRouter(include_in_schema=False)

STATIC = Path(__file__).resolve().parent.parent / "static"


def _page(name: str) -> FileResponse:
    # no-store: these shells are tiny and always fetch fresh state on load;
    # a cached signed-in page shown after logout would be a nasty surprise.
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


def _guard(request: Request, settings: Settings, name: str) -> Response:
    if session.lookup(request, settings) is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return _page(name)


@router.get("/")
async def chat_page(request: Request, settings: Settings = Depends(get_settings)):
    return _guard(request, settings, "chat.html")


@router.get("/keys")
async def keys_page(request: Request, settings: Settings = Depends(get_settings)):
    return _guard(request, settings, "keys.html")


@router.get("/usage")
async def usage_page(request: Request, settings: Settings = Depends(get_settings)):
    return _guard(request, settings, "usage.html")


@router.get("/login")
async def login_page(request: Request, settings: Settings = Depends(get_settings)):
    if session.lookup(request, settings) is not None:
        return RedirectResponse("/", status_code=303)
    return _page("login.html")


@router.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
