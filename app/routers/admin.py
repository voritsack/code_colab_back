"""Server-rendered admin dashboard.

Deliberately not a SPA: it is a handful of pages plus one JSON endpoint that
the dashboard polls, so the whole product still ships without a frontend
build step.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..actions import set_status
from ..config import settings
from ..db import get_db
from ..hub import hub
from ..models import (
    STATE_APPROVED,
    STATE_PENDING,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    ActivityEvent,
    CollabSession,
    Participant,
    SessionFile,
    User,
    utcnow,
)
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
from ..services import log_event

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
    session = await db.scalar(
        select(CollabSession).where(CollabSession.public_id == public_id)
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

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


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


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

    session = await db.scalar(
        select(CollabSession).where(CollabSession.public_id == public_id)
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

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
