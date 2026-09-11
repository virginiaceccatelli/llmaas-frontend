"""
LLMaaS frontend — the public tier.

    browser --(session cookie, TLS)--> THIS --(JWT, private net)--> broker

It is a backend-for-frontend, not a static site: the broker deliberately ships
no CORS middleware and must never be reachable from a browser, so every call
the pages make is proxied here, under a session cookie the page's JavaScript
cannot read.

Run it:  uvicorn app.main:app --reload --port 8081
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import broker, config, session, users
from .config import get_settings
from .routers import auth, chat, keys, pages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loaded = config.load_env_file()
    if loaded:
        log.info("loaded %d settings from .env: %s", len(loaded), ", ".join(loaded))

    settings = get_settings()

    # Config problems must stop the process here, not surface as a broken
    # button three clicks in.
    broker.validate_config(settings)
    session.configure(settings)
    users.load(settings)  # raises if the file exists but is malformed

    await session.connect(settings)
    await broker.connect(settings)

    log.info(
        "frontend ready on http://%s:%d — broker=%s (auth %s), login: %s",
        settings.host, settings.port, settings.broker_url,
        settings.broker_auth_mode, users.describe(settings),
    )
    if not settings.cookie_secure:
        log.warning(
            "COOKIE_SECURE=false: the session cookie will be sent over plain "
            "HTTP. Fine on localhost, never in production."
        )

    yield

    await broker.disconnect()
    await session.disconnect()


app = FastAPI(
    title="LLMaaS Frontend",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(keys.router)
app.include_router(chat.router)
app.mount("/static", StaticFiles(directory=pages.STATIC), name="static")


@app.exception_handler(404)
async def not_found(request, exc):
    """JSON for the API, a pointer to the real pages for everything else."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "no such endpoint"}, status_code=404)
    return JSONResponse(
        {"detail": f"no page at {request.url.path}", "pages": ["/", "/keys", "/usage"]},
        status_code=404,
    )

# NOTE: no CORS middleware here either — the pages are served from this same
# origin, so they never need it. Adding one would only let *other* sites drive
# a signed-in user's session.
#
# EXTEND: put this behind TLS (nginx/Caddy on the frontend VM), set
# COOKIE_SECURE=true, and add a request-id header that is forwarded to the
# broker so one click can be traced across all three tiers.
