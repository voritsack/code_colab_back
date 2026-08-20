"""Keeping the database and the disk from filling up.

Two callers, one set of rules: the background sweeper runs these on a timer,
and the admin dashboard runs them on demand. A session that ends leaves
behind everything that was shared into it, and nothing here is meant to be
kept - so the only question is when it goes, not whether.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import storage
from .config import settings
from .db import SessionLocal
from .hub import hub
from .models import (
    STATUS_ENDED,
    ActivityEvent,
    Attachment,
    BoardStroke,
    ChatMessage,
    CollabSession,
    Participant,
    SessionFile,
    utcnow,
)

logger = logging.getLogger("codecolab")

CLOSE_SESSION_ENDED = 4006

# Every table that hangs off a session, in the order they have to go: the
# children first, or a foreign key stops the parent from being deleted.
SESSION_CHILDREN = (BoardStroke, ChatMessage, SessionFile, Attachment, ActivityEvent, Participant)

# What an ended session keeps only so somebody can still read it back.
ARTEFACT_MODELS = (SessionFile, BoardStroke, ChatMessage)


async def close_orphaned_sessions() -> None:
    """Reconcile the database with the fact that the process just started.

    The hub only exists in memory, so after a restart nobody is connected -
    whatever the participant rows say. Sessions idle beyond the timeout are
    ended outright; the rest keep their status and wait for their host to
    reconnect.
    """
    cutoff = utcnow() - timedelta(minutes=settings.session_idle_timeout_minutes)
    async with SessionLocal() as db:
        marked = await db.execute(
            update(Participant)
            .where(Participant.connected.is_(True))
            .values(connected=False)
        )

        stale = (
            await db.scalars(
                select(CollabSession).where(
                    CollabSession.status != STATUS_ENDED,
                    CollabSession.last_activity_at < cutoff,
                )
            )
        ).all()
        now = utcnow()
        for session in stale:
            session.status = STATUS_ENDED
            session.ended_at = now

        await db.commit()

        if marked.rowcount or stale:
            logger.info(
                "Startup sweep: cleared %s stale connection(s), ended %s idle session(s)",
                marked.rowcount or 0,
                len(stale),
            )


async def end_idle_sessions(db: AsyncSession, now=None) -> int:
    """End sessions nobody has touched for longer than the idle timeout."""
    now = now or utcnow()
    cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    stale = (
        await db.scalars(
            select(CollabSession).where(
                CollabSession.status != STATUS_ENDED,
                CollabSession.last_activity_at < cutoff,
            )
        )
    ).all()
    for session in stale:
        session.status = STATUS_ENDED
        session.ended_at = now
        hub.set_room_status(session.public_id, STATUS_ENDED)
    return len(stale)


async def drop_dead_attachments(db: AsyncSession) -> int:
    """Delete attachments belonging to sessions that are over.

    They go with the session that carried them - whether it ended cleanly or
    the process died before it could tidy up after itself.
    """
    rows = (
        await db.scalars(
            select(Attachment)
            .join(CollabSession, Attachment.session_id == CollabSession.id)
            .where(CollabSession.status == STATUS_ENDED)
        )
    ).all()
    if not rows:
        return 0
    storage.remove_many(row.stored_name for row in rows)
    await db.execute(delete(Attachment).where(Attachment.id.in_([r.id for r in rows])))
    return len(rows)


async def strip_artefacts(db: AsyncSession, *, older_than_hours: int | None = None) -> int:
    """Throw away what an ended session was carrying: files, chat, drawings.

    These are the bulk of the storage - a shared workspace can be tens of
    megabytes - and they are dead weight once the session is over. Pass
    ``older_than_hours=None`` to strip every ended session immediately,
    which is what the dashboard's "free space now" button does.
    """
    query = select(CollabSession.id).where(CollabSession.status == STATUS_ENDED)
    if older_than_hours:
        cutoff = utcnow() - timedelta(hours=older_than_hours)
        query = query.where(CollabSession.last_activity_at < cutoff)

    spent = list((await db.scalars(query)).all())
    if not spent:
        return 0

    removed = 0
    for model in ARTEFACT_MODELS:
        result = await db.execute(delete(model).where(model.session_id.in_(spent)))
        removed += result.rowcount or 0
    return removed


async def purge_sessions(db: AsyncSession, *, older_than_days: int | None = None) -> int:
    """Delete ended session rows outright, roster and all.

    ``older_than_days=None`` deletes every ended session now. Anything still
    live is left alone: pruning must never disconnect people who are working.
    """
    query = select(CollabSession.id).where(CollabSession.status == STATUS_ENDED)
    if older_than_days:
        cutoff = utcnow() - timedelta(days=older_than_days)
        query = query.where(
            CollabSession.ended_at.isnot(None), CollabSession.ended_at < cutoff
        )

    doomed = list((await db.scalars(query)).all())
    if not doomed:
        return 0

    # The bytes on disk have no foreign key to follow, so they go by hand.
    stored = (
        await db.scalars(
            select(Attachment.stored_name).where(Attachment.session_id.in_(doomed))
        )
    ).all()
    storage.remove_many(stored)

    for model in SESSION_CHILDREN:
        await db.execute(delete(model).where(model.session_id.in_(doomed)))
    await db.execute(delete(CollabSession).where(CollabSession.id.in_(doomed)))
    return len(doomed)


async def delete_session(db: AsyncSession, session: CollabSession) -> None:
    """Remove one session and everything hanging off it, live or not.

    Anyone still connected is disconnected first: deleting the row under an
    open socket would leave the extension talking to a session that no
    longer exists.
    """
    public_id = session.public_id
    session_id = session.id

    await hub.broadcast(
        public_id,
        {"type": "session_ended", "reason": "Deleted by an administrator"},
        approved_only=False,
    )
    await hub.close_room(public_id, code=CLOSE_SESSION_ENDED, reason="Session deleted")
    hub.forget_room_status(public_id)
    hub.forget_locks(public_id)
    hub.cancel_empty_timer(public_id)

    stored = (
        await db.scalars(
            select(Attachment.stored_name).where(Attachment.session_id == session_id)
        )
    ).all()
    storage.remove_many(stored)

    for model in SESSION_CHILDREN:
        await db.execute(delete(model).where(model.session_id == session_id))
    await db.execute(delete(CollabSession).where(CollabSession.id == session_id))


async def storage_overview(db: AsyncSession) -> dict:
    """What is currently being kept, so pruning is an informed decision."""
    async def count(model, *where) -> int:
        return int(await db.scalar(select(func.count(model.id)).where(*where)) or 0)

    ended = await count(CollabSession, CollabSession.status == STATUS_ENDED)
    live = await count(CollabSession, CollabSession.status != STATUS_ENDED)
    file_bytes = int(await db.scalar(select(func.sum(SessionFile.size))) or 0)

    return {
        "sessions_live": live,
        "sessions_ended": ended,
        "files": await count(SessionFile),
        "file_bytes": file_bytes,
        "chat_messages": await count(ChatMessage),
        "board_strokes": await count(BoardStroke),
        "attachments": await count(Attachment),
        "attachment_bytes": storage.usage_bytes(),
        "participants": await count(Participant),
        "events": await count(ActivityEvent),
        "retention_days": settings.retention_days,
        "artefact_retention_hours": settings.artefact_retention_hours,
        "idle_timeout_minutes": settings.session_idle_timeout_minutes,
        "sweep_interval_minutes": settings.sweep_interval_minutes,
    }


async def sweep_once() -> dict:
    """End idle sessions and delete the ones that finished long ago.

    Without this, a host who closes their laptop leaves a session showing as
    live for good, and every file ever shared stays in the database.
    """
    async with SessionLocal() as db:
        ended = await end_idle_sessions(db)
        detached = await drop_dead_attachments(db)
        stripped = (
            await strip_artefacts(db, older_than_hours=settings.artefact_retention_hours)
            if settings.artefact_retention_hours > 0
            else 0
        )
        purged = (
            await purge_sessions(db, older_than_days=settings.retention_days)
            if settings.retention_days > 0
            else 0
        )
        await db.commit()

        # Files on disk with no row pointing at them - a crash between the
        # write and the commit - would otherwise sit there forever.
        known = set((await db.scalars(select(Attachment.stored_name))).all())
        orphans = storage.sweep_orphans(known)

    if ended or purged or stripped or detached:
        logger.info(
            "Sweep: ended %s idle session(s), freed %s artefact row(s), "
            "deleted %s attachment(s), purged %s expired session(s)",
            ended,
            stripped,
            detached,
            purged,
        )
    return {
        "ended": ended,
        "stripped": stripped,
        "detached": detached,
        "purged": purged,
        "orphans": orphans,
    }


async def sweeper() -> None:
    interval = max(settings.sweep_interval_minutes, 1) * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_once()
        except Exception:  # noqa: BLE001 - housekeeping must never kill the app
            logger.exception("Sweep failed")
