"""Passwords, tokens and the auth dependencies built on top of them.

Three token flavours, all HS256 JWTs signed with SECRET_KEY:

* ``access``  - short-lived, identifies a user to the REST API.
* ``refresh`` - long-lived, single-use, hashed in the database so it can be
                revoked. Exchanging one rotates it.
* ``session`` - scoped to exactly one collaboration session and one
                participant. This is the only token the WebSocket accepts,
                so a leaked session token cannot touch the rest of the API.

A fourth, ``admin``, lives in an httpOnly cookie for the dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import (
    STATE_APPROVED,
    STATE_PENDING,
    STATUS_ENDED,
    CollabSession,
    Participant,
    User,
    utcnow,
)

ALGORITHM = "HS256"

TOKEN_ACCESS = "access"
TOKEN_REFRESH = "refresh"
TOKEN_SESSION = "session"
TOKEN_ADMIN = "admin"

ADMIN_COOKIE = "codecolab_admin"
CSRF_COOKIE = "codecolab_csrf"

# bcrypt silently ignores everything past 72 bytes and raises on longer input
# in 4.x, so truncate explicitly rather than letting it surprise us.
_BCRYPT_MAX_BYTES = 72


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


def create_token(
    *,
    subject: str | int,
    token_type: str,
    expires_delta: timedelta,
    claims: dict[str, Any] | None = None,
) -> str:
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": secrets.token_urlsafe(12),
    }
    if claims:
        payload.update(claims)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    if payload.get("typ") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong token type"
        )
    return payload


def create_access_token(user: User) -> str:
    return create_token(
        subject=user.id,
        token_type=TOKEN_ACCESS,
        expires_delta=timedelta(minutes=settings.access_token_ttl_minutes),
        claims={"email": user.email, "adm": user.is_admin},
    )


def create_refresh_token(user: User) -> tuple[str, datetime]:
    expires_at = utcnow() + timedelta(days=settings.refresh_token_ttl_days)
    token = create_token(
        subject=user.id,
        token_type=TOKEN_REFRESH,
        expires_delta=timedelta(days=settings.refresh_token_ttl_days),
    )
    return token, expires_at


def create_session_token(*, participant_id: int, session_public_id: str, role: str) -> str:
    return create_token(
        subject=participant_id,
        token_type=TOKEN_SESSION,
        expires_delta=timedelta(hours=settings.session_token_ttl_hours),
        claims={"sid": session_public_id, "role": role},
    )


def create_admin_token(user: User) -> str:
    return create_token(
        subject=user.id,
        token_type=TOKEN_ADMIN,
        expires_delta=timedelta(minutes=settings.admin_session_ttl_minutes),
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return None


async def load_active_user(db: AsyncSession, user_id: str | int) -> User:
    try:
        pk = int(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject"
        ) from exc

    user = await db.scalar(select(User).where(User.id == pk))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable"
        )
    return user


async def current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    token = bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(token, TOKEN_ACCESS)
    return await load_active_user(db, payload.get("sub", ""))


async def optional_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    """Same as ``current_user`` but tolerates anonymous callers.

    Used by the join endpoint, which serves both signed-in users and guests.
    """
    token = bearer_token(request)
    if not token:
        return None
    payload = decode_token(token, TOKEN_ACCESS)
    return await load_active_user(db, payload.get("sub", ""))


async def current_admin(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """Admin identity for the dashboard, read from the httpOnly cookie."""
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in")
    payload = decode_token(token, TOKEN_ADMIN)
    user = await load_active_user(db, payload.get("sub", ""))
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not an admin")
    return user


class SessionAuthError(Exception):
    """Raised by ``resolve_session_token`` so WebSocket code can close cleanly."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def resolve_session_token(
    db: AsyncSession, token: str
) -> tuple[Participant, CollabSession]:
    """Validate a session-scoped token and return who and where it belongs to.

    Plain function rather than a dependency so the WebSocket handler, which
    has no ``Request``, can reuse exactly the same checks.
    """
    try:
        payload = decode_token(token, TOKEN_SESSION)
    except HTTPException as exc:
        raise SessionAuthError(str(exc.detail)) from exc

    try:
        participant_id = int(payload.get("sub", ""))
    except (TypeError, ValueError) as exc:
        raise SessionAuthError("Invalid token subject") from exc

    participant = await db.get(Participant, participant_id)
    if participant is None:
        raise SessionAuthError("Participant no longer exists")
    if participant.state not in (STATE_APPROVED, STATE_PENDING):
        raise SessionAuthError(f"Participant is {participant.state}")

    session = await db.get(CollabSession, participant.session_id)
    if session is None:
        raise SessionAuthError("Session no longer exists")
    if session.public_id != payload.get("sid"):
        raise SessionAuthError("Token does not match this session")
    if session.status == STATUS_ENDED:
        raise SessionAuthError("Session has ended")

    return participant, session


async def session_context(
    request: Request, db: AsyncSession = Depends(get_db)
) -> tuple[Participant, CollabSession]:
    token = bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await resolve_session_token(db, token)
    except SessionAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.reason
        ) from exc


async def require_csrf(request: Request) -> None:
    """Double-submit CSRF check for admin form posts."""
    cookie_token = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    form_token = form.get("csrf_token")
    if not csrf_matches(cookie_token, str(form_token) if form_token else None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF check failed"
        )
