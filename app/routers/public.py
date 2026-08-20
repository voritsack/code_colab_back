"""Unauthenticated surface: health, service info, and the join landing page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import STATE_APPROVED, STATUS_ENDED, CollabSession, Participant
from ..templating import templates
from ..services import get_session_by_code
from ..utils import normalize_join_code

router = APIRouter(tags=["public"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/info")
async def info() -> dict[str, object]:
    """What a client needs to know before it has a token."""
    return {
        "app": settings.app_name,
        "requires_host_code": bool(settings.host_access_code),
        "allow_guests_default": settings.allow_guests_default,
        "public_base_url": settings.public_base_url,
        "extension_id": settings.vscode_extension_id,
        "limits": {
            "max_file_bytes": settings.max_file_bytes,
            "max_files_per_snapshot": settings.max_files_per_snapshot,
            "max_participants": settings.max_participants,
        },
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"settings": settings}
    )


@router.get("/j/{code}", response_class=HTMLResponse)
async def join_page(
    code: str, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    """The page an invite link opens.

    It exists for one reason: to hand the browser off to VS Code. There is no
    web editor here on purpose - the whole product lives inside the IDE.
    """
    normalized = normalize_join_code(code)
    session = await get_session_by_code(db, normalized)

    context: dict[str, object] = {
        "settings": settings,
        "code": normalized,
        "session": None,
        "host_name": "",
        "participants": 0,
        "deep_link": settings.vscode_deep_link(normalized),
    }

    if session is not None:
        context["session"] = session
        context["host_name"] = session.host_name or "the host"
        context["participants"] = int(
            await db.scalar(
                select(func.count(Participant.id)).where(
                    Participant.session_id == session.id,
                    Participant.state == STATE_APPROVED,
                )
            )
            or 0
        )

    return templates.TemplateResponse(request, "join.html", context)
