"""Server-rendered admin dashboard.

Deliberately not a SPA: it is a handful of pages plus one JSON endpoint that
the dashboard polls, so the whole product still ships without a frontend
build step.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..actions import attachment_list, chat_history, set_status
from ..config import settings
from ..db import get_db
from ..housekeeping import (
    delete_session,
    end_idle_sessions,
    purge_sessions,
    storage_overview,
    strip_artefacts,
    sweep_once,
)
from ..hub import hub
from ..models import (
    STATE_APPROVED,
    STATE_PENDING,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    ActivityEvent,
    Attachment,
    BoardStroke,
    ChatMessage,
    CollabSession,
    Participant,
    SessionFile,
    User,
    utcnow,
)
from . import extension as extension_router
from ..ratelimit import RateLimiter, enforce
from ..security import (
    ADMIN_COOKIE,
    CSRF_COOKIE,
    create_admin_token,
    current_admin,
    new_csrf_token,
    require_csrf,
    verify_password,
)
from ..templating import templates
from ..services import log_event, touch, unique_join_code

router = APIRouter(prefix="/admin", tags=["admin"])

_admin_limiter = RateLimiter(
    settings.login_rate_limit, settings.login_rate_window_seconds
)


def _set_csrf(response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=False,
        secure=settings.use_secure_cookies,
        samesite="lax",
        max_age=settings.admin_session_ttl_minutes * 60,
        path="/admin",
    )


def _csrf_for(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or new_csrf_token()


async def _session_or_404(db: AsyncSession, public_id: str) -> CollabSession:
    session = await db.scalar(
        select(CollabSession).where(CollabSession.public_id == public_id)
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


# How much of a file the watch view will show. Long enough for any source
# file, short enough that a minified bundle cannot wedge the page.
WATCH_CONTENT_LIMIT = 200_000


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    token = new_csrf_token()
    response = templates.TemplateResponse(
        request,
        "admin_login.html",
        {"settings": settings, "csrf_token": token, "error": error},
    )
    _set_csrf(response, token)
    return response


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    enforce(_admin_limiter, request, "admin-login")

    user = await db.scalar(select(User).where(User.email == email.strip().lower()))
    if (
        user is None
        or not user.is_active
        or not user.is_admin
        or not verify_password(password, user.password_hash)
    ):
        return RedirectResponse(
            "/admin/login?error=Invalid+credentials", status_code=status.HTTP_303_SEE_OTHER
        )

    user.last_login_at = utcnow()
    await log_event(db, kind="admin.login", message=user.email, user=user)
    await db.commit()

    response = RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_COOKIE,
        create_admin_token(user),
        httponly=True,
        secure=settings.use_secure_cookies,
        samesite="lax",
        max_age=settings.admin_session_ttl_minutes * 60,
        path="/",
    )
    _set_csrf(response, new_csrf_token())
    return response


@router.post("/logout")
async def logout(_: None = Depends(require_csrf)):
    response = RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(ADMIN_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return response


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "settings": settings,
            "admin": admin,
            "csrf_token": token,
            "stats": await _collect_stats(db),
        },
    )
    _set_csrf(response, token)
    return response


@router.get("/sessions/{public_id}", response_class=HTMLResponse)
async def session_page(
    public_id: str,
    request: Request,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    session = await _session_or_404(db, public_id)

    participants = (
        await db.scalars(
            select(Participant)
            .where(Participant.session_id == session.id)
            .order_by(Participant.requested_at)
        )
    ).all()
    files = (
        await db.scalars(
            select(SessionFile)
            .where(SessionFile.session_id == session.id)
            .order_by(SessionFile.updated_at.desc())
            .limit(200)
        )
    ).all()
    events = (
        await db.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.session_id == session.id)
            .order_by(ActivityEvent.created_at.desc())
            .limit(100)
        )
    ).all()

    token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "admin_session.html",
        {
            "settings": settings,
            "admin": admin,
            "csrf_token": token,
            "session": session,
            "participants": participants,
            "files": files,
            "events": events,
            "live_ids": hub.live_session_ids(),
            "join_url": settings.join_url(session.join_code),
            "counts": {
                "chat": int(
                    await db.scalar(
                        select(func.count(ChatMessage.id)).where(
                            ChatMessage.session_id == session.id
                        )
                    )
                    or 0
                ),
                "strokes": int(
                    await db.scalar(
                        select(func.count(BoardStroke.id)).where(
                            BoardStroke.session_id == session.id
                        )
                    )
                    or 0
                ),
                "attachments": int(
                    await db.scalar(
                        select(func.count(Attachment.id)).where(
                            Attachment.session_id == session.id
                        )
                    )
                    or 0
                ),
            },
        },
    )
    _set_csrf(response, token)
    return response


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    users = (
        await db.scalars(select(User).order_by(User.created_at.desc()).limit(500))
    ).all()
    token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "settings": settings,
            "admin": admin,
            "csrf_token": token,
            "users": users,
        },
    )
    _set_csrf(response, token)
    return response


@router.get("/sessions/{public_id}/watch", response_class=HTMLResponse)
async def watch_page(
    public_id: str,
    request: Request,
    path: str | None = None,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Look inside a running session without joining it.

    Read-only by construction: this reads the stored snapshot rather than
    opening a socket, so watching can never write a keystroke into somebody
    else's editor or show up in their participant list.
    """
    session = await _session_or_404(db, public_id)
    token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "admin_watch.html",
        {
            "settings": settings,
            "admin": admin,
            "csrf_token": token,
            "session": session,
            "selected": path or "",
            "join_url": settings.join_url(session.join_code),
            "watch_url": f"/admin/api/sessions/{quote(public_id)}/watch",
        },
    )
    _set_csrf(response, token)
    return response


