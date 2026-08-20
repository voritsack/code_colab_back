"""End-to-end smoke test against a running CodeColab server.

    python tests/e2e.py [base-url]

There are no user accounts: creating a session mints a host token, joining
mints a participant token, and those tokens are the only credentials the API
takes. Admin credentials come from ADMIN_EMAIL / ADMIN_PASSWORD in the
environment or BACK/.env, and only reach the dashboard.

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


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def recv_until(ws, kinds, timeout=8.0, collect=None):
    seen = collect if collect is not None else []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        left = deadline - asyncio.get_event_loop().time()
        if left <= 0:
            raise TimeoutError(f"waited for {kinds}, saw {[s.get('type') for s in seen]}")
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
        seen.append(msg)
        if msg.get("type") in kinds:
            return msg


async def main():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        print("Set ADMIN_EMAIL and ADMIN_PASSWORD (or fill them in .env) first.")
        sys.exit(2)

    tag = secrets.token_hex(3)

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # ---- no accounts anywhere -----------------------------------------
        for path in ("/api/auth/login", "/api/auth/register", "/api/auth/me"):
            r = await c.post(path, json={})
            check(f"{path} is gone", r.status_code == 404, str(r.status_code))

        r = await c.get("/api/info")
        check("info advertises no registration", "allow_registration" not in r.json())

        # ---- hosting needs nothing but a name ------------------------------
        r = await c.post("/api/sessions", json={
            "title": f"Lesson {tag}", "display_name": "Ada Host",
            "workspace_name": "demo", "allow_guests": True, "require_approval": True})
        check("create session without signing in", r.status_code == 201, r.text[:300])
        s = r.json()
        pid, code, host_token = s["public_id"], s["join_code"], s["session_token"]
        check("host name recorded", s["host_name"] == "Ada Host", s["host_name"])
        check("join code shape", len(code.split("-")) == 3, code)
        check("vscode link", s["vscode_link"].startswith("vscode://"), s["vscode_link"])

        r = await c.post("/api/sessions", json={"title": "x", "display_name": "a"})
        check("display name must be usable", r.status_code == 422, str(r.status_code))

        # ---- host actions are gated by the host token ----------------------
        r = await c.put(f"/api/sessions/{pid}/files", json={"files": [
            {"path": "src/main.py", "content": "print('hello')\n"},
            {"path": "README.md", "content": "# demo\n"}]}, headers=bearer(host_token))
        check("upload snapshot", r.status_code == 200, r.text[:200])

        r = await c.put(f"/api/sessions/{pid}/files", json={"files": []})
        check("snapshot needs a token", r.status_code == 401, str(r.status_code))

        r = await c.put(f"/api/sessions/{pid}/files", headers=bearer(host_token), json={
            "files": [{"path": "../../etc/passwd", "content": "x"}]})
        check("traversal rejected", r.status_code == 422, str(r.status_code))
        r = await c.put(f"/api/sessions/{pid}/files", headers=bearer(host_token), json={
            "files": [{"path": "C:/Windows/system.ini", "content": "x"}]})
        check("drive path rejected", r.status_code == 422, str(r.status_code))

        r = await c.get(f"/api/sessions/{pid}", headers=bearer(host_token))
        check("host can read the session", r.status_code == 200, str(r.status_code))

        # ---- join page ------------------------------------------------------
        r = await c.get(f"/j/{code}")
        check("join page shows the session", r.status_code == 200 and f"Lesson {tag}" in r.text)
        r = await c.get("/j/zzz-zzzz-zzz")
        check("unknown code page", r.status_code == 200 and "not live" in r.text)

        # ---- joining --------------------------------------------------------
        r = await c.post("/api/sessions/join", json={
            "code": code.upper().replace("-", ""), "display_name": "Grace Guest"})
        check("join by normalised code", r.status_code == 200, r.text[:300])
        j = r.json()
        check("guest waits for approval", j["state"] == "pending", j["state"])
        guest_token, guest_pid = j["session_token"], j["participant_id"]

        r = await c.post("/api/sessions/join", json={"code": code})
        check("join needs a display name", r.status_code == 422, str(r.status_code))
        r = await c.post("/api/sessions/join", json={"code": "aaa-bbbb-ccc", "display_name": "Nobody"})
        check("unknown code -> 404", r.status_code == 404, str(r.status_code))

        r = await c.post("/api/sessions/join", json={"code": code, "display_name": "Walk In"})
        check("second guest accepted", r.status_code == 200, r.text[:200])
        anon_token, anon_pid = r.json()["session_token"], r.json()["participant_id"]

        # A guest token must not be able to act as the host.
        r = await c.post(f"/api/sessions/{pid}/pause", headers=bearer(guest_token))
        check("guest cannot pause over REST", r.status_code == 403, str(r.status_code))
        r = await c.put(f"/api/sessions/{pid}/files", headers=bearer(guest_token), json={"files": []})
        check("guest cannot replace the project", r.status_code == 403, str(r.status_code))
        r = await c.get(f"/api/sessions/{pid}/files", headers=bearer(guest_token))
        check("pending guest cannot read files", r.status_code == 403, str(r.status_code))

    # ---- websockets ---------------------------------------------------------
    url = f"{WS}/ws/session/{pid}"
    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": "Bearer junk"}, open_timeout=15
        ) as bad:
            await asyncio.wait_for(bad.recv(), timeout=4)
        check("bad ws token refused", False, "connection stayed open")
    except websockets.exceptions.InvalidStatus as exc:
        code_ = exc.response.status_code
        check("websocket upgrade reaches the app", False,
              f"handshake rejected with HTTP {code_}; the proxy in front of {BASE} "
              f"is not upgrading websocket connections")
        print()
        print("WebSocket transport is broken at this origin - stopping here.")
        print("Everything past this point needs a working socket.")
        sys.exit(1)
    except websockets.exceptions.ConnectionClosed as exc:
        check("bad ws token refused", exc.code == 4001, f"closed with {exc.code}")

    async with websockets.connect(url, additional_headers=bearer(host_token)) as hws:
        hello = await recv_until(hws, {"hello"})
        check("host hello", hello["you"]["role"] == "host", str(hello["you"]))
        check("host sees the join code", hello["session"]["join_code"] == code)
        await recv_until(hws, {"snapshot"})

        async with websockets.connect(url, additional_headers=bearer(guest_token)) as gws:
            g_hello = await recv_until(gws, {"hello"})
            check("guest hello", g_hello["you"]["state"] == "pending", str(g_hello["you"]))
            await recv_until(gws, {"pending"})

            req = await recv_until(hws, {"join_request"})
            check("host got join_request",
                  req["participant"]["participant_id"] == guest_pid, str(req))

            await gws.send(json.dumps({"type": "file_update", "path": "src/main.py", "content": "hax"}))
            err = await recv_until(gws, {"error"})
            check("pending guest cannot edit", err["code"] == "forbidden", str(err))

            await gws.send(json.dumps({"type": "pause"}))
            err = await recv_until(gws, {"error"})
            check("guest cannot pause over the socket", err["code"] == "forbidden", str(err))

            await hws.send(json.dumps({
                "type": "approve_join", "participant_id": guest_pid, "role": "editor"}))
            ok = await recv_until(gws, {"approved"})
            check("guest admitted as editor", ok["role"] == "editor", str(ok))
            snap = await recv_until(gws, {"snapshot"})
            paths = sorted(f["path"] for f in snap["files"])
            check("snapshot delivered", paths == ["README.md", "src/main.py"], str(paths))

            await gws.send(json.dumps({
                "type": "file_update", "path": "src/main.py",
                "content": "print('edited by guest')\n"}))
            upd = await recv_until(hws, {"file_update"})
            check("edit reached the host", "edited by guest" in upd["content"])

            await gws.send(json.dumps({
                "type": "file_update", "path": "../../../evil.txt", "content": "x"}))
            err = await recv_until(gws, {"error"})
            check("ws traversal blocked", err["code"] == "bad_path", str(err))

            await gws.send(json.dumps({
                "type": "file_update", "path": "big.txt", "content": "x" * 600_000}))
            err = await recv_until(gws, {"error"})
            check("oversized file refused", err["code"] == "file_too_large", str(err))

            await gws.send(json.dumps({"type": "ping", "t": 1}))
            await recv_until(gws, {"pong"})
            check("heartbeat answered", True)

            await hws.send(json.dumps({"type": "pause"}))
            st = await recv_until(gws, {"session_state"})
            check("guest told paused", st["status"] == "paused", str(st))
            await gws.send(json.dumps({
                "type": "file_update", "path": "src/main.py", "content": "while paused"}))
            err = await recv_until(gws, {"error"})
            check("edit blocked while paused", err["code"] == "paused", str(err))

            await hws.send(json.dumps({"type": "resume"}))
            st = await recv_until(gws, {"session_state"})
            check("guest told active", st["status"] == "active", str(st))
            snap = await recv_until(gws, {"snapshot"})
            main_py = next(f for f in snap["files"] if f["path"] == "src/main.py")
            check("resume resends the last good content", "edited by guest" in main_py["content"])

            await hws.send(json.dumps({
                "type": "set_role", "participant_id": guest_pid, "role": "viewer"}))
            rc = await recv_until(gws, {"role_changed"})
            check("demoted to viewer", rc["role"] == "viewer", str(rc))
            await gws.send(json.dumps({
                "type": "file_update", "path": "src/main.py", "content": "viewer edit"}))
            err = await recv_until(gws, {"error"})
            check("viewer edit blocked", err["code"] == "forbidden", str(err))

        async with websockets.connect(url, additional_headers=bearer(anon_token)) as aws:
            await recv_until(aws, {"pending"})
            await recv_until(hws, {"join_request"})
            await hws.send(json.dumps({"type": "deny_join", "participant_id": anon_pid}))
            msg = await recv_until(aws, {"denied"})
            check("refused guest told why", msg["type"] == "denied", str(msg))

        await hws.send(json.dumps({"type": "end_session"}))
        await asyncio.sleep(1.0)

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        r = await c.get(f"/api/sessions/{pid}", headers=bearer(host_token))
        check("ended session rejects its own token", r.status_code == 401, str(r.status_code))
        r = await c.post("/api/sessions/join", json={"code": code, "display_name": "Too Late"})
        check("cannot join an ended session", r.status_code == 404, str(r.status_code))

        # ---- admin dashboard ------------------------------------------------
        async with httpx.AsyncClient(base_url=BASE, timeout=30,
                                     follow_redirects=False) as ac:
            r = await ac.get("/admin")
            check("admin needs auth", r.status_code in (303, 401), str(r.status_code))

            r = await ac.get("/admin/login")
            check("login page", r.status_code == 200, str(r.status_code))
            csrf = ac.cookies.get("codecolab_csrf")
            check("csrf cookie set", bool(csrf))

            r = await ac.post("/admin/login", data={
                "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": csrf})
            check("admin login", r.status_code == 303 and r.headers["location"] == "/admin",
                  f"{r.status_code} {r.headers.get('location')}")

            r = await ac.post("/admin/login", data={
                "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": "wrong"})
            check("csrf enforced", r.status_code == 403, str(r.status_code))

            r = await ac.get("/admin")
            check("dashboard renders", r.status_code == 200 and "Live overview" in r.text)
            r = await ac.get("/admin/api/stats")
            check("stats json", r.status_code == 200 and "totals" in r.json())
            r = await ac.get(f"/admin/sessions/{pid}")
            check("session page shows the host name",
                  r.status_code == 200 and "Ada Host" in r.text, str(r.status_code))
            r = await ac.get("/admin/users")
            check("only admins are listed",
                  r.status_code == 200 and "Administrators" in r.text and "Ada Host" not in r.text)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


asyncio.run(main())
