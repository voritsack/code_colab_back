"""Shared logic used by more than one router."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import (
    STATE_APPROVED,
    STATE_PENDING,
    STATUS_ENDED,
    ActivityEvent,
    CollabSession,
    Participant,
    SessionFile,
    User,
    utcnow,
)
from .schemas import ParticipantOut, SessionDetailOut, SessionOut
from .utils import generate_join_code


async def log_event(
    db: AsyncSession,
    *,
    kind: str,
    message: str = "",
    actor: str = "",
    session: CollabSession | None = None,
    participant: Participant | None = None,
    user: User | None = None,
) -> ActivityEvent:
    event = ActivityEvent(
        kind=kind,
        message=message[:500],
        actor=(actor or (user.name if user else "") or (participant.display_name if participant else ""))[:120],
        session_id=session.id if session else None,
        participant_id=participant.id if participant else None,
        user_id=user.id if user else None,
    )
    db.add(event)
    return event


async def unique_join_code(db: AsyncSession) -> str:
    """Generate a join code no session has ever used.

    The alphabet is 25 letters over 10 positions, so a collision is a
    lottery-win event; retrying a handful of times is plenty. Codes are never
    recycled, which keeps an old link from silently landing someone in a
    stranger's new session.
    """
    for _ in range(12):
        code = generate_join_code()
        taken = await db.scalar(
            select(CollabSession.id).where(CollabSession.join_code == code)
        )
        if taken is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Could not allocate a join code, try again",
    )


async def get_session_or_404(db: AsyncSession, public_id: str) -> CollabSession:
    session = await db.scalar(
        select(CollabSession).where(CollabSession.public_id == public_id)
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    return session


async def get_session_by_code(db: AsyncSession, code: str) -> CollabSession | None:
    return await db.scalar(
        select(CollabSession).where(
            CollabSession.join_code == code,
            CollabSession.status != STATUS_ENDED,
        )
    )


async def active_participant_count(db: AsyncSession, session_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count(Participant.id)).where(
                Participant.session_id == session_id,
                Participant.state.in_((STATE_APPROVED, STATE_PENDING)),
            )
        )
        or 0
    )


async def file_count(db: AsyncSession, session_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count(SessionFile.id)).where(
                SessionFile.session_id == session_id
            )
        )
        or 0
    )


def touch(session: CollabSession) -> None:
    session.last_activity_at = utcnow()


def build_session_out(
    session: CollabSession, *, host_name: str | None = None, participant_count: int
) -> SessionOut:
    return SessionOut(
        public_id=session.public_id,
        join_code=session.join_code,
        title=session.title,
        workspace_name=session.workspace_name,
        status=session.status,
        allow_guests=session.allow_guests,
        require_approval=session.require_approval,
        max_participants=session.max_participants,
        created_at=session.created_at,
        host_name=host_name if host_name is not None else session.host_name,
        participant_count=participant_count,
        join_url=settings.join_url(session.join_code),
        vscode_link=settings.vscode_deep_link(session.join_code),
    )


async def build_session_detail(
    db: AsyncSession, session: CollabSession, *, host_name: str | None = None
) -> SessionDetailOut:
    participants = (
        await db.scalars(
            select(Participant)
            .where(Participant.session_id == session.id)
            .order_by(Participant.requested_at)
        )
    ).all()
    base = build_session_out(
        session,
        host_name=host_name,
        participant_count=sum(
            1 for p in participants if p.state in (STATE_APPROVED, STATE_PENDING)
        ),
    )
    return SessionDetailOut(
        **base.model_dump(),
        participants=[ParticipantOut.model_validate(p) for p in participants],
        file_count=await file_count(db, session.id),
    )


def ws_url_for(public_id: str) -> str:
    base = settings.public_base_url
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{ws_base}/ws/session/{public_id}"


def participant_payload(participant: Participant) -> dict[str, Any]:
    return {
        "participant_id": participant.id,
        "display_name": participant.display_name,
        "role": participant.role,
        "state": participant.state,
        "connected": participant.connected,
        "active_file": participant.active_file,
        "edits": participant.edits,
    }
