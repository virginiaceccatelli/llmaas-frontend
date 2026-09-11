"""Check the Redis session store without needing Redis.

    python scripts/test_session_store.py

The risky part of moving sessions out of a dict is not the network — it is the
round trip. A Session is a dataclass holding the plaintext API key and the
CSRF token; through Redis it becomes JSON and back. Get a field name wrong, or
add a field later without thinking, and sessions silently lose their API key or
their CSRF token, which looks like a random logout rather than a bug.

This drives the real store functions against a minimal in-memory stand-in for
redis.asyncio, so it runs on a laptop with no Redis installed. CI exercises the
genuine article through docker-compose.full.yml.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import session as S  # noqa: E402
from app.config import Settings  # noqa: E402

failures: list[str] = []
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeRedis:
    """Just enough of redis.asyncio for the session store."""

    def __init__(self):
        self.data: dict[str, tuple[str, float]] = {}

    async def setex(self, key, ttl, value):
        self.data[key] = (value, time.time() + ttl)

    async def get(self, key):
        hit = self.data.get(key)
        if hit is None:
            return None
        value, expires = hit
        if time.time() >= expires:       # Redis would have dropped it
            del self.data[key]
            return None
        return value

    async def delete(self, key):
        self.data.pop(key, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class FakeRequest:
    def __init__(self, cookies):
        self.cookies = cookies


class FakeResponse:
    def __init__(self):
        self.cookies = {}
        self.deleted = []

    def set_cookie(self, name, value, **kw):
        self.cookies[name] = value

    def delete_cookie(self, name, **kw):
        self.deleted.append(name)


async def main() -> int:
    settings = Settings(session_secret="test-secret", session_ttl_s=3600)
    S.configure(settings)
    fake = FakeRedis()
    S._redis = fake                      # what connect() would have set

    print("\nRedis session store\n")

    # --- create -> cookie -> lookup --------------------------------------
    resp = FakeResponse()
    sess = await S.create("alice", resp, settings)
    cookie = resp.cookies[settings.session_cookie]
    check("create stores one key in redis", len(fake.data) == 1)
    check("cookie is signed sid", cookie.startswith(sess.sid + "."))

    req = FakeRequest({settings.session_cookie: cookie})
    got = await S.lookup(req, settings)
    check("lookup returns the session", got is not None and got.sid == sess.sid)
    check("user_id survives the round trip", got.user_id == "alice")
    check("csrf survives the round trip", got.csrf == sess.csrf)

    # --- the mutation path that the dict store used to do for free -------
    got.api_key = "wiit_secret_value"
    got.api_key_id = "abc-123"
    got.api_key_prefix = "wiit_abc"
    got.api_key_is_ours = True
    await S.save(got)

    again = await S.lookup(FakeRequest({settings.session_cookie: cookie}), settings)
    check("api_key persists across requests", again.api_key == "wiit_secret_value")
    check("api_key_id persists", again.api_key_id == "abc-123")
    check("api_key_is_ours persists", again.api_key_is_ours is True)

    # Every dataclass field must survive, or a future field silently vanishes.
    stored = json.loads(list(fake.data.values())[0][0])
    missing = set(sess.__dataclass_fields__) - set(stored)
    check("every Session field is serialised", not missing,
          f"missing: {sorted(missing)}" if missing else "all present")

    # --- a tampered cookie must not resolve ------------------------------
    bad = await S.lookup(
        FakeRequest({settings.session_cookie: sess.sid + ".deadbeef"}), settings)
    check("forged signature is rejected", bad is None)

    other = await S.lookup(FakeRequest({settings.session_cookie: "nope.nope"}), settings)
    check("garbage cookie is rejected", other is None)

    # --- expiry ----------------------------------------------------------
    expired = await S.create("bob", FakeResponse(), settings)
    expired.expires_at = time.time() - 1
    await S.save(expired)
    gone = await S.lookup(
        FakeRequest({settings.session_cookie: S._cookie_value(expired.sid)}), settings)
    check("expired session is not returned", gone is None)

    # --- unreadable payload must not 500 ---------------------------------
    fake.data[S.KEY_PREFIX + "junk"] = ("{not json", time.time() + 60)
    junk = await S.lookup(
        FakeRequest({settings.session_cookie: S._cookie_value("junk")}), settings)
    check("unreadable session is discarded, not fatal", junk is None)

    # --- destroy ---------------------------------------------------------
    resp2 = FakeResponse()
    await S.destroy(again, resp2, settings)
    after = await S.lookup(FakeRequest({settings.session_cookie: cookie}), settings)
    check("destroy removes the session", after is None)
    check("destroy clears the cookie", settings.session_cookie in resp2.deleted)

    print(f"\n{total - len(failures)} passed, {len(failures)} failed\n")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
