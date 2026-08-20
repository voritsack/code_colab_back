"""Application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select, update
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import settings
from .db import SessionLocal, engine, init_models
from .models import (
    STATUS_ENDED,
    BoardStroke,
    ChatMessage,
    CollabSession,
    Participant,
    SessionFile,
    User,
    utcnow,
)
from .routers import admin, extension, public, sessions, ws
from .security import hash_password
from .templating import STATIC_DIR

logger = logging.getLogger("codecolab")


async def bootstrap_admin() -> None:
    """Make sure someone can sign in to the dashboard on a fresh database."""
    if not settings.admin_email or not settings.admin_password:
        logger.warning(
            "ADMIN_EMAIL/ADMIN_PASSWORD are not set - no admin account was created"
        )
        return

    email = settings.admin_email.strip().lower()
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    name=settings.admin_name,
                    password_hash=hash_password(settings.admin_password),
                    is_admin=True,
                    is_active=True,
                )
            )
            await db.commit()
            logger.info("Created admin account %s", email)
            return

        changed = False
        if not user.is_admin:
            user.is_admin = True
            changed = True
            logger.info("Promoted %s to admin", email)

        if settings.admin_reset_password:
            user.password_hash = hash_password(settings.admin_password)
            changed = True
            logger.warning(
                "Reset the password for %s from ADMIN_PASSWORD. Set "
                "ADMIN_RESET_PASSWORD=false so the next restart leaves it alone.",
                email,
            )

        if changed:
            await db.commit()


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


async def sweep_once() -> None:
    """End idle sessions and delete the ones that finished long ago.

    Without this, a host who closes their laptop leaves a session showing as
    live for good, and every file ever shared stays in the database.
    """
    now = utcnow()
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)

    async with SessionLocal() as db:
        stale = (
            await db.scalars(
                select(CollabSession).where(
                    CollabSession.status != STATUS_ENDED,
                    CollabSession.last_activity_at < idle_cutoff,
                )
            )
        ).all()
        for session in stale:
            session.status = STATUS_ENDED
            session.ended_at = now

        # Stage one: an ended session's contents. These are what actually
        # take up room - a shared workspace can be tens of megabytes - and
        # they are dead weight once the session is over.
        stripped = 0
        if settings.artefact_retention_hours > 0:
            artefact_cutoff = now - timedelta(hours=settings.artefact_retention_hours)
            spent = list(
                (
                    await db.scalars(
                        select(CollabSession.id).where(
                            CollabSession.status == STATUS_ENDED,
                            CollabSession.last_activity_at < artefact_cutoff,
                        )
                    )
                ).all()
            )
            if spent:
                for model in (SessionFile, BoardStroke, ChatMessage):
                    result = await db.execute(
                        delete(model).where(model.session_id.in_(spent))
                    )
                    stripped += result.rowcount or 0

        # Stage two: the session row and its roster, once it is old enough
        # that nobody is going to look it up in the dashboard again.
        purged = 0
        if settings.retention_days > 0:
            old_cutoff = now - timedelta(days=settings.retention_days)
            doomed = list(
                (
                    await db.scalars(
                        select(CollabSession.id).where(
                            CollabSession.status == STATUS_ENDED,
                            CollabSession.ended_at.isnot(None),
                            CollabSession.ended_at < old_cutoff,
                        )
                    )
                ).all()
            )
            if doomed:
                for model in (BoardStroke, ChatMessage, SessionFile, Participant):
                    await db.execute(
                        delete(model).where(model.session_id.in_(doomed))
                    )
                await db.execute(
                    delete(CollabSession).where(CollabSession.id.in_(doomed))
                )
                purged = len(doomed)

        await db.commit()

    if stale or purged or stripped:
        logger.info(
            "Sweep: ended %s idle session(s), freed %s artefact row(s), "
            "purged %s expired session(s)",
            len(stale),
            stripped,
            purged,
        )


async def sweeper() -> None:
    interval = max(settings.sweep_interval_minutes, 1) * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_once()
        except Exception:  # noqa: BLE001 - housekeeping must never kill the app
            logger.exception("Sweep failed")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_models()
    await bootstrap_admin()
    await close_orphaned_sessions()

    task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Close pooled connections on the way out, so the driver is not left
        # tidying up sockets after the event loop has gone.
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Live collaborative coding for VS Code.",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )

    if settings.trusted_hosts:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts
        )

    # The VS Code extension is not a browser and sends no Origin, so this list
    # stays empty unless you deliberately add a web client.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if settings.use_secure_cookies:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith(("/admin", "/j/", "/")) and response.headers.get(
            "content-type", ""
        ).startswith("text/html"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'",
            )
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if request.url.path.startswith("/admin"):
            return RedirectResponse("/admin/login", status_code=303)
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.exception_handler(401)
    async def unauthorized(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith("/admin"):
            return RedirectResponse("/admin/login", status_code=303)
        return JSONResponse({"detail": getattr(exc, "detail", "Unauthorized")}, status_code=401)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(public.router)
    app.include_router(extension.router)
    app.include_router(sessions.router)
    app.include_router(ws.router)
    app.include_router(admin.router)

    return app


app = create_app()
