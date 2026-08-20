"""Account creation and token issuance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import RefreshToken, User, utcnow
from ..ratelimit import RateLimiter, enforce
from ..schemas import LoginIn, MessageOut, RefreshIn, RegisterIn, TokenPair, UserOut
from ..security import (
    TOKEN_REFRESH,
    create_access_token,
    create_refresh_token,
    current_user,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from ..services import log_event

router = APIRouter(prefix="/api/auth", tags=["auth"])

_login_limiter = RateLimiter(
    settings.login_rate_limit, settings.login_rate_window_seconds
)
_register_limiter = RateLimiter(
    settings.register_rate_limit, settings.register_rate_window_seconds
)


async def _issue_tokens(
    db: AsyncSession, user: User, request: Request
) -> TokenPair:
    access = create_access_token(user)
    refresh, expires_at = create_refresh_token(user)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh),
            expires_at=expires_at,
            user_agent=(request.headers.get("user-agent") or "")[:255],
        )
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenPair:
    if not settings.allow_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled on this server",
        )
    enforce(_register_limiter, request, "register")

    email = payload.email.lower()
    exists = await db.scalar(select(User.id).where(User.email == email))
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    await db.flush()

    tokens = await _issue_tokens(db, user, request)
    await log_event(db, kind="user.registered", message=email, user=user)
    await db.commit()
    return tokens


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenPair:
    enforce(_login_limiter, request, "login")

    email = payload.email.lower()
    user = await db.scalar(select(User).where(User.email == email))

    # Same response for "no such user" and "wrong password" so the endpoint
    # cannot be used to enumerate accounts.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled"
        )

    user.last_login_at = utcnow()
    tokens = await _issue_tokens(db, user, request)
    await log_event(db, kind="user.login", message=email, user=user)
    await db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenPair:
    claims = decode_token(payload.refresh_token, TOKEN_REFRESH)
    token_hash = hash_token(payload.refresh_token)

    stored = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    if stored is None or stored.revoked_at is not None or stored.expires_at < utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token is not valid"
        )

    user = await db.scalar(select(User).where(User.id == int(claims["sub"])))
    if user is None or not user.is_active or user.id != stored.user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable"
        )

    # Rotate: the presented token dies here whether or not the client keeps it.
    stored.revoked_at = utcnow()
    tokens = await _issue_tokens(db, user, request)
    await db.commit()
    return tokens


@router.post("/logout", response_model=MessageOut)
async def logout(
    payload: RefreshIn, db: AsyncSession = Depends(get_db)
) -> MessageOut:
    stored = await db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = utcnow()
        await db.commit()
    return MessageOut(detail="Signed out")


@router.post("/logout-all", response_model=MessageOut)
async def logout_all(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> MessageOut:
    tokens = (
        await db.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).all()
    now = utcnow()
    for token in tokens:
        token.revoked_at = now
    await db.commit()
    return MessageOut(detail=f"Revoked {len(tokens)} session(s)")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)
