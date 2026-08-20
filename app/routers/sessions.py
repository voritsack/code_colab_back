"""Creating, joining and steering collaboration sessions.

There are no accounts here. Creating a session mints a session token with
``role=host``; holding that token is what makes you the host, and it is the
only credential the host-only endpoints accept. Joining mints a token for the
newcomer, scoped to that one session.
"""

from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..actions import (
    approve_participant,
    broadcast_roster,
    change_role,
    deny_participant,
    remove_participant,
    set_status,
)
from ..config import settings
from ..db import get_db
from ..models import (
    ROLE_HOST,
    ROLE_VIEWER,
    STATE_APPROVED,
    STATE_PENDING,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    CollabSession,
    Participant,
    SessionFile,
    utcnow,
)
from ..ratelimit import RateLimiter, enforce
from ..schemas import (
    FileOut,
    JoinIn,
    JoinOut,
    MessageOut,
    ParticipantOut,
    RoleUpdateIn,
    SessionCreatedOut,
    SessionCreateIn,
    SessionDetailOut,
    SessionOut,
    SessionSettingsIn,
    SnapshotIn,
    SnapshotOut,
)
from ..security import create_session_token, require_host_session, session_context
from ..services import (
    active_participant_count,
    build_session_detail,
    build_session_out,
    get_session_by_code,
    log_event,
    touch,
    unique_join_code,
    ws_url_for,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_join_limiter = RateLimiter(settings.join_rate_limit, settings.join_rate_window_seconds)
_create_limiter = RateLimiter(
    settings.session_create_rate_limit, settings.session_create_rate_window_seconds
)


def _check_host_code(supplied: str | None) -> None:
    """Enforce HOST_ACCESS_CODE, when one is configured.

    Only gates *creating* a session. Joining is already gated by needing an
    unguessable code and by the host admitting you.
    """
    expected = settings.host_access_code
    if not expected:
        return
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This server needs a host access code to start a session",
        )


def _same_session(session: CollabSession, public_id: str) -> None:
    if session.public_id != public_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token is for another session"
        )


# --------------------------------------------------------------------------
# Starting one
# --------------------------------------------------------------------------


