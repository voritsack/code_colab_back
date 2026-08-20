"""Creating, joining and steering collaboration sessions."""

from __future__ import annotations

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
    STATE_DENIED,
    STATE_PENDING,
    STATE_REMOVED,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    CollabSession,
    Participant,
    SessionFile,
    User,
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
from ..security import create_session_token, current_user, optional_user, session_context
from ..services import (
    active_participant_count,
    build_session_detail,
    build_session_out,
    get_session_by_code,
    get_session_or_404,
    log_event,
    require_host,
    touch,
    ws_url_for,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_join_limiter = RateLimiter(settings.join_rate_limit, settings.join_rate_window_seconds)


# --------------------------------------------------------------------------
# Host: lifecycle
# --------------------------------------------------------------------------


@router.post("", response_model=SessionCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionCreatedOut:
    from ..services import unique_join_code

    session = CollabSession(
        public_id=uuid.uuid4().hex,
        join_code=await unique_join_code(db),
        title=payload.title.strip(),
        workspace_name=(payload.workspace_name or "").strip(),
        host_id=user.id,
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
        user_id=user.id,
        display_name=user.name,
        is_guest=False,
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
        session=session,
        user=user,
    )
    await db.commit()

    base = build_session_out(session, host_name=user.name, participant_count=1)
    return SessionCreatedOut(
        **base.model_dump(),
        participant_id=host_participant.id,
        session_token=create_session_token(
            participant_id=host_participant.id,
            session_public_id=session.public_id,
            role=ROLE_HOST,
        ),
    )


@router.get("/mine", response_model=list[SessionOut])
async def my_sessions(
    include_ended: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SessionOut]:
    query = select(CollabSession).where(CollabSession.host_id == user.id)
    if not include_ended:
        query = query.where(CollabSession.status != STATUS_ENDED)
    sessions = (
        await db.scalars(query.order_by(CollabSession.created_at.desc()).limit(100))
    ).all()

    return [
        build_session_out(
            item,
            host_name=user.name,
            participant_count=await active_participant_count(db, item.id),
        )
        for item in sessions
    ]


@router.get("/{public_id}", response_model=SessionDetailOut)
async def session_detail(
    public_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionDetailOut:
    session = await require_host(db, public_id, user)
    host = await db.get(User, session.host_id)
    return await build_session_detail(
        db, session, host_name=host.name if host else "unknown"
    )


@router.patch("/{public_id}", response_model=SessionOut)
async def update_settings(
    public_id: str,
    payload: SessionSettingsIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    session = await require_host(db, public_id, user)
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
        session,
        host_name=user.name,
        participant_count=await active_participant_count(db, session.id),
    )


@router.post("/{public_id}/pause", response_model=MessageOut)
async def pause(
    public_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session = await require_host(db, public_id, user)
    if session.status == STATUS_ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session has ended"
        )
    await set_status(db, session, STATUS_PAUSED, actor=user.name)
    return MessageOut(detail="Session paused")


@router.post("/{public_id}/resume", response_model=MessageOut)
async def resume(
    public_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session = await require_host(db, public_id, user)
    if session.status == STATUS_ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session has ended"
        )
    await set_status(db, session, STATUS_ACTIVE, actor=user.name)
    return MessageOut(detail="Session resumed")


@router.post("/{public_id}/end", response_model=MessageOut)
async def end(
    public_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session = await require_host(db, public_id, user)
    await set_status(db, session, STATUS_ENDED, actor=user.name)
    return MessageOut(detail="Session ended")


# --------------------------------------------------------------------------
# Host: participants
# --------------------------------------------------------------------------


@router.get("/{public_id}/participants", response_model=list[ParticipantOut])
async def list_participants(
    public_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ParticipantOut]:
    session = await require_host(db, public_id, user)
    people = (
        await db.scalars(
            select(Participant)
            .where(Participant.session_id == session.id)
            .order_by(Participant.requested_at)
        )
    ).all()
    return [ParticipantOut.model_validate(p) for p in people]


async def _host_and_participant(
    db: AsyncSession, public_id: str, participant_id: int, user: User
) -> tuple[CollabSession, Participant]:
    session = await require_host(db, public_id, user)
    participant = await db.get(Participant, participant_id)
    if participant is None or participant.session_id != session.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Participant not found"
        )
    return session, participant


@router.post("/{public_id}/participants/{participant_id}/approve", response_model=MessageOut)
async def approve(
    public_id: str,
    participant_id: int,
    role: str = ROLE_VIEWER,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session, participant = await _host_and_participant(db, public_id, participant_id, user)
    await approve_participant(db, session, participant, actor=user.name, role=role)
    return MessageOut(detail="Approved")


@router.post("/{public_id}/participants/{participant_id}/deny", response_model=MessageOut)
async def deny(
    public_id: str,
    participant_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session, participant = await _host_and_participant(db, public_id, participant_id, user)
    await deny_participant(db, session, participant, actor=user.name)
    return MessageOut(detail="Denied")


@router.post("/{public_id}/participants/{participant_id}/remove", response_model=MessageOut)
async def remove(
    public_id: str,
    participant_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session, participant = await _host_and_participant(db, public_id, participant_id, user)
    if participant.role == ROLE_HOST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the host"
        )
    await remove_participant(db, session, participant, actor=user.name)
    return MessageOut(detail="Removed")


@router.patch("/{public_id}/participants/{participant_id}/role", response_model=MessageOut)
async def set_role(
    public_id: str,
    participant_id: int,
    payload: RoleUpdateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    session, participant = await _host_and_participant(db, public_id, participant_id, user)
    await change_role(db, session, participant, payload.role, actor=user.name)
    return MessageOut(detail=f"Role set to {payload.role}")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


@router.put("/{public_id}/files", response_model=MessageOut)
async def upload_snapshot(
    public_id: str,
    payload: SnapshotIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """Replace the stored project with the host's current workspace."""
    session = await require_host(db, public_id, user)
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
        session=session,
        user=user,
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
    if session.public_id != public_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token is for another session"
        )
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
    payload: JoinIn,
    request: Request,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> JoinOut:
    enforce(_join_limiter, request, "join")

    session = await get_session_by_code(db, payload.code)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No live session for that code"
        )

    host = await db.get(User, session.host_id)
    host_name = host.name if host else "unknown"

    # Someone who already has a seat keeps it, so a reconnect or a second
    # window does not queue them up for approval all over again.
    existing: Participant | None = None
    if user is not None:
        existing = await db.scalar(
            select(Participant).where(
                Participant.session_id == session.id, Participant.user_id == user.id
            )
        )

    if existing is not None:
        if existing.state in (STATE_DENIED, STATE_REMOVED):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not allowed in this session",
            )
        participant = existing
        if participant.role == ROLE_HOST:
            # The host rejoining their own session never waits in the lobby.
            participant.state = STATE_APPROVED
        if payload.client_id:
            participant.client_id = payload.client_id[:64]
    else:
        if user is None:
            if not session.allow_guests:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="This session requires a signed-in account",
                )
            display_name = (payload.display_name or "").strip()
            if len(display_name) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Guests must supply a display name",
                )
        else:
            display_name = user.name

        if await active_participant_count(db, session.id) >= session.max_participants:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Session is full"
            )

        participant = Participant(
            session_id=session.id,
            user_id=user.id if user else None,
            display_name=display_name[:120],
            is_guest=user is None,
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
            session=session,
            participant=participant,
        )

    touch(session)
    await db.commit()

    if participant.state == STATE_PENDING:
        # Tell any connected host straight away, so the prompt shows up even
        # before the newcomer's WebSocket is up.
        from ..hub import hub

        await hub.notify_hosts(
            session.public_id,
            {
                "type": "join_request",
                "participant": {
                    "participant_id": participant.id,
                    "display_name": participant.display_name,
                    "is_guest": participant.is_guest,
                },
            },
        )
    await broadcast_roster(db, session)

    return JoinOut(
        public_id=session.public_id,
        title=session.title,
        host_name=host_name,
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
    host = await db.get(User, session.host_id)
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
        "host_name": host.name if host else "unknown",
        "status": session.status,
        "allow_guests": session.allow_guests,
        "require_approval": session.require_approval,
        "participants": approved,
    }
