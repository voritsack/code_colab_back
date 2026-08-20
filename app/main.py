"""Application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import settings
from .db import SessionLocal, init_models
from .models import User
from .routers import admin, auth, public, sessions, ws
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
        elif not user.is_admin:
            user.is_admin = True
            await db.commit()
            logger.info("Promoted %s to admin", email)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_models()
    await bootstrap_admin()
    yield


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
    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(ws.router)
    app.include_router(admin.router)

    return app


app = create_app()
