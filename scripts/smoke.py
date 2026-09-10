"""
End-to-end check of the frontend, and through it the broker.

Standard library only, so it runs anywhere the app runs, with no venv:

    python scripts/smoke.py                       # against 127.0.0.1:8081
    python scripts/smoke.py --base http://host:8081 --user alice --password s3cret

It walks every route the browser can reach: the pages, the redirect guard,
login, key create/list/revoke, models, chat (streaming and not), usage, the
CSRF rejection, and sign-out. Exit code 0 means all paths resolve.
"""
import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

OK, FAIL = "  ok  ", " FAIL "
_failures: list[str] = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """We assert on the 303s themselves, so do not follow them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), NoRedirect
        )
        self.csrf = ""

    def request(self, method: str, path: str, body=None, csrf=True, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        if csrf and self.csrf and method != "GET":
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            resp = self.opener.open(req, timeout=120)
            payload, status = resp.read(), resp.status
            location = resp.headers.get("Location", "")
        except urllib.error.HTTPError as exc:
            payload, status = exc.read(), exc.code
            location = exc.headers.get("Location", "")
        except urllib.error.URLError as exc:
            return 0, str(exc.reason), ""
        text = payload.decode("utf-8", "replace")
        if raw:
            return status, text, location
        try:
            return status, json.loads(text) if text else None, location
        except json.JSONDecodeError:
            return status, text, location


def check(name: str, condition: bool, note: str = "") -> None:
    print(f"[{OK if condition else FAIL}] {name}" + (f" - {note}" if note else ""))
    if not condition:
        _failures.append(name)


def stream_deltas(body: str) -> int:
    """Count content chunks in an OpenAI-style SSE body."""
    count = 0
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = event.get("choices") or [{}]
        if choices[0].get("delta", {}).get("content"):
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8081")
    ap.add_argument("--user", default="smoke-user")
    ap.add_argument("--password", default="")
    ap.add_argument("--model", default="", help="default: the first model offered")
    args = ap.parse_args()

    c = Client(args.base)
    print(f"frontend: {args.base}\n")

    # --- reachability -----------------------------------------------------
    status, health, _ = c.request("GET", "/api/health")
    if status != 200:
        print(f"[{FAIL}] GET /api/health - {health}")
        print("\nthe frontend is not running:  uvicorn app.main:app --port 8081")
        return 1
    broker_status = (health or {}).get("broker", {}).get("status")
    check("GET /api/health", True, f"broker: {broker_status}")
    if broker_status != "ok":
        print(f"\n  ! the broker reports {broker_status!r} - checks below may fail\n")

    # --- the static shell -------------------------------------------------
    for path in ("/login", "/static/style.css", "/static/app.js"):
        status, _, _ = c.request("GET", path, raw=True)
        check(f"GET {path}", status == 200)

    # --- the guard: no session, no pages ----------------------------------
    for path in ("/", "/keys", "/usage"):
        status, _, location = c.request("GET", path, raw=True)
        check(
            f"GET {path} signed out redirects",
            status == 303 and location.startswith("/login"),
        )

    status, _, _ = c.request("GET", "/api/keys")
    check("GET /api/keys signed out is 401", status == 401)

    status, _, _ = c.request("GET", "/no-such-page", raw=True)
    check("GET /no-such-page is 404", status == 404)

    # --- sign in ----------------------------------------------------------
    status, body, _ = c.request(
        "POST", "/api/login", {"user_id": args.user, "password": args.password}
    )
    check("POST /api/login", status == 200, f"as {args.user}")
    if status != 200:
        print(f"\nlogin failed: {body}")
        return 1

    status, me, _ = c.request("GET", "/api/me")
    c.csrf = (me or {}).get("csrf", "")
    check(
        "GET /api/me",
        status == 200 and bool(me.get("signed_in")) and bool(c.csrf),
    )

    for path in ("/", "/keys", "/usage"):
        status, _, _ = c.request("GET", path, raw=True)
        check(f"GET {path} signed in", status == 200)

    # --- CSRF -------------------------------------------------------------
    status, _, _ = c.request("POST", "/api/keys", {"label": "csrf-probe"}, csrf=False)
    check("POST /api/keys without CSRF token is 403", status == 403)

    # --- control plane ----------------------------------------------------
    status, created, _ = c.request("POST", "/api/keys", {"label": "smoke"})
    created = created or {}
    check(
        "POST /api/keys",
        status == 201 and bool(created.get("api_key")),
        created.get("prefix", ""),
    )
    key_id = created.get("id")

    status, keys, _ = c.request("GET", "/api/keys")
    keys = keys if isinstance(keys, list) else []
    check(
        "GET /api/keys",
        status == 200 and any(k["id"] == key_id for k in keys),
        f"{len(keys)} key(s)",
    )

    # --- data plane -------------------------------------------------------
    status, models, _ = c.request("GET", "/api/models")
    names = [m["id"] for m in (models or {}).get("data", [])] if status == 200 else []
    check("GET /api/models", status == 200 and bool(names), ", ".join(names))
    model = args.model or (names[0] if names else "")

    if model:
        ask = {"model": model, "messages": [{"role": "user", "content": "ping"}]}

        status, reply, _ = c.request("POST", "/api/chat", dict(ask, stream=False))
        choices = (reply or {}).get("choices") or [{}]
        text = choices[0].get("message", {}).get("content", "")
        check("POST /api/chat (buffered)", status == 200 and bool(text), text[:60])

        status, body, _ = c.request("POST", "/api/chat", dict(ask, stream=True), raw=True)
        deltas = stream_deltas(body if isinstance(body, str) else "")
        check("POST /api/chat (streaming)", status == 200 and deltas > 0, f"{deltas} chunks")

        status, usage, _ = c.request("GET", "/api/usage")
        usage = usage if isinstance(usage, list) else []
        check(
            "GET /api/usage",
            status == 200,
            ", ".join(f"{r['model']}={r['requests']}" for r in usage),
        )

    # --- clean up ---------------------------------------------------------
    if key_id:
        status, _, _ = c.request("DELETE", f"/api/keys/{key_id}")
        check("DELETE /api/keys/<id>", status == 200)

    status, _, _ = c.request("DELETE", "/api/session-key")
    check("DELETE /api/session-key", status == 200)

    status, _, _ = c.request("POST", "/api/logout")
    check("POST /api/logout", status == 200)

    status, _, location = c.request("GET", "/", raw=True)
    check(
        "GET / after logout redirects",
        status == 303 and location.startswith("/login"),
    )

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed: {', '.join(_failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
