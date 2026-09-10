# WIIT LLMaaS — Frontend VM

```
user ──TLS──► frontend VM ──private network──► gateway/broker VM ──► GPU VM(s)
              THIS REPO                        broker + Postgres      vLLM
              pages + BFF                      + Redis
```

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
