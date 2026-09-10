
## Quick start (no Docker)

The broker must be running first — see the backend repo's Quick start A, which
needs three terminals of its own (mock upstream, Postgres, broker).

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env          # defaults match the broker's dev defaults

uvicorn app.main:app --reload --port 8081
```

Open <http://127.0.0.1:8081>, sign in as any user id (see the warning below),
and send a message. Then verify every path resolves:

```powershell
python scripts\smoke.py
```

```
[  ok  ] GET /api/health - broker: ok
[  ok  ] GET / signed out redirects
[  ok  ] POST /api/keys without CSRF token is 403
[  ok  ] GET /api/models - qwen-instruct, qwen-coder
[  ok  ] POST /api/chat (streaming) - 5 chunks
...
all checks passed
```

Interactive API docs: <http://127.0.0.1:8081/docs>

---

## The two auth systems, and how this repo uses both

The broker keeps a **control plane** (a human managing keys) apart from a
**data plane** (a program spending tokens). The chat page needs both at once:
it is driven by a logged-in human, but `/v1/chat/completions` only accepts an
API key. So:

| | Credential | Where it lives | Set by |
|---|---|---|---|
| Browser → BFF | session cookie | `HttpOnly` cookie + this process | login |
| BFF → broker control plane | short-lived JWT (`sub` = user) | minted per call, never stored | `BROKER_AUTH_MODE` |
| BFF → broker data plane | API key | this process, for the session's life | minted on first chat |

The session's API key is minted on the first chat request, labelled
`web-ui session`, and **revoked at sign-out**. It is never sent to the browser
— the broker shows a key exactly once, and that plaintext stays here. Users
who already hold a key can paste it on the Keys page instead; a pasted key is
never revoked by us, because we did not create it.

### Matching the broker's `AUTH_MODE`

`BROKER_AUTH_MODE` in this repo must match `AUTH_MODE` in the broker's `.env`:

| Broker `AUTH_MODE` | Set here | What crosses the private network |
|---|---|---|
| `dev` | `BROKER_AUTH_MODE=dev` | `X-Dev-User: <id>`, unverified |
| `hs256` | `BROKER_AUTH_MODE=hs256` + the same `AUTH_JWT_SECRET` | a JWT the broker verifies |
| `oidc` | not yet implemented here | the IdP's token, forwarded |

Mismatches fail at startup, not on the first click. Both `dev` and `hs256`
are verified by `scripts/smoke.py`.

For `hs256`, generate one secret and put the identical value in both `.env`
files, along with matching issuer and audience:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Login

Two modes, chosen by whether `users.json` exists:

```powershell
python scripts\users.py add alice          # prompts for a password (scrypt)
python scripts\users.py list
python scripts\users.py remove alice
```

**With no users file, anyone may sign in as any user id, with no password.**
That is dev convenience and the login page says so in a banner. Set
`ALLOW_PASSWORDLESS=false` (or add a user) before this reaches a network.

Passwords use `hashlib.scrypt` — a deliberately slow hash, because unlike API
keys, passwords are low-entropy. Unknown users are still hashed against a
dummy value so response times do not reveal which ids exist.

---

---

## What of the architecture is already wired up

Measured against [architecture.md](../architecture.md), across both repos.

> That link resolves when this repo is checked out as the `frontend/` submodule
> of `llmaas-backend`, which is how the two are joined. Standalone, read it at
> <https://github.com/virginiaceccatelli/llmaas-backend/blob/main/architecture.md>.

| Layer | Planned | Status |
|---|---|---|
| Frontend UI | React or HTML/JS | **done** — HTML/JS, no build step |
| Frontend BFF | FastAPI: login, sessions, key management, proxies chat | **done** |
| API keys | issued per user, hashed at rest, shown once, `wiit_` prefix | **done** (broker) |
| Usage metering | one row per request, per-model totals for billing | **done** (broker) |
| Rate limiting | per-key, Redis counters | **done**, but local runs use `RATE_LIMIT_BACKEND=memory` — per-process, resets on restart |
| Model routing | public name → upstream, config not code | **done** — `serving/models.*.yaml` |
| Tenant cache isolation | per-tenant `cache_salt` so the KV cache is never shared | **done** (broker injects it; verified arriving upstream) |
| Model isolation | one vLLM process per model | **by config** — real on the GPU VM; in dev both names hit one upstream |
| Postgres | keys + usage | **done** |
| Serving engine | vLLM on GPU VMs | **not yet** — dev routes to HF's router (`models.dev.yaml`) or an offline mock |
| Envoy AI Gateway | owns the data plane | **not deployed** — the broker's `routers/chat.py` is the data plane today |
| Control-plane login | Auth.js / Authentik | **partial** — `dev` and `hs256` work end to end; `oidc` is not implemented here |
| Vault | holds all secrets | **not started** — secrets are in `.env` |
| TLS everywhere | user→frontend, and between tiers | **not started** |
| Network segmentation | only the frontend tier is public | **structural, not enforced** — the BFF is the broker's only client by construction; the security groups are a deploy-time job |
| OpenStack VMs | three tiers on separate VMs | **not yet** — everything runs on one laptop |

The short version: the whole request path — sign in, mint a key, authenticate,
rate limit, route, stream, meter, bill — is real and verified. What is missing
is the *production substrate*: real GPUs, Envoy, Vault, TLS and the VMs.

## What this frontend does NOT do

Everything below is marked with an `EXTEND:` comment where it belongs.

| Missing | Where | Priority |
|---|---|---|
| **TLS + `COOKIE_SECURE=true`** | a Caddy/nginx terminator in front | **blocking before exposure** |
| **OIDC login** (Authentik/Keycloak) — replaces all of `users.py` | `app/users.py`, `app/broker.py` | **high** |
| Sessions in Redis, not a dict — they die on restart and are per-process | `app/session.py` | high |
| Password reset, email verification, lockout, MFA | `app/users.py`, or free with OIDC | high |
| Rate limiting on `/api/login` | `app/routers/auth.py` | high |
| Server-side conversation history (today: this browser's localStorage only) | new table + router | medium |
| Usage date filters and per-key breakdown | needs `?since=` in the broker first | medium |
| Request-id propagation to the broker for tracing | `app/broker.py` | medium |

## Security properties already in place

- The browser never holds an API key, and never learns the broker's address.
- Session cookie is `HttpOnly`, `SameSite=Lax`, `Secure` when configured; it
  carries an opaque id only — user id, CSRF token and API key stay server-side.
- Double-submit CSRF token on every mutating call, bound to the session; a
  token from another session is rejected.
- Control-plane calls carry a JWT with `exp` ≤ 5 minutes by default, so the
  broker verifies a signature rather than trusting this tier's word.
- Tenant isolation is the broker's, and it holds through here: one user cannot
  list or revoke another's keys (verified in `scripts/smoke.py`'s sibling
  checks).
- Chat responses are written with `textContent`, never `innerHTML` — model
  output cannot inject markup.
- The session's minted key is revoked at sign-out rather than left valid and
  orphaned in the database.
- No CORS middleware here either: the pages are same-origin, so adding one
  would only let other sites drive a signed-in user's session.

---

## Deploying

```powershell
Copy-Item .env.example .env    # set BROKER_URL, AUTH_JWT_SECRET, SESSION_SECRET
docker compose up --build
```

`docker-compose.yml` binds to `127.0.0.1:8081`, expecting a TLS terminator in
front of it. See the backend repo's
[docs/INTEGRATION_PLAN.md, Phase 1](../docs/INTEGRATION_PLAN.md#phase-1--openstack)
for VM sizing and the security-group table: the frontend VM is the only one
with a public interface, and it needs egress to the gateway VM's private
address only — not to the GPU VMs.

To run **both tiers at once** during development, use the backend repo's
`docker-compose.full.yml` rather than this file; it wires this service to a
broker on the same network with `hs256` already configured on both sides.
