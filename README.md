# CodeColab — backend

Server for live collaborative coding inside VS Code. A host shares the folder
they already have open, gets a link and a Meet-style code, and admits people
one at a time. Everyone works in their own editor; there is no web IDE and no
frontend build step.

Companion extension: <https://github.com/voritsack/code_colab_extention>

---

## What it does

- **Sessions** — a host creates one from their workspace and gets
  `https://<host>/j/abc-defg-hij` plus the bare code `abc-defg-hij`.
- **Lobby** — anyone opening the link is handed off to VS Code, requests entry,
  and waits until the host approves. Guests without an account are allowed per
  session.
- **Roles** — `host`, `editor`, `viewer`. Viewers receive edits and cannot send
  them.
- **Pause / resume** — the host can freeze the session. Nothing propagates or
  is stored while paused; resuming pushes a fresh snapshot so everyone
  resynchronises.
- **Admin dashboard** — server-rendered pages at `/admin` showing live
  sessions, who is connected, which file each person has open, edit counts and
  an activity feed.

---

## Running it

```bash
cd BACK
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit it - see below
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Tables are created on first boot, and the account in `ADMIN_EMAIL` /
`ADMIN_PASSWORD` is created (or promoted) so `/admin` is reachable immediately.

**Run a single worker.** The WebSocket hub is in-process, so two workers would
put participants of the same session in different rooms. To scale past one
process, replace `app/hub.py` with a Redis pub/sub backend — nothing else has
to change.

### Configuration

Every setting is an environment variable; `.env.example` documents all of them.
The ones you must set:

| Variable | Why |
| --- | --- |
| `SECRET_KEY` | Signs every token. 32+ random characters. |
| `DATABASE_URL` | `mysql+aiomysql://…`, `postgresql+asyncpg://…` or `sqlite+aiosqlite:///./codecolab.db` |
| `PUBLIC_BASE_URL` | The origin invited people will open. `127.0.0.1` only works if everyone is on your machine. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Dashboard sign-in. |
| `VSCODE_EXTENSION_ID` | `<publisher>.<name>` from the extension, used to build `vscode://` links. |

Percent-encode `: / ? # [ ] @ + ^ !` if they appear in a database password.

---

## API

### Auth — `/api/auth`

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/register` | Returns an access + refresh pair. Disable with `ALLOW_REGISTRATION=false`. |
| POST | `/login` | Same response shape. Rate limited per IP. |
| POST | `/refresh` | Rotates: the presented refresh token is revoked. |
| POST | `/logout` | Revokes one refresh token. |
| POST | `/logout-all` | Revokes every refresh token for the caller. |
| GET | `/me` | Current user. |

### Sessions — `/api/sessions`

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `` | access | Create. Returns `join_code`, `join_url`, `vscode_link`, `session_token`. |
| GET | `/mine` | access | Sessions you host. |
| GET | `/{public_id}` | access (host) | Detail with participants. |
| PATCH | `/{public_id}` | access (host) | Title, guest policy, approval policy. |
| POST | `/{public_id}/pause` `…/resume` `…/end` | access (host) | Lifecycle. |
| PUT | `/{public_id}/files` | access (host) | Replace the stored project. |
| GET | `/{public_id}/files` | **session** | Resync snapshot. |
| POST | `/join` | access *or* none | Body `{code, display_name?}`. Returns a session token. |
| GET | `/by-code/{code}` | none | Public preview for the join page. |
| POST | `/{public_id}/participants/{id}/approve` `…/deny` `…/remove` | access (host) | |
| PATCH | `/{public_id}/participants/{id}/role` | access (host) | `editor` or `viewer`. |

### WebSocket — `/ws/session/{public_id}`

Authenticate with the **session token**, preferably as
`Authorization: Bearer <token>` (node's `ws` client can set headers, which
keeps the token out of proxy logs). `?token=` is accepted as a fallback.

Client → server: `ping`, `presence`, `file_update`, `file_delete`,
`request_snapshot`, and host-only `approve_join`, `deny_join`,
`remove_participant`, `set_role`, `pause`, `resume`, `end_session`.

Server → client: `hello`, `pending`, `approved`, `denied`, `removed`,
`join_request`, `participants`, `participant_joined`, `participant_left`,
`snapshot`, `file_update`, `file_deleted`, `presence`, `role_changed`,
`session_state`, `session_ended`, `pong`, `error`.

Close codes: `4001` unauthorised, `4002` bad message, `4003` denied,
`4004` replaced by a newer connection, `4005` removed, `4006` session ended,
`4008` too fast, `4009` message too large.

---

## Security

- **bcrypt** password hashes; login answers identically for an unknown email
  and a wrong password, so the endpoint cannot enumerate accounts.
- **Three token types.** Access tokens reach the REST API. Refresh tokens are
  stored only as a SHA-256 hash and rotate on every use. Session tokens are
  scoped to one participant in one session and are the *only* thing the
  WebSocket accepts — a leaked one cannot touch the rest of the API.
- **Path safety.** `app/utils.sanitize_relative_path` is the single choke point
  for every path that arrives from a peer. Absolute paths, `..`, drive letters,
  UNC prefixes, null bytes and reserved Windows names are refused, over REST and
  over the socket alike.
- **Payload limits** on file size, file count, snapshot size and WebSocket
  frame size, all configurable.
- **Rate limits** on register, login and join (per IP) and on WebSocket frames
  (per connection).
- **Admin dashboard** uses an httpOnly cookie plus double-submit CSRF on every
  form, and is served with `X-Frame-Options: DENY` and a CSP that forbids
  inline scripts and styles.
- CORS is **off** by default. The extension is not a browser and sends no
  `Origin`; only add origins if you build a web client.

Behind a reverse proxy, run uvicorn with `--proxy-headers` and make sure the
proxy strips inbound `X-Forwarded-For` — the rate limiter trusts it.

---

## Tests

```bash
pip install httpx websockets
python -m uvicorn app.main:app --port 8000     # in another terminal
python tests/e2e.py                            # or: python tests/e2e.py https://your-host
```

Covers registration, login, refresh rotation, session creation, snapshot
upload, path-traversal rejection, the join page, the lobby, approve/deny,
role changes, pause/resume, ending a session, and the admin dashboard
including its CSRF check. Admin credentials come from `ADMIN_EMAIL` /
`ADMIN_PASSWORD` in the environment or `.env`.

## Layout

```
app/
  main.py         app factory, middleware, lifespan
  config.py       every environment variable
  models.py       User, CollabSession, Participant, SessionFile, ActivityEvent
  db.py           async engine + session
  security.py     hashing, tokens, auth dependencies
  hub.py          in-memory WebSocket registry
  actions.py      mutations shared by REST and WebSocket
  services.py     queries and serialisation helpers
  utils.py        path sanitising, join codes
  ratelimit.py    sliding-window limiter
  routers/        auth, sessions, ws, admin, public
  templates/      admin + join pages (Jinja2)
  static/         one stylesheet, two small scripts
```

There are no migrations: `Base.metadata.create_all` runs at startup. Add
Alembic before you start changing columns on a database with real data in it.
