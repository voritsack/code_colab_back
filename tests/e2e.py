"""End-to-end smoke test against a running CodeColab server.

    python tests/e2e.py [base-url]

Reads ADMIN_EMAIL / ADMIN_PASSWORD from the environment (or BACK/.env) so the
admin-dashboard checks sign in with the same account the server bootstrapped.
Requires httpx and websockets:

    pip install httpx websockets
"""

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import httpx
import websockets

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
WS = BASE.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
FAILS = []


def _dotenv(name: str) -> str | None:
    """Read one key out of BACK/.env without pulling in a dependency."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return None


ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL") or _dotenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or _dotenv("ADMIN_PASSWORD")


def check(label, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(f"{label} {extra}")
    print(f"[{status}] {label} {extra if not cond else ''}".rstrip())


def skip(label, why):
    print(f"[SKIP] {label} - {why}")


async def recv_until(ws, kinds, timeout=6.0, collect=None):
    """Read frames until one of `kinds` arrives; return it."""
    seen = collect if collect is not None else []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        left = deadline - asyncio.get_event_loop().time()
        if left <= 0:
            raise TimeoutError(f"waited for {kinds}, saw {[s.get('type') for s in seen]}")
        raw = await asyncio.wait_for(ws.recv(), timeout=left)
        msg = json.loads(raw)
        seen.append(msg)
        if msg.get("type") in kinds:
            return msg


async def main():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        print("Set ADMIN_EMAIL and ADMIN_PASSWORD (or fill them in .env) first.")
        sys.exit(2)

    tag = secrets.token_hex(4)
    host_email = f"host-{tag}@example.com"
    guest_email = f"guest-{tag}@example.com"
    pw = "SuperSecret123!"

    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        # ---- registration -------------------------------------------------
        r = await c.post("/api/auth/register",
                         json={"email": host_email, "name": "Ada Host", "password": pw})
        check("register host", r.status_code == 201, r.text[:200])
        host_tok = r.json()["access_token"]
        host_refresh = r.json()["refresh_token"]
        H = {"Authorization": f"Bearer {host_tok}"}

        r = await c.post("/api/auth/register",
                         json={"email": guest_email, "name": "Grace Guest", "password": pw})
        check("register guest", r.status_code == 201, r.text[:200])
        guest_tok = r.json()["access_token"]
        G = {"Authorization": f"Bearer {guest_tok}"}

        # duplicate email rejected
        r = await c.post("/api/auth/register",
                         json={"email": host_email, "name": "x", "password": pw})
        if r.status_code == 429:
            skip("duplicate email -> 409",
                 "register rate limit reached; raise REGISTER_RATE_LIMIT to re-test")
        else:
            check("duplicate email -> 409", r.status_code == 409, str(r.status_code))

        # wrong password rejected
        r = await c.post("/api/auth/login", json={"email": host_email, "password": "nope"})
        check("bad password -> 401", r.status_code == 401, str(r.status_code))

        # refresh rotation
        r = await c.post("/api/auth/refresh", json={"refresh_token": host_refresh})
        check("refresh works", r.status_code == 200, r.text[:200])
        r2 = await c.post("/api/auth/refresh", json={"refresh_token": host_refresh})
        check("refresh reuse rejected", r2.status_code == 401, str(r2.status_code))

        # no token -> 401
        r = await c.get("/api/auth/me")
        check("unauth /me -> 401", r.status_code == 401, str(r.status_code))

        # ---- session creation ---------------------------------------------
        r = await c.post("/api/sessions", headers=H,
                         json={"title": "Lesson 1", "workspace_name": "demo-proj",
                               "allow_guests": True, "require_approval": True})
        check("create session", r.status_code == 201, r.text[:300])
        s = r.json()
        pid, code = s["public_id"], s["join_code"]
        host_session_token = s["session_token"]
        check("join code shape", len(code.split("-")) == 3, code)
        check("join url", s["join_url"].endswith("/j/" + code), s["join_url"])
        check("vscode link", s["vscode_link"].startswith("vscode://local.codecolab/join"),
              s["vscode_link"])

        # ---- snapshot upload ----------------------------------------------
        r = await c.put(f"/api/sessions/{pid}/files", headers=H, json={"files": [
            {"path": "src/main.py", "content": "print('hello')\n"},
            {"path": "README.md", "content": "# demo\n"},
        ]})
        check("upload snapshot", r.status_code == 200, r.text[:200])

        # path traversal rejected
        r = await c.put(f"/api/sessions/{pid}/files", headers=H, json={"files": [
            {"path": "../../etc/passwd", "content": "x"}]})
        check("traversal rejected", r.status_code == 422, str(r.status_code))
        r = await c.put(f"/api/sessions/{pid}/files", headers=H, json={"files": [
            {"path": "C:/Windows/system.ini", "content": "x"}]})
        check("drive path rejected", r.status_code == 422, str(r.status_code))

        # non-host cannot upload
        r = await c.put(f"/api/sessions/{pid}/files", headers=G, json={"files": []})
        check("non-host upload -> 403", r.status_code == 403, str(r.status_code))

        # ---- public join page ---------------------------------------------
        r = await c.get(f"/j/{code}")
        check("join page 200", r.status_code == 200, str(r.status_code))
        check("join page shows title", "Lesson 1" in r.text)
        check("join page has deep link", "vscode://local.codecolab/join" in r.text)
        r = await c.get("/j/zzz-zzzz-zzz")
        check("unknown code page", r.status_code == 200 and "not live" in r.text)

        # ---- guest joins ---------------------------------------------------
        r = await c.post("/api/sessions/join", headers=G,
                         json={"code": code.upper().replace("-", "")})
        check("join by normalised code", r.status_code == 200, r.text[:300])
        j = r.json()
        check("guest is pending", j["state"] == "pending", j["state"])
        guest_session_token = j["session_token"]
        guest_pid = j["participant_id"]

        r = await c.post("/api/sessions/join", json={"code": "aaa-bbbb-ccc"})
        check("bad code -> 404", r.status_code == 404, str(r.status_code))

        # anonymous guest without a name
        r = await c.post("/api/sessions/join", json={"code": code})
        check("anon without name -> 400", r.status_code == 400, str(r.status_code))
        r = await c.post("/api/sessions/join",
                         json={"code": code, "display_name": "Walk In"})
        check("anon guest with name ok", r.status_code == 200, r.text[:200])
        anon_token = r.json()["session_token"]
        anon_pid = r.json()["participant_id"]

        # session token must not work on the user API
        r = await c.get("/api/auth/me",
                        headers={"Authorization": f"Bearer {guest_session_token}"})
        check("session token rejected on /me", r.status_code == 401, str(r.status_code))

        # pending guest may not download files
        r = await c.get(f"/api/sessions/{pid}/files",
                        headers={"Authorization": f"Bearer {guest_session_token}"})
        check("pending download -> 403", r.status_code == 403, str(r.status_code))

    # ---- websockets --------------------------------------------------------
    url = f"{WS}/ws/session/{pid}"
    host_hdr = {"Authorization": f"Bearer {host_session_token}"}
    guest_hdr = {"Authorization": f"Bearer {guest_session_token}"}

    # bad token is refused
    try:
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer junk"}) as bad:
            await asyncio.wait_for(bad.recv(), timeout=4)
        check("bad ws token refused", False, "connection stayed open")
    except Exception as exc:
        check("bad ws token refused", True, type(exc).__name__)

    async with websockets.connect(url, additional_headers=host_hdr) as hws:
        hello = await recv_until(hws, {"hello"})
        check("host hello", hello["you"]["role"] == "host", str(hello["you"]))
        check("host sees join code", hello["session"]["join_code"] == code)
        await recv_until(hws, {"snapshot"})

        async with websockets.connect(url, additional_headers=guest_hdr) as gws:
            g_hello = await recv_until(gws, {"hello"})
            check("guest hello", g_hello["you"]["state"] == "pending", str(g_hello["you"]))
            await recv_until(gws, {"pending"})

            req = await recv_until(hws, {"join_request"})
            check("host got join_request",
                  req["participant"]["participant_id"] == guest_pid, str(req))

            # guest cannot edit while pending
            await gws.send(json.dumps({"type": "file_update",
                                       "path": "src/main.py", "content": "hax"}))
            err = await recv_until(gws, {"error"})
            check("pending guest edit blocked", err["code"] == "forbidden", str(err))

            # guest cannot issue host commands
            await gws.send(json.dumps({"type": "pause"}))
            err = await recv_until(gws, {"error"})
            check("guest pause blocked", err["code"] == "forbidden", str(err))

            # host approves as editor
            await hws.send(json.dumps({"type": "approve_join",
                                       "participant_id": guest_pid, "role": "editor"}))
            ok = await recv_until(gws, {"approved"})
            check("guest approved as editor", ok["role"] == "editor", str(ok))
            snap = await recv_until(gws, {"snapshot"})
            paths = sorted(f["path"] for f in snap["files"])
            check("snapshot delivered", paths == ["README.md", "src/main.py"], str(paths))

            # guest edit propagates to host
            await gws.send(json.dumps({"type": "file_update",
                                       "path": "src/main.py",
                                       "content": "print('edited by guest')\n"}))
            upd = await recv_until(hws, {"file_update"})
            check("edit reached host", "edited by guest" in upd["content"], str(upd)[:120])

            # traversal over the socket is refused
            await gws.send(json.dumps({"type": "file_update",
                                       "path": "../../../evil.txt", "content": "x"}))
            err = await recv_until(gws, {"error"})
            check("ws traversal blocked", err["code"] == "bad_path", str(err))

            # oversized file refused
            await gws.send(json.dumps({"type": "file_update", "path": "big.txt",
                                       "content": "x" * 600_000}))
            err = await recv_until(gws, {"error"})
            check("oversized file refused", err["code"] == "file_too_large", str(err))

            # presence
            await gws.send(json.dumps({"type": "presence", "path": "README.md",
                                       "line": 3, "column": 1}))
            pres = await recv_until(hws, {"presence"})
            check("presence relayed", pres["path"] == "README.md", str(pres))

            # pause blocks edits
            await hws.send(json.dumps({"type": "pause"}))
            st = await recv_until(gws, {"session_state"})
            check("guest told paused", st["status"] == "paused", str(st))
            await gws.send(json.dumps({"type": "file_update",
                                       "path": "src/main.py", "content": "while paused"}))
            err = await recv_until(gws, {"error"})
            check("edit blocked while paused", err["code"] == "paused", str(err))

            # resume re-syncs
            await hws.send(json.dumps({"type": "resume"}))
            st = await recv_until(gws, {"session_state"})
            check("guest told active", st["status"] == "active", str(st))
            snap = await recv_until(gws, {"snapshot"})
            main_py = next(f for f in snap["files"] if f["path"] == "src/main.py")
            check("resume snapshot has last good content",
                  "edited by guest" in main_py["content"], main_py["content"][:60])

            # role demotion
            await hws.send(json.dumps({"type": "set_role",
                                       "participant_id": guest_pid, "role": "viewer"}))
            rc = await recv_until(gws, {"role_changed"})
            check("demoted to viewer", rc["role"] == "viewer", str(rc))
            await gws.send(json.dumps({"type": "file_update",
                                       "path": "src/main.py", "content": "viewer edit"}))
            err = await recv_until(gws, {"error"})
            check("viewer edit blocked", err["code"] == "forbidden", str(err))

        # anonymous guest gets denied
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {anon_token}"}
        ) as aws:
            await recv_until(aws, {"pending"})
            await recv_until(hws, {"join_request"})
            await hws.send(json.dumps({"type": "deny_join", "participant_id": anon_pid}))
            msg = await recv_until(aws, {"denied"})
            check("anon denied", msg["type"] == "denied", str(msg))

        # end the session
        await hws.send(json.dumps({"type": "end_session"}))
        await asyncio.sleep(1.0)

    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        H = {"Authorization": f"Bearer {host_tok}"}
        r = await c.get(f"/api/sessions/{pid}", headers=H)
        check("session ended", r.json()["status"] == "ended", r.json()["status"])
        r = await c.post("/api/sessions/join", headers=G, json={"code": code})
        check("cannot join ended session", r.status_code == 404, str(r.status_code))

        # ---- admin dashboard ---------------------------------------------
        async with httpx.AsyncClient(base_url=BASE, timeout=20,
                                     follow_redirects=False) as ac:
            r = await ac.get("/admin")
            check("admin needs auth", r.status_code in (303, 401), str(r.status_code))

            r = await ac.get("/admin/login")
            check("login page", r.status_code == 200, str(r.status_code))
            csrf = ac.cookies.get("codecolab_csrf")
            check("csrf cookie set", bool(csrf))

            r = await ac.post("/admin/login", data={
                "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                "csrf_token": csrf})
            check("admin login", r.status_code == 303 and r.headers["location"] == "/admin",
                  f"{r.status_code} {r.headers.get('location')}")

            r = await ac.post("/admin/login", data={
                "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                "csrf_token": "wrong"})
            check("csrf enforced", r.status_code == 403, str(r.status_code))

            r = await ac.get("/admin")
            check("dashboard renders", r.status_code == 200 and "Live overview" in r.text,
                  str(r.status_code))
            r = await ac.get("/admin/api/stats")
            check("stats json", r.status_code == 200 and "totals" in r.json(),
                  str(r.status_code))
            r = await ac.get(f"/admin/sessions/{pid}")
            check("session page", r.status_code == 200 and "Lesson 1" in r.text,
                  str(r.status_code))
            r = await ac.get("/admin/users")
            check("users page", r.status_code == 200 and host_email in r.text,
                  str(r.status_code))

            r = await ac.post("/admin/login", data={
                "email": host_email, "password": pw, "csrf_token": ac.cookies.get("codecolab_csrf")})
            check("non-admin cannot sign in",
                  r.status_code == 303 and "error" in (r.headers.get("location") or ""),
                  r.headers.get("location"))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


asyncio.run(main())