@router.post("", response_model=SessionCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SessionCreatedOut:
    enforce(_create_limiter, request, "create-session")
    _check_host_code(payload.access_code)

    host_name = payload.display_name.strip()

    session = CollabSession(
        public_id=uuid.uuid4().hex,
        join_code=await unique_join_code(db),
        title=payload.title.strip(),
        workspace_name=(payload.workspace_name or "").strip(),
        host_name=host_name[:120],
        status=STATUS_ACTIVE,
        allow_guests=(
            settings.allow_guests_default
            if payload.allow_guests is None
            else payload.allow_guests
        ),
        require_approval=(
            settings.require_approval_default
            if payload.require_approval is None
            else payload.require_approval
        ),
        max_participants=min(
            payload.max_participants or settings.max_participants,
            settings.max_participants,
        ),
    )
    db.add(session)
    await db.flush()

    host_participant = Participant(
        session_id=session.id,
        display_name=host_name[:120],
        role=ROLE_HOST,
        state=STATE_APPROVED,
        approved_at=utcnow(),
    )
    db.add(host_participant)
    await db.flush()

    await log_event(
        db,
        kind="session.created",
        message=f"{session.title} ({session.join_code})",
        actor=host_name,
        session=session,
        participant=host_participant,
    )
    await db.commit()

    base = build_session_out(session, participant_count=1)
    return SessionCreatedOut(
        **base.model_dump(),
        participant_id=host_participant.id,
        session_token=create_session_token(
            participant_id=host_participant.id,
            session_public_id=session.public_id,
            role=ROLE_HOST,
        ),
    )


# --------------------------------------------------------------------------
# Host controls - all authorised by a host session token
# --------------------------------------------------------------------------


@router.get("/{public_id}", response_model=SessionDetailOut)
async def session_detail(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> SessionDetailOut:
    _, session = context
    _same_session(session, public_id)
    return await build_session_detail(db, session)


@router.patch("/{public_id}", response_model=SessionOut)
async def update_settings(
    public_id: str,
    payload: SessionSettingsIn,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    _, session = context
    _same_session(session, public_id)
    if session.status == STATUS_ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session has ended"
        )

    if payload.title is not None:
        session.title = payload.title.strip()
    if payload.allow_guests is not None:
        session.allow_guests = payload.allow_guests
    if payload.require_approval is not None:
        session.require_approval = payload.require_approval
    touch(session)
    await db.commit()

    return build_session_out(
        session, participant_count=await active_participant_count(db, session.id)
    )


async def _lifecycle(
    db: AsyncSession,
    context: tuple[Participant, CollabSession],
    public_id: str,
    target: str,
) -> MessageOut:
    host, session = context
    _same_session(session, public_id)
    if session.status == STATUS_ENDED and target != STATUS_ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session has ended"
        )
    await set_status(db, session, target, actor=host.display_name)
    return MessageOut(detail=f"Session {target}")


@router.post("/{public_id}/pause", response_model=MessageOut)
async def pause(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    return await _lifecycle(db, context, public_id, STATUS_PAUSED)


@router.post("/{public_id}/resume", response_model=MessageOut)
async def resume(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    return await _lifecycle(db, context, public_id, STATUS_ACTIVE)


@router.post("/{public_id}/end", response_model=MessageOut)
async def end(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    return await _lifecycle(db, context, public_id, STATUS_ENDED)


@router.get("/{public_id}/participants", response_model=list[ParticipantOut])
async def list_participants(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> list[ParticipantOut]:
    _, session = context
    _same_session(session, public_id)
    people = (
        await db.scalars(
            select(Participant)
            .where(Participant.session_id == session.id)
            .order_by(Participant.requested_at)
        )
    ).all()
    return [ParticipantOut.model_validate(p) for p in people]


async def _target(
    db: AsyncSession,
    context: tuple[Participant, CollabSession],
    public_id: str,
    participant_id: int,
) -> tuple[Participant, CollabSession, Participant]:
    host, session = context
    _same_session(session, public_id)
    target = await db.get(Participant, participant_id)
    if target is None or target.session_id != session.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Participant not found"
        )
    return host, session, target


@router.post("/{public_id}/participants/{participant_id}/approve", response_model=MessageOut)
async def approve(
    public_id: str,
    participant_id: int,
    role: str = ROLE_VIEWER,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    host, session, target = await _target(db, context, public_id, participant_id)
    await approve_participant(
        db, session, target, actor=host.display_name, role=role
    )
    return MessageOut(detail="Approved")


@router.post("/{public_id}/participants/{participant_id}/deny", response_model=MessageOut)
async def deny(
    public_id: str,
    participant_id: int,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    host, session, target = await _target(db, context, public_id, participant_id)
    await deny_participant(db, session, target, actor=host.display_name)
    return MessageOut(detail="Denied")


@router.post("/{public_id}/participants/{participant_id}/remove", response_model=MessageOut)
async def remove(
    public_id: str,
    participant_id: int,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    host, session, target = await _target(db, context, public_id, participant_id)
    if target.role == ROLE_HOST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the host"
        )
    await remove_participant(db, session, target, actor=host.display_name)
    return MessageOut(detail="Removed")


@router.patch("/{public_id}/participants/{participant_id}/role", response_model=MessageOut)
async def set_role(
    public_id: str,
    participant_id: int,
    payload: RoleUpdateIn,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    host, session, target = await _target(db, context, public_id, participant_id)
    await change_role(db, session, target, payload.role, actor=host.display_name)
    return MessageOut(detail=f"Role set to {payload.role}")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


@router.put("/{public_id}/files", response_model=MessageOut)
async def upload_snapshot(
    public_id: str,
    payload: SnapshotIn,
    context: tuple[Participant, CollabSession] = Depends(require_host_session),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """Replace the stored project with the host's current workspace."""
    host, session = context
    _same_session(session, public_id)
    if session.status == STATUS_ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session has ended"
        )

    await db.execute(delete(SessionFile).where(SessionFile.session_id == session.id))
    now = utcnow()
    for item in payload.files:
        db.add(
            SessionFile(
                session_id=session.id,
                path=item.path,
                content=item.content,
                size=len(item.content.encode("utf-8")),
                updated_at=now,
            )
        )
    touch(session)
    await log_event(
        db,
        kind="session.snapshot",
        message=f"{len(payload.files)} file(s) uploaded",
        actor=host.display_name,
        session=session,
    )
    await db.commit()

    from ..actions import broadcast_snapshot

    await broadcast_snapshot(db, session)
    return MessageOut(detail=f"Stored {len(payload.files)} file(s)")


@router.get("/{public_id}/files", response_model=SnapshotOut)
async def download_snapshot(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(session_context),
    db: AsyncSession = Depends(get_db),
) -> SnapshotOut:
    """Fetch the project. Used to resync after a dropped connection."""
    participant, session = context
    _same_session(session, public_id)
    if participant.state != STATE_APPROVED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Waiting for host approval"
        )

    files = (
        await db.scalars(
            select(SessionFile)
            .where(SessionFile.session_id == session.id)
            .order_by(SessionFile.path)
        )
    ).all()
    return SnapshotOut(
        public_id=session.public_id,
        status=session.status,
        files=[
            FileOut(path=f.path, content=f.content, updated_at=f.updated_at)
            for f in files
        ],
    )


# --------------------------------------------------------------------------
# Joining
# --------------------------------------------------------------------------


@router.post("/join", response_model=JoinOut)
async def join_session(
    payload: JoinIn, request: Request, db: AsyncSession = Depends(get_db)
) -> JoinOut:
    enforce(_join_limiter, request, "join")

    session = await get_session_by_code(db, payload.code)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No live session for that code"
        )
    if not session.allow_guests:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The host has closed this session to new people",
        )

    if await active_participant_count(db, session.id) >= session.max_participants:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session is full"
        )

    participant = Participant(
        session_id=session.id,
        display_name=payload.display_name.strip()[:120],
        role=ROLE_VIEWER,
        state=STATE_PENDING if session.require_approval else STATE_APPROVED,
        client_id=(payload.client_id or "")[:64] or None,
        approved_at=None if session.require_approval else utcnow(),
    )
    db.add(participant)
    await db.flush()

    await log_event(
        db,
        kind="participant.requested" if session.require_approval else "participant.joined",
        message=f"{participant.display_name} -> {session.title}",
        actor=participant.display_name,
        session=session,
        participant=participant,
    )
    touch(session)
    await db.commit()

    if participant.state == STATE_PENDING:
        # Tell any connected host straight away, so the prompt appears even
        # before the newcomer's socket is up.
        from ..hub import hub

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
    await broadcast_roster(db, session)

    return JoinOut(
        public_id=session.public_id,
        title=session.title,
        host_name=session.host_name,
        status=session.status,
        state=participant.state,
        role=participant.role,
        participant_id=participant.id,
        session_token=create_session_token(
            participant_id=participant.id,
            session_public_id=session.public_id,
            role=participant.role,
        ),
        ws_url=ws_url_for(session.public_id),
    )


@router.get("/by-code/{code}", response_model=dict)
async def peek_session(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Minimal public preview so the join page can show what you are joining."""
    from ..utils import normalize_join_code

    session = await get_session_by_code(db, normalize_join_code(code))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No live session for that code"
        )
    approved = int(
        await db.scalar(
            select(func.count(Participant.id)).where(
                Participant.session_id == session.id,
                Participant.state == STATE_APPROVED,
            )
        )
        or 0
    )
    return {
        "title": session.title,
        "workspace_name": session.workspace_name,
        "host_name": session.host_name,
        "status": session.status,
        "allow_guests": session.allow_guests,
        "require_approval": session.require_approval,
        "participants": approved,
    }
