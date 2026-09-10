# WIIT LLMaaS — Frontend VM

```
user ──TLS──► frontend VM ──private network──► gateway/broker VM ──► GPU VM(s)
              THIS REPO                        broker + Postgres      vLLM
              pages + BFF                      + Redis
```

## How this repo relates to the backend

The backend lives at
[`virginiaceccatelli/llmaas-backend`](https://github.com/virginiaceccatelli/llmaas-backend)
and **consumes this repo as a git submodule** at `frontend/`. That is the only
mechanical link between the two, and it is deliberately one-directional: the
backend repo pins which commit of this one it was verified against.

Practical consequences:

- **This repo does not, and should not, contain a submodule of the backend.**
  Two repos pointing at each other is a cycle, and there would then be two
  competing answers to "which commit pair is verified?".
- **The cross-repo tests live in the backend repo**, not here:
  `.github/workflows/contract.yml` brings up both tiers and runs
  `scripts/smoke.py` from *this* repo against that broker.
- **Merging a change here is half a change.** After pushing, bump the pointer
  in the backend repo (`git add frontend && git commit`), or CI keeps testing
  the version before your fix.
- The shared contract — JWT claims, the settings that must match on both sides
  — is written down at
  [`contracts/control_token.md`](https://github.com/virginiaceccatelli/llmaas-backend/blob/main/contracts/control_token.md).

## Layout

```
.
├── app/
│   ├── main.py            app wiring + lifespan + startup validation
│   ├── config.py          env-driven settings
│   ├── session.py         cookie sessions, CSRF, the session store
│   ├── users.py           login: scrypt password hashes, or dev passwordless
│   ├── broker.py          the ONLY thing that talks to the broker; mints JWTs
│   ├── apikeys.py         the session's data-plane key: mint, adopt, revoke
│   ├── routers/
│   │   ├── pages.py         /, /keys, /usage, /login  (real URLs, guarded)
│   │   ├── auth.py          /api/login, /api/logout, /api/me, /api/health
│   │   ├── keys.py          /api/keys…, /api/usage    -> broker control plane
│   │   └── chat.py          /api/chat, /api/models    -> broker data plane
│   └── static/            chat.html, keys.html, usage.html, login.html,
│                          app.js (shell, SSE reader, chat store), style.css
├── scripts/
│   ├── smoke.py           end-to-end check of every route (stdlib only)
│   └── users.py           add/list/remove users in users.json
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
