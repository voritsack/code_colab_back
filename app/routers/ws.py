"""The collaboration socket.

One connection per participant. The session token in the ``Authorization``
header decides who you are; everything after that is authorisation:

* pending participants sit in a lobby and receive nothing but their own status
* viewers receive edits but may not send them
* editors and the host may send edits, but not while the session is paused
* only the host may admit, remove, promote, pause, resume or end
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from ..actions import (
    CLOSE_SESSION_ENDED,
    apply_file_update,
    approve_participant,
    broadcast_roster,
    change_role,
    delete_file,
    deny_participant,
    remove_participant,
    send_snapshot,
    set_status,
)
from ..config import settings
from ..db import SessionLocal
from ..hub import Connection, hub
from ..models import (
    ROLE_EDITOR,
    ROLE_HOST,
    ROLE_VIEWER,
    STATE_APPROVED,
    STATE_LEFT,
    STATE_PENDING,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    CollabSession,
    Participant,
    utcnow,
)
from ..security import SessionAuthError, resolve_session_token
from ..services import log_event, participant_payload
from ..utils import UnsafePathError, sanitize_relative_path

router = APIRouter(tags=["websocket"])

# Close codes the extension knows how to explain to the user.
CLOSE_UNAUTHORIZED = 4001
CLOSE_BAD_MESSAGE = 4002
CLOSE_TOO_FAST = 4008
CLOSE_TOO_LARGE = 4009

HOST_COMMANDS = {
    "approve_join",
    "deny_join",
    "remove_participant",
    "set_role",
    "pause",
    "resume",
    "end_session",
}


def _extract_token(websocket: WebSocket) -> str | None:
    """Prefer the Authorization header; fall back to a query parameter.

    The extension uses the header (node's ``ws`` client can set one), which
    keeps the token out of proxy access logs. The query parameter exists only
    for clients that cannot.
    """
    header = websocket.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    protocol = websocket.headers.get("sec-websocket-protocol") or ""
    for part in (p.strip() for p in protocol.split(",")):
        if part.startswith("bearer."):
            return part[len("bearer.") :]

    token = websocket.query_params.get("token")
    return token.strip() if token else None


class MessageBudget:
    """Per-connection flood guard."""

    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True


@router.websocket("/ws/session/{public_id}")
async def session_socket(websocket: WebSocket, public_id: str) -> None:
    await websocket.accept()

    token = _extract_token(websocket)
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Session token required")
        return

    async with SessionLocal() as db:
        try:
            participant, session = await resolve_session_token(db, token)
        except SessionAuthError as exc:
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason=exc.reason[:120])
            return

        if session.public_id != public_id:
            await websocket.close(
                code=CLOSE_UNAUTHORIZED, reason="Token is for another session"
            )
            return

        conn = Connection(
            websocket=websocket,
            participant_id=participant.id,
            session_public_id=session.public_id,
            display_name=participant.display_name,
            role=participant.role,
            approved=participant.state == STATE_APPROVED,
            active_file=participant.active_file,
            edits=participant.edits,
        )
        await hub.register(conn)
        hub.set_room_status(session.public_id, session.status)

        participant.connected = True
        participant.last_seen_at = utcnow()
        await db.commit()

        try:
            await _greet(db, conn, participant, session)
            await _pump(db, conn, session)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - never let one socket kill the server
            await hub.send(
                conn, {"type": "error", "code": "internal", "message": "Server error"}
            )
        finally:
            await _farewell(db, conn, session)


async def _greet(
    db: AsyncSession,
    conn: Connection,
    participant: Participant,
    session: CollabSession,
) -> None:
    await hub.send(
        conn,
        {
            "type": "hello",
            "you": {
                "participant_id": participant.id,
                "display_name": participant.display_name,
                "role": participant.role,
                "state": participant.state,
            },
            "session": {
                "public_id": session.public_id,
                "title": session.title,
                "join_code": session.join_code if conn.is_host else None,
                "status": session.status,
                "workspace_name": session.workspace_name,
                "require_approval": session.require_approval,
                "allow_guests": session.allow_guests,
            },
            "limits": {
                "max_file_bytes": settings.max_file_bytes,
                "max_message_bytes": settings.max_ws_message_bytes,
            },
        },
    )

    if participant.state == STATE_PENDING:
        await hub.send(
            conn,
            {"type": "pending", "message": "Waiting for the host to let you in"},
        )
        await hub.notify_hosts(
            session.public_id,
            {
                "type": "join_request",
                "participant": {
                    "participant_id": participant.id,
                    "display_name": participant.display_name,
                },
            },
        )
    else:
        await send_snapshot(db, session, participant.id)
        await hub.broadcast(
            session.public_id,
            {
                "type": "participant_joined",
                "participant": participant_payload(participant),
            },
            exclude_participant=participant.id,
        )

    await broadcast_roster(db, session)
    # Close the read transaction: an idle socket should hold no database
    # connection, and the next frame must not read a stale snapshot.
    await db.commit()


async def _farewell(
    db: AsyncSession, conn: Connection, session: CollabSession
) -> None:
    await hub.unregister(conn)

    participant = await db.get(Participant, conn.participant_id)
    if participant is not None:
        participant.connected = False
        participant.last_seen_at = utcnow()
        if participant.state == STATE_PENDING:
            # Someone who closed the lobby before being admitted is simply gone.
            participant.state = STATE_LEFT
            participant.left_at = utcnow()
        await db.commit()

    await hub.broadcast(
        session.public_id,
        {"type": "participant_left", "participant_id": conn.participant_id},
    )
    await broadcast_roster(db, session)

    if not hub.connections(session.public_id):
        hub.forget_room_status(session.public_id)


async def _pump(db: AsyncSession, conn: Connection, session: CollabSession) -> None:
    budget = MessageBudget(
        settings.ws_message_rate_limit, settings.ws_message_rate_window_seconds
    )

    while True:
        raw = await conn.websocket.receive_text()

        if len(raw.encode("utf-8", "ignore")) > settings.max_ws_message_bytes:
            await hub.send(
                conn, {"type": "error", "code": "too_large", "message": "Message too big"}
            )
            await conn.websocket.close(code=CLOSE_TOO_LARGE, reason="Message too big")
            return

        if not budget.allow():
            await conn.websocket.close(code=CLOSE_TOO_FAST, reason="Slow down")
            return

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await hub.send(
                conn, {"type": "error", "code": "bad_json", "message": "Malformed frame"}
            )
            continue

        if not isinstance(message, dict):
            await hub.send(
                conn, {"type": "error", "code": "bad_frame", "message": "Expected an object"}
            )
            continue

        conn.last_seen_at = utcnow()
        try:
            await _dispatch(db, conn, session, message)
        finally:
            # One transaction per frame. Without this the connection would
            # keep the snapshot it opened with and stop seeing other
            # participants' commits.
            await db.commit()


async def _dispatch(
    db: AsyncSession,
    conn: Connection,
    session: CollabSession,
    message: dict[str, Any],
) -> None:
    kind = str(message.get("type") or "")

    if kind == "ping":
        await hub.send(conn, {"type": "pong", "t": message.get("t")})
        return

    if kind in HOST_COMMANDS:
        if not conn.is_host:
            await _deny(conn, "Only the host can do that")
            return
        await _host_command(db, conn, session, kind, message)
        return

    if not conn.approved:
        await _deny(conn, "You have not been admitted yet")
        return

    if kind == "request_snapshot":
        await send_snapshot(db, session, conn.participant_id)
        return

    if kind == "presence":
        await _presence(db, conn, session, message)
        return

    if kind in ("file_update", "file_delete"):
        await _edit(db, conn, session, kind, message)
        return

    await hub.send(
        conn, {"type": "error", "code": "unknown_type", "message": f"Unknown type {kind}"}
    )


async def _deny(conn: Connection, message: str) -> None:
    await hub.send(conn, {"type": "error", "code": "forbidden", "message": message})


async def _presence(
    db: AsyncSession,
    conn: Connection,
    session: CollabSession,
    message: dict[str, Any],
) -> None:
    raw_path = message.get("path")
    path: str | None = None
    if isinstance(raw_path, str) and raw_path:
        try:
            path = sanitize_relative_path(raw_path)
        except UnsafePathError:
            return

    conn.active_file = path
    participant = await db.get(Participant, conn.participant_id)
    if participant is not None:
        participant.active_file = path
        participant.last_seen_at = utcnow()
        await db.commit()

    await hub.broadcast(
        session.public_id,
        {
            "type": "presence",
            "participant_id": conn.participant_id,
            "display_name": conn.display_name,
            "path": path,
            "line": message.get("line"),
            "column": message.get("column"),
        },
        exclude_participant=conn.participant_id,
    )


async def _edit(
    db: AsyncSession,
    conn: Connection,
    session: CollabSession,
    kind: str,
    message: dict[str, Any],
) -> None:
    if conn.role not in (ROLE_HOST, ROLE_EDITOR):
        await _deny(conn, "You are in view-only mode")
        return

    status = hub.room_status(session.public_id, STATUS_ACTIVE)
    if status == STATUS_PAUSED:
        await hub.send(
            conn,
            {"type": "error", "code": "paused", "message": "The session is paused"},
        )
        return
    if status == STATUS_ENDED:
        await conn.websocket.close(code=CLOSE_SESSION_ENDED, reason="Session ended")
        return

    try:
        path = sanitize_relative_path(str(message.get("path") or ""))
    except UnsafePathError as exc:
        await hub.send(
            conn, {"type": "error", "code": "bad_path", "message": str(exc)}
        )
        return

    participant = await db.get(Participant, conn.participant_id)
    if participant is None:
        return

    if kind == "file_delete":
        frame = await delete_file(db, session, participant, path)
    else:
        content = message.get("content")
        if not isinstance(content, str):
            await hub.send(
                conn,
                {"type": "error", "code": "bad_content", "message": "content must be a string"},
            )
            return
        if len(content.encode("utf-8")) > settings.max_file_bytes:
            await hub.send(
                conn,
                {
                    "type": "error",
                    "code": "file_too_large",
                    "message": f"File exceeds {settings.max_file_bytes} bytes",
                    "path": path,
                },
            )
            return
        frame = await apply_file_update(db, session, participant, path, content)
        conn.edits = participant.edits
        conn.active_file = path

    await db.commit()
    await hub.broadcast(
        session.public_id, frame, exclude_participant=conn.participant_id
    )


async def _host_command(
    db: AsyncSession,
    conn: Connection,
    session: CollabSession,
    kind: str,
    message: dict[str, Any],
) -> None:
    actor = conn.display_name

    if kind == "pause":
        await set_status(db, session, STATUS_PAUSED, actor=actor)
        hub.set_room_status(session.public_id, STATUS_PAUSED)
        return
    if kind == "resume":
        await set_status(db, session, STATUS_ACTIVE, actor=actor)
        hub.set_room_status(session.public_id, STATUS_ACTIVE)
        return
    if kind == "end_session":
        await set_status(db, session, STATUS_ENDED, actor=actor)
        hub.set_room_status(session.public_id, STATUS_ENDED)
        await log_event(
            db, kind="session.ended", message=session.title, actor=actor, session=session
        )
        await db.commit()
        return

    raw_id = message.get("participant_id")
    try:
        target_id = int(raw_id)
    except (TypeError, ValueError):
        await _deny(conn, "participant_id is required")
        return

    target = await db.get(Participant, target_id)
    if target is None or target.session_id != session.id:
        await _deny(conn, "No such participant in this session")
        return

    if kind == "approve_join":
        role = str(message.get("role") or ROLE_VIEWER)
        await approve_participant(
            db,
            session,
            target,
            actor=actor,
            role=role if role in (ROLE_EDITOR, ROLE_VIEWER) else ROLE_VIEWER,
        )
    elif kind == "deny_join":
        await deny_participant(db, session, target, actor=actor)
    elif kind == "remove_participant":
        if target.role == ROLE_HOST:
            await _deny(conn, "The host cannot be removed")
            return
        await remove_participant(db, session, target, actor=actor)
    elif kind == "set_role":
        role = str(message.get("role") or ROLE_VIEWER)
        if role not in (ROLE_EDITOR, ROLE_VIEWER):
            await _deny(conn, "role must be 'editor' or 'viewer'")
            return
        await change_role(db, session, target, role, actor=actor)
