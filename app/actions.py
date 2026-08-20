"""Session mutations that both the REST API and the WebSocket handler perform.

Keeping them here means "host approves a guest" behaves identically whether
it was clicked in the admin dashboard or sent as a WebSocket frame.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .hub import hub
from .models import (
    ROLE_EDITOR,
    ROLE_HOST,
    ROLE_VIEWER,
    STATE_APPROVED,
    STATE_DENIED,
    STATE_PENDING,
    STATE_REMOVED,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    CollabSession,
    Participant,
    SessionFile,
    utcnow,
)
from .services import log_event, participant_payload, touch

# WebSocket close codes (4000+ is the application-defined range).
CLOSE_DENIED = 4003
CLOSE_REMOVED = 4005
CLOSE_SESSION_ENDED = 4006


async def roster(db: AsyncSession, session: CollabSession) -> list[Participant]:
    return list(
        (
            await db.scalars(
                select(Participant)
                .where(
                    Participant.session_id == session.id,
                    Participant.state.in_((STATE_APPROVED, STATE_PENDING)),
                )
                .order_by(Participant.requested_at)
            )
        ).all()
    )


async def broadcast_roster(db: AsyncSession, session: CollabSession) -> None:
    """Push the participant list out.

    Hosts see everyone including people still waiting; approved participants
    see only the approved roster, so a pending guest is not advertised to the
    room before the host has decided.
    """
    people = await roster(db, session)
    everyone = [participant_payload(p) for p in people]
    approved = [p for p in everyone if p["state"] == STATE_APPROVED]

    await hub.notify_hosts(
        session.public_id, {"type": "participants", "participants": everyone}
    )
    for conn in hub.connections(session.public_id):
        if conn.is_host or not conn.approved:
            continue
        await hub.send(conn, {"type": "participants", "participants": approved})


async def send_snapshot(
    db: AsyncSession, session: CollabSession, participant_id: int
) -> None:
    files = (
        await db.scalars(
            select(SessionFile)
            .where(SessionFile.session_id == session.id)
            .order_by(SessionFile.path)
        )
    ).all()
    await hub.send_to_participant(
        session.public_id,
        participant_id,
        {
            "type": "snapshot",
            "status": session.status,
            "files": [{"path": f.path, "content": f.content} for f in files],
        },
    )


async def broadcast_snapshot(db: AsyncSession, session: CollabSession) -> None:
    for conn in hub.connections(session.public_id):
        if conn.approved:
            await send_snapshot(db, session, conn.participant_id)


async def approve_participant(
    db: AsyncSession,
    session: CollabSession,
    participant: Participant,
    *,
    actor: str,
    role: str = ROLE_VIEWER,
) -> None:
    if participant.state == STATE_APPROVED:
        return

    participant.state = STATE_APPROVED
    participant.approved_at = utcnow()
    if participant.role != ROLE_HOST:
        participant.role = role if role in (ROLE_EDITOR, ROLE_VIEWER) else ROLE_VIEWER
    touch(session)

    await log_event(
        db,
        kind="participant.approved",
        message=f"{participant.display_name} joined {session.title}",
        actor=actor,
        session=session,
        participant=participant,
    )
    await db.commit()

    conn = hub.get(session.public_id, participant.id)
    if conn is not None:
        conn.approved = True
        conn.role = participant.role

    await hub.send_to_participant(
        session.public_id,
        participant.id,
        {
            "type": "approved",
            "role": participant.role,
            "session": {
                "public_id": session.public_id,
                "title": session.title,
                "status": session.status,
            },
        },
    )
    await send_snapshot(db, session, participant.id)
    await hub.broadcast(
        session.public_id,
        {"type": "participant_joined", "participant": participant_payload(participant)},
        exclude_participant=participant.id,
    )
    await broadcast_roster(db, session)


async def deny_participant(
    db: AsyncSession, session: CollabSession, participant: Participant, *, actor: str
) -> None:
    participant.state = STATE_DENIED
    participant.left_at = utcnow()
    participant.connected = False
    await log_event(
        db,
        kind="participant.denied",
        message=f"{participant.display_name} was refused entry",
        actor=actor,
        session=session,
        participant=participant,
    )
    await db.commit()

    await hub.send_to_participant(
        session.public_id,
        participant.id,
        {"type": "denied", "reason": "The host did not admit you"},
    )
    await hub.disconnect_participant(
        session.public_id, participant.id, code=CLOSE_DENIED, reason="Not admitted"
    )
    await broadcast_roster(db, session)


async def remove_participant(
    db: AsyncSession, session: CollabSession, participant: Participant, *, actor: str
) -> None:
    participant.state = STATE_REMOVED
    participant.left_at = utcnow()
    participant.connected = False
    await log_event(
        db,
        kind="participant.removed",
        message=f"{participant.display_name} was removed",
        actor=actor,
        session=session,
        participant=participant,
    )
    await db.commit()

    await hub.send_to_participant(
        session.public_id,
        participant.id,
        {"type": "removed", "reason": "The host removed you from the session"},
    )
    await hub.disconnect_participant(
        session.public_id, participant.id, code=CLOSE_REMOVED, reason="Removed"
    )
    await hub.broadcast(
        session.public_id,
        {"type": "participant_left", "participant_id": participant.id},
    )
    await broadcast_roster(db, session)


async def change_role(
    db: AsyncSession,
    session: CollabSession,
    participant: Participant,
    role: str,
    *,
    actor: str,
) -> None:
    if participant.role == ROLE_HOST:
        return
    participant.role = role
    await log_event(
        db,
        kind="participant.role",
        message=f"{participant.display_name} is now {role}",
        actor=actor,
        session=session,
        participant=participant,
    )
    await db.commit()

    conn = hub.get(session.public_id, participant.id)
    if conn is not None:
        conn.role = role

    await hub.send_to_participant(
        session.public_id, participant.id, {"type": "role_changed", "role": role}
    )
    await broadcast_roster(db, session)


async def set_status(
    db: AsyncSession, session: CollabSession, new_status: str, *, actor: str
) -> None:
    """Pause, resume or end a session.

    Pausing freezes propagation in both directions: nothing is broadcast and
    nothing is stored, so the host can talk over a frozen picture. Resuming
    re-sends the snapshot, which resynchronises anyone who typed locally
    while the session was frozen.
    """
    if session.status == new_status:
        return

    previous = session.status
    session.status = new_status
    # Update the hot-path cache the socket reads, whoever triggered the change.
    hub.set_room_status(session.public_id, new_status)
    touch(session)

    if new_status == STATUS_PAUSED:
        session.paused_at = utcnow()
    elif new_status == STATUS_ACTIVE:
        session.paused_at = None
    elif new_status == STATUS_ENDED:
        session.ended_at = utcnow()
        for participant in await roster(db, session):
            participant.connected = False
            if participant.left_at is None:
                participant.left_at = utcnow()

    await log_event(
        db,
        kind=f"session.{new_status}",
        message=f"{session.title}: {previous} -> {new_status}",
        actor=actor,
        session=session,
    )
    await db.commit()

    if new_status == STATUS_ENDED:
        await hub.broadcast(
            session.public_id,
            {"type": "session_ended", "reason": f"Ended by {actor}"},
            approved_only=False,
        )
        await hub.close_room(
            session.public_id, code=CLOSE_SESSION_ENDED, reason="Session ended"
        )
        return

    await hub.broadcast(
        session.public_id,
        {"type": "session_state", "status": new_status, "by": actor},
        approved_only=False,
    )
    if new_status == STATUS_ACTIVE and previous == STATUS_PAUSED:
        await broadcast_snapshot(db, session)


async def apply_file_update(
    db: AsyncSession,
    session: CollabSession,
    participant: Participant,
    path: str,
    content: str,
) -> dict[str, Any]:
    """Persist one file edit and return the frame to fan out."""
    row = await db.scalar(
        select(SessionFile).where(
            SessionFile.session_id == session.id, SessionFile.path == path
        )
    )
    size = len(content.encode("utf-8"))

    if row is None:
        row = SessionFile(
            session_id=session.id,
            path=path,
            content=content,
            size=size,
            updated_by_id=participant.id,
        )
        db.add(row)
    else:
        row.content = content
        row.size = size
        row.updated_at = utcnow()
        row.updated_by_id = participant.id

    participant.edits += 1
    participant.active_file = path[:500]
    participant.last_seen_at = utcnow()
    touch(session)

    return {
        "type": "file_update",
        "path": path,
        "content": content,
        "from": participant.display_name,
        "participant_id": participant.id,
    }


async def delete_file(
    db: AsyncSession, session: CollabSession, participant: Participant, path: str
) -> dict[str, Any]:
    row = await db.scalar(
        select(SessionFile).where(
            SessionFile.session_id == session.id, SessionFile.path == path
        )
    )
    if row is not None:
        await db.delete(row)
    touch(session)
    return {
        "type": "file_deleted",
        "path": path,
        "from": participant.display_name,
        "participant_id": participant.id,
    }
