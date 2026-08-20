# CodeColab — backend

Server for live collaborative coding inside VS Code. A host shares the folder
they already have open, gets a link and a Meet-style code, and admits people
one at a time. Everyone works in their own editor; there is no web IDE and no
frontend build step.

Companion extension: <https://github.com/voritsack/code_colab_extention>

---

## What it does

- **No accounts.** Hosting and joining need nothing but a display name.
  Creating a session mints a host token; joining mints a participant token.
  Those tokens are the only credentials the API takes, and each is scoped to
  one session. The only accounts on the server are administrators.
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
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit it, see below
python main.py
```

`main.py` is the entry point: it reads the port from `SERVER_PORT` (or `PORT`,
or `.env`), binds `HOST`, and starts one uvicorn worker. For development with
reload, `python -m uvicorn app.main:app --reload --port 8000` still works.

Tables are created on first boot, and the account in `ADMIN_EMAIL` /
`ADMIN_PASSWORD` is created (or promoted) so `/admin` is reachable immediately.

**One worker, always.** The WebSocket hub lives in this process's memory, so
two workers would scatter the participants of one session across processes
that cannot see each other. To scale past one process, replace `app/hub.py`
with a Redis pub/sub backend — nothing else has to change.

### Deploying to a hosting panel

Panels that fetch the repository into a container and run one named file need
three things:

| Panel field | Value |
| --- | --- |
| Entry file | `main.py` |
| Port | whatever the panel assigned; it exports `SERVER_PORT` and `main.py` reads it |
| Dependencies | `requirements.txt`, installed automatically |

Then:

1. **Upload `.env`** through the panel's file manager, next to `main.py`. It is
   not in the repository and the fetch will not create it. If the panel wipes
   the folder on restart, set the same names as environment variables instead
   — real environment variables always win over the file. Without `SECRET_KEY`
   and `DATABASE_URL` the server refuses to start and says why.
2. **Set `PUBLIC_BASE_URL`** to the address people will actually open, port
   included (`http://203.0.113.10:25589`). Every invite link is built from it,
   so a wrong value produces links that go nowhere.
3. **Leave `HOST=0.0.0.0`.** Binding a loopback address inside a container
   means nothing outside can reach it.
4. **Set `TRUSTED_HOSTS`** to the host or IP you serve from. Ports are ignored
   in the comparison.
5. **Leave `TRUST_PROXY_HEADERS=false`** unless a reverse proxy terminates the
   connection in front of the server. On a directly exposed port,
   `X-Forwarded-For` is attacker controlled, and honouring it hands anyone a
   fresh rate-limit bucket per request.

A push to the deployed branch restarts the container on the new commit.

#### The proxy in front must forward WebSocket upgrades

This is the one deployment mistake that leaves the site looking healthy while
the product does not work at all. The join page renders, the admin dashboard
loads, `/healthz` returns 200 — and every session silently fails, because
collaboration is the socket.

Check it from anywhere:

```bash
curl -s -D - -o /dev/null   -H "Connection: Upgrade" -H "Upgrade: websocket"   -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="   https://your-domain/ws/session/x
```

`101 Switching Protocols` is correct. A `404` means something in front dropped
the `Upgrade` and `Connection` headers, so the request arrived as a plain GET
and no HTTP route matched it.

- **Cloudflare**: Network -> WebSockets must be on.
- **nginx**: the location block needs `proxy_http_version 1.1;`,
  `proxy_set_header Upgrade $http_upgrade;` and
  `proxy_set_header Connection "upgrade";`
- **Panels with a proxy toggle**: enable WebSocket support for the port.

`python tests/e2e.py https://your-domain` checks this first and stops with a
plain explanation if the handshake never reaches the application.

> **Serve it over TLS.** On plain HTTP the admin password, every token and the
> shared source code all travel in cleartext, and the admin cookie cannot be
> marked `Secure`. Put a domain and a certificate in front — a Cloudflare
> Tunnel or an nginx/Caddy reverse proxy both work — then set
> `PUBLIC_BASE_URL=https://...` and `TRUST_PROXY_HEADERS=true`. The server logs
> a warning at boot until you do.

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
| `HOST` | `0.0.0.0` in a container, `127.0.0.1` on a laptop. |
| `SERVER_PORT` / `PORT` | Usually injected by the host. Falls back to `PORT` in `.env`, then 8000. |
| `TRUSTED_HOSTS` | Hostnames or IPs this server answers for. Empty disables the check. |
| `TRUST_PROXY_HEADERS` | `true` only behind a reverse proxy. |

Percent-encode `: / ? # [ ] @ + ^ !` if they appear in a database password.

---

## API

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

- **Two token types, both narrow.** A session token identifies one
  participant in one session and carries their role; holding one with
  `role=host` is what makes you the host. An admin token lives in an httpOnly
  cookie and only reaches the dashboard. A leaked session token cannot touch
  anything outside its own session, and expires with it.
- **Anyone can host by default**, which is the point — but it does mean the
  server will store files for whoever finds the address. Set
  `HOST_ACCESS_CODE` to require a shared secret before a session can be
  *created*; joining is unaffected. Session creation is rate limited per IP
  either way.
- **bcrypt** for the admin password; the login form answers identically for an
  unknown email and a wrong password.
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

Behind a reverse proxy, set `TRUST_PROXY_HEADERS=true` and make sure the proxy
overwrites inbound `X-Forwarded-For`. Left at `false` the rate limiter uses the
socket address and ignores the header entirely, which is the right behaviour
for a directly exposed port.

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

## Migrating from the account-based version

Sessions used to belong to a user account. If your database predates that
change, drop the affected tables once and let the models rebuild them:

```bash
python scripts/migrate_accountless.py          # dry run
python scripts/migrate_accountless.py --yes
```

Administrator accounts are kept; sessions, participants, files and events are
not. Run it while the server is stopped, then start it back up.

## Maintenance

```bash
python scripts/cleanup.py --stale                 # end sessions nobody has touched
python scripts/cleanup.py --purge-ended 30 --yes  # drop sessions ended over a month ago
python scripts/cleanup.py --test-data --yes       # remove every non-admin account
python scripts/cleanup.py --all --yes             # back to just the admin account
```

Every run is a dry run until you add `--yes`, and prints exactly what it would
touch. Rows are removed in dependency order rather than relying on
`ON DELETE CASCADE`, so the outcome does not depend on how the schema was
created.

The server also reconciles itself on boot: the hub only exists in memory, so
after a restart it clears every stale `connected` flag and ends sessions idle
beyond `SESSION_IDLE_TIMEOUT_MINUTES`. Without that, a host who closes their
laptop leaves a session sitting on the dashboard as live for good.

## Layout

```
main.py           entry point: reads SERVER_PORT, starts one worker
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
deploy/
  nginx/          a vhost that actually forwards websocket upgrades
scripts/
  cleanup.py      stale sessions, purging, test-data removal
  migrate_accountless.py
tests/
  e2e.py          end-to-end checks against a running server
```

There are no migrations: `Base.metadata.create_all` runs at startup. Add
Alembic before you start changing columns on a database with real data in it.