@router.get("/api/sessions/{public_id}/watch")
async def watch_data(
    public_id: str,
    path: str | None = None,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Everything the watch page redraws on a timer."""
    session = await _session_or_404(db, public_id)

    people = (
        await db.scalars(
            select(Participant)
            .where(Participant.session_id == session.id)
            .order_by(Participant.requested_at)
        )
    ).all()
    files = (
        await db.scalars(
            select(SessionFile)
            .where(SessionFile.session_id == session.id)
            .order_by(SessionFile.path)
        )
    ).all()
    events = (
        await db.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.session_id == session.id)
            .order_by(ActivityEvent.created_at.desc())
            .limit(30)
        )
    ).all()

    locks = hub.file_locks(public_id)
    holder_names = {p.id: p.display_name for p in people}

    selected = None
    if path:
        row = next((f for f in files if f.path == path), None)
        if row is not None:
            body = row.content or ""
            selected = {
                "path": row.path,
                "content": body[:WATCH_CONTENT_LIMIT],
                "truncated": len(body) > WATCH_CONTENT_LIMIT,
                "size": row.size,
                "updated_at": row.updated_at.isoformat(),
            }

    return JSONResponse(
        {
            "generated_at": utcnow().isoformat(),
            "status": session.status,
            "title": session.title,
            "online": sum(1 for p in people if p.connected),
            "participants": [
                {
                    "id": p.id,
                    "name": p.display_name,
                    "role": p.role,
                    "state": p.state,
                    "connected": p.connected,
                    "active_file": p.active_file,
                    "edits": p.edits,
                    "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
                }
                for p in people
            ],
            "files": [
                {
                    "path": f.path,
                    "size": f.size,
                    "updated_at": f.updated_at.isoformat(),
                    "locked_by": holder_names.get(locks.get(f.path)) if f.path in locks else None,
                }
                for f in files
            ],
            "chat": await chat_history(db, session),
            "attachments": await attachment_list(db, session),
            "events": [
                {
                    "at": e.created_at.isoformat(),
                    "kind": e.kind,
                    "actor": e.actor,
                    "message": e.message,
                }
                for e in events
            ],
            "selected": selected,
        }
    )


@router.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(
    request: Request,
    done: str | None = None,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "admin_maintenance.html",
        {
            "settings": settings,
            "admin": admin,
            "csrf_token": token,
            "overview": await storage_overview(db),
            "done": done,
        },
    )
    _set_csrf(response, token)
    return response


@router.get("/extension", response_class=HTMLResponse)
async def extension_page(
    request: Request,
    error: str | None = None,
    done: str | None = None,
    admin: User = Depends(current_admin),
) -> HTMLResponse:
    """The build the extension updates itself from, and where to get it."""
    manifest = extension_router.read_manifest()
    package = extension_router.package_path(manifest) if manifest else None
    token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "admin_extension.html",
        {
            "settings": settings,
            "admin": admin,
            "csrf_token": token,
            "manifest": manifest if package is not None else None,
            "size": package.stat().st_size if package is not None else 0,
            "urls": extension_router.manifest_urls(),
            "error": error,
            "done": done,
        },
    )
    _set_csrf(response, token)
    return response


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


@router.post("/sessions/{public_id}/edit")
async def session_edit(
    public_id: str,
    title: str = Form(""),
    max_participants: int = Form(0),
    allow_guests: str | None = Form(None),
    require_approval: str | None = Form(None),
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Change a live session's settings on the host's behalf.

    The same fields the host has in the extension, so an administrator can
    close a session to new guests, or lift approval for a class, without
    having to ask whoever started it.
    """
    session = await _session_or_404(db, public_id)
    if session.status == STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Session has ended")

    clean_title = title.strip()[:200]
    if clean_title:
        session.title = clean_title
    # Checkboxes are absent from the form when unticked, which is exactly
    # what "off" has to mean here.
    session.allow_guests = allow_guests is not None
    session.require_approval = require_approval is not None
    if max_participants:
        session.max_participants = max(1, min(int(max_participants), 500))
    touch(session)

    await log_event(
        db,
        kind="admin.session_edit",
        message=(
            f"{session.title}: guests="
            f"{'open' if session.allow_guests else 'closed'} approval="
            f"{'required' if session.require_approval else 'off'} "
            f"max={session.max_participants}"
        ),
        actor=f"{admin.name} (admin)",
        session=session,
        user=admin,
    )
    await db.commit()
    return RedirectResponse(
        f"/admin/sessions/{public_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/sessions/{public_id}/rotate-code")
async def rotate_code(
    public_id: str,
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Issue a new join code, invalidating every invite already handed out."""
    session = await _session_or_404(db, public_id)
    if session.status == STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Session has ended")

    previous = session.join_code
    session.join_code = await unique_join_code(db)
    await log_event(
        db,
        kind="admin.session_code",
        message=f"join code {previous} -> {session.join_code}",
        actor=f"{admin.name} (admin)",
        session=session,
        user=admin,
    )
    await db.commit()
    return RedirectResponse(
        f"/admin/sessions/{public_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/sessions/{public_id}/delete")
async def session_delete(
    public_id: str,
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Remove a session and everything it was carrying.

    Ending a session leaves the record for the dashboard; this leaves
    nothing at all, which is what you want for one that should not have
    been created in the first place.
    """
    session = await _session_or_404(db, public_id)
    title = session.title
    await delete_session(db, session)
    await log_event(
        db,
        kind="admin.session_deleted",
        message=f"{title} ({public_id}) deleted",
        actor=f"{admin.name} (admin)",
        user=admin,
    )
    await db.commit()
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/maintenance/prune")
async def prune(
    scope: str = Form("sweep"),
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Run housekeeping now instead of waiting for the timer.

    ``sweep`` is exactly what the background task does. The other two ignore
    the retention windows: they are for a server that is out of room today.
    """
    if scope not in ("sweep", "idle", "artefacts", "ended"):
        raise HTTPException(status_code=400, detail="Unknown prune scope")

    if scope == "sweep":
        result = await sweep_once()
        summary = (
            f"ended {result['ended']}, stripped {result['stripped']} row(s), "
            f"deleted {result['detached']} attachment(s), "
            f"purged {result['purged']} session(s)"
        )
    elif scope == "idle":
        count = await end_idle_sessions(db)
        await db.commit()
        summary = f"ended {count} idle session(s)"
    elif scope == "artefacts":
        count = await strip_artefacts(db)
        await db.commit()
        summary = f"freed {count} row(s) from ended sessions"
    else:
        count = await purge_sessions(db)
        await db.commit()
        summary = f"deleted {count} ended session(s)"

    await log_event(
        db,
        kind="admin.prune",
        message=f"{scope}: {summary}",
        actor=f"{admin.name} (admin)",
        user=admin,
    )
    await db.commit()
    return RedirectResponse(
        f"/admin/maintenance?done={quote(summary)}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/extension/publish")
async def publish_extension(
    package: UploadFile = File(...),
    notes: str = Form(""),
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Hand a new build to every sideloaded extension pointed at this server.

    They install it without being asked, so this is the one admin action
    that runs code on other people's machines: the upload is checked for
    being a real .vsix, and its digest is published with it so a client can
    refuse anything that arrives altered.
    """
    data = await package.read()
    try:
        published = extension_router.publish_package(data, notes)
    except extension_router.PublishError as exc:
        return RedirectResponse(
            f"/admin/extension?error={quote(str(exc))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    await log_event(
        db,
        kind="admin.extension_published",
        message=f"{published['version']} ({len(data)} bytes)",
        actor=f"{admin.name} (admin)",
        user=admin,
    )
    await db.commit()
    return RedirectResponse(
        f"/admin/extension?done={quote('published ' + published['version'])}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/extension/unpublish")
async def unpublish_extension(
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Stop offering updates. Installed copies keep working as they are."""
    removed = extension_router.unpublish()
    if removed:
        await log_event(
            db,
            kind="admin.extension_unpublished",
            message="the published build was withdrawn",
            actor=f"{admin.name} (admin)",
            user=admin,
        )
        await db.commit()
    return RedirectResponse(
        f"/admin/extension?done={quote('withdrawn' if removed else 'nothing to withdraw')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/sessions/{public_id}/{action}")
async def session_action(
    public_id: str,
    action: str,
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    if action not in ("pause", "resume", "end"):
        raise HTTPException(status_code=400, detail="Unknown action")

    session = await _session_or_404(db, public_id)

    target = {
        "pause": STATUS_PAUSED,
        "resume": STATUS_ACTIVE,
        "end": STATUS_ENDED,
    }[action]
    await set_status(db, session, target, actor=f"{admin.name} (admin)")
    return RedirectResponse(
        f"/admin/sessions/{public_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/users/{user_id}/{action}")
async def user_action(
    user_id: int,
    action: str,
    admin: User = Depends(current_admin),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    if action not in ("toggle-active", "toggle-admin"):
        raise HTTPException(status_code=400, detail="Unknown action")

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        # Locking yourself out of the only admin account is not a feature.
        raise HTTPException(status_code=400, detail="You cannot change your own access")

    if action == "toggle-active":
        target.is_active = not target.is_active
        kind, note = "admin.user_active", f"{target.email} active={target.is_active}"
    else:
        target.is_admin = not target.is_admin
        kind, note = "admin.user_role", f"{target.email} admin={target.is_admin}"

    await log_event(db, kind=kind, message=note, user=admin, actor=admin.name)
    await db.commit()
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Live data
# --------------------------------------------------------------------------


@router.get("/api/stats")
async def stats(
    admin: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    return JSONResponse(await _collect_stats(db))


async def _collect_stats(db: AsyncSession) -> dict:
    now = utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)

    total_users = int(await db.scalar(select(func.count(User.id))) or 0)
    total_sessions = int(await db.scalar(select(func.count(CollabSession.id))) or 0)

    live_sessions = (
        await db.scalars(
            select(CollabSession)
            .where(CollabSession.status.in_((STATUS_ACTIVE, STATUS_PAUSED)))
            .order_by(CollabSession.last_activity_at.desc())
            .limit(100)
        )
    ).all()

    rows = []
    total_online = 0
    for session in live_sessions:
        people = (
            await db.scalars(
                select(Participant)
                .where(
                    Participant.session_id == session.id,
                    Participant.state.in_((STATE_APPROVED, STATE_PENDING)),
                )
                .order_by(Participant.requested_at)
            )
        ).all()
        online = sum(1 for p in people if p.connected)
        total_online += online
        rows.append(
            {
                "public_id": session.public_id,
                "title": session.title,
                "workspace": session.workspace_name,
                "join_code": session.join_code,
                "status": session.status,
                "host": session.host_name or "unknown",
                "created_at": session.created_at.isoformat(),
                "last_activity_at": session.last_activity_at.isoformat(),
                "online": online,
                "waiting": sum(1 for p in people if p.state == STATE_PENDING),
                "files": int(
                    await db.scalar(
                        select(func.count(SessionFile.id)).where(
                            SessionFile.session_id == session.id
                        )
                    )
                    or 0
                ),
                "participants": [
                    {
                        "id": p.id,
                        "name": p.display_name,
                        "role": p.role,
                        "state": p.state,
                        "connected": p.connected,
                        "active_file": p.active_file,
                        "edits": p.edits,
                        "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
                    }
                    for p in people
                ],
            }
        )

    events = (
        await db.scalars(
            select(ActivityEvent).order_by(ActivityEvent.created_at.desc()).limit(40)
        )
    ).all()

    edits_last_hour = int(
        await db.scalar(
            select(func.count(SessionFile.id)).where(SessionFile.updated_at >= hour_ago)
        )
        or 0
    )
    sessions_today = int(
        await db.scalar(
            select(func.count(CollabSession.id)).where(
                CollabSession.created_at >= day_ago
            )
        )
        or 0
    )

    return {
        "generated_at": now.isoformat(),
        "totals": {
            "users": total_users,
            "sessions_total": total_sessions,
            "sessions_live": len(rows),
            "sessions_today": sessions_today,
            "participants_online": total_online,
            "socket_connections": hub.connection_count(),
            "files_touched_last_hour": edits_last_hour,
        },
        "sessions": rows,
        "events": [
            {
                "at": e.created_at.isoformat(),
                "kind": e.kind,
                "actor": e.actor,
                "message": e.message,
            }
            for e in events
        ],
    }
