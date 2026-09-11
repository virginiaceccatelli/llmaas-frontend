"""
Frontend BFF configuration. Environment-driven, same 12-factor style as the
broker, so the same image runs under docker-compose today and Kubernetes later.

See .env.example for the full list.
"""
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = ".env"


def load_env_file(path: str = ENV_FILE) -> list[str]:
    """Copy .env into os.environ without overriding real environment vars.

    Same helper as the broker's config.py, and for the same reason: real
    environment variables must win so Docker/Kubernetes keep control.
    """
    f = Path(path)
    if not f.exists():
        return []
    loaded = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # --- Where the broker lives -------------------------------------------
    # On a laptop: http://127.0.0.1:8080. On the VMs: the broker's PRIVATE ip.
    # This URL is never sent to the browser; only this process dials it.
    broker_url: str = "http://127.0.0.1:8080"
    broker_timeout_s: float = 120.0

    # --- How we prove to the broker which user we are acting for ----------
    # Must match the broker's AUTH_MODE. See LLMaaS/docs/AUTH.md.
    #   "dev"   - send an unverified X-Dev-User header (LOCAL ONLY)
    #   "hs256" - mint a short-lived JWT signed with a shared secret
    broker_auth_mode: str = "dev"
    auth_jwt_secret: str = ""
    auth_jwt_issuer: str = "llmaas-frontend"
    auth_jwt_audience: str = "llmaas-broker"
    auth_jwt_ttl_s: int = 300  # keep it short; the broker requires exp

    # --- Browser session ---------------------------------------------------
    # Signs the session cookie. Leave empty and a random one is generated at
    # startup, which logs everyone out on restart. Set it in production.
    # Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Where sessions live. Set it and sessions survive a restart and work
    # across workers; leave it empty and they are a dict in this process
    # (local development only). On the VMs this is the gateway VM's Redis,
    # on its own database number so a FLUSHDB elsewhere cannot sign everyone
    # out. See app/session.py.
    redis_url: str = ""

    session_secret: str = ""
    session_ttl_s: int = 8 * 3600
    session_cookie: str = "llmaas_session"
    # MUST be true anywhere but localhost — the cookie is a bearer credential.
    cookie_secure: bool = False

    # --- Login -------------------------------------------------------------
    # A JSON file of {"user_id": {"password_hash": "...", "email": "..."}}.
    # Manage it with `python scripts/users.py`. When the file is absent,
    # passwordless dev sign-in applies (see allow_passwordless).
    users_file: str = "users.json"
    # LOCAL ONLY: with no users file, anyone may sign in as any user id.
    allow_passwordless: bool = True

    # --- Chat --------------------------------------------------------------
    # The data plane authenticates with an API key, not a login. When true the
    # BFF mints one key per browser session (label below) on first use and
    # revokes it at logout, so chat works with no manual key handling.
    auto_mint_session_key: bool = True
    session_key_label: str = "web-ui session"

    host: str = "127.0.0.1"
    port: int = 8081


@lru_cache
def get_settings() -> Settings:
    return Settings()
