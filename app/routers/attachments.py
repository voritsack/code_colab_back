"""Passing files round a session.

Separate from the workspace sync on purpose. The sync is for code: text,
edited live, merged into everyone's folder. This is for the other thing -
an image, a zip, a PDF - which nobody wants appearing in their project tree
but everybody needs a copy of.

Everything here dies with the session.
"""

from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..actions import broadcast_attachments
from ..config import settings
from ..db import get_db
from ..hub import hub
from ..models import (
    ROLE_HOST,
    STATE_APPROVED,
    STATUS_ENDED,
    Attachment,
    CollabSession,
    Participant,
)
from ..schemas import AttachmentOut, MessageOut
from ..security import session_context
from ..services import log_event, touch

router = APIRouter(prefix="/api/sessions/{public_id}/attachments", tags=["attachments"])

CHUNK = 1024 * 256


def _serialise(row: Attachment) -> AttachmentOut:
    return AttachmentOut(
        id=row.id,
        name=row.name,
        size=row.size,
        content_type=row.content_type,
        uploaded_by=row.uploaded_by,
        participant_id=row.participant_id,
        created_at=row.created_at,
    )


async def _context(
    public_id: str, context: tuple[Participant, CollabSession]
) -> tuple[Participant, CollabSession]:
    participant, session = context
    if session.public_id != public_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token is for another session"
        )
    if participant.state != STATE_APPROVED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Waiting for host approval"
        )
    return participant, session


@router.get("", response_model=list[AttachmentOut])
async def list_attachments(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(session_context),
    db: AsyncSession = Depends(get_db),
) -> list[AttachmentOut]:
    _, session = await _context(public_id, context)
    rows = (
        await db.scalars(
            select(Attachment)
            .where(Attachment.session_id == session.id)
            .order_by(Attachment.id)
        )
    ).all()
    return [_serialise(row) for row in rows]


@router.post("", response_model=AttachmentOut, status_code=status.HTTP_201_CREATED)
async def upload(
    public_id: str,
    file: UploadFile = File(...),
    context: tuple[Participant, CollabSession] = Depends(session_context),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    participant, session = await _context(public_id, context)
    if session.status == STATUS_ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Session has ended"
        )

    count = int(
        await db.scalar(
            select(func.count(Attachment.id)).where(Attachment.session_id == session.id)
        )
        or 0
    )
    if count >= settings.max_attachments_per_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This session already has {count} attachments",
        )

    used = int(
        await db.scalar(
            select(func.coalesce(func.sum(Attachment.size), 0)).where(
                Attachment.session_id == session.id
            )
        )
        or 0
    )

    stored_name = storage.new_stored_name()
    written = 0
    digest = ""

    def chunks():
        """Stream it, checking the limits as we go.

        Reading the whole upload into memory first would let one caller
        allocate whatever they liked before any limit was consulted.
        """
        nonlocal written
        while True:
            chunk = file.file.read(CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > settings.max_attachment_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Files are limited to {settings.max_attachment_bytes} bytes",
                )
            if used + written > settings.max_session_attachment_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="This session has no room left for attachments",
                )
            yield chunk

    try:
        size, digest = storage.write(stored_name, chunks())
    except HTTPException:
        storage.remove(stored_name)
        raise
    except Exception as exc:  # noqa: BLE001
        storage.remove(stored_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not store the file: {exc}",
        ) from exc

    if size == 0:
        storage.remove(stored_name)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That file is empty"
        )

    row = Attachment(
        session_id=session.id,
        participant_id=participant.id,
        uploaded_by=participant.display_name,
        name=storage.display_name(file.filename or "file"),
        stored_name=stored_name,
        content_type=(file.content_type or "application/octet-stream")[:120],
        size=size,
        sha256=digest,
    )
    db.add(row)
    touch(session)
    await log_event(
        db,
        kind="attachment.added",
        message=f"{row.name} ({size} bytes)",
        actor=participant.display_name,
        session=session,
        participant=participant,
    )
    await db.commit()

    await broadcast_attachments(db, session)
    return _serialise(row)


@router.get("/bundle.zip")
async def bundle(
    public_id: str,
    context: tuple[Participant, CollabSession] = Depends(session_context),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Every attachment in one archive, so a batch is a single save."""
    _, session = await _context(public_id, context)
    rows = (
        await db.scalars(
            select(Attachment)
            .where(Attachment.session_id == session.id)
            .order_by(Attachment.id)
        )
    ).all()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Nothing attached yet"
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        seen: dict[str, int] = {}
        for row in rows:
            path = storage.path_for(row.stored_name)
            if path is None or not path.exists():
                continue
            # Two people uploading "screenshot.png" must not collide.
            name = row.name
            if name in seen:
                seen[name] += 1
                stem, _, suffix = name.rpartition(".")
                name = (
                    f"{stem} ({seen[name]}).{suffix}" if stem else f"{name} ({seen[name]})"
                )
            else:
                seen[name] = 0
            archive.write(path, arcname=name)
    buffer.seek(0)

    safe_title = storage.display_name(session.title or "session")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}-files.zip"'
        },
    )


@router.get("/{attachment_id}")
async def download(
    public_id: str,
    attachment_id: int,
    context: tuple[Participant, CollabSession] = Depends(session_context),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    _, session = await _context(public_id, context)
    row = await db.get(Attachment, attachment_id)
    if row is None or row.session_id != session.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such attachment"
        )

    path = storage.path_for(row.stored_name)
    if path is None or not path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="That file is no longer stored"
        )
    return FileResponse(path, media_type=row.content_type, filename=row.name)


@router.delete("/{attachment_id}", response_model=MessageOut)
async def detach(
    public_id: str,
    attachment_id: int,
    context: tuple[Participant, CollabSession] = Depends(session_context),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    participant, session = await _context(public_id, context)
    row = await db.get(Attachment, attachment_id)
    if row is None or row.session_id != session.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such attachment"
        )

    # Your own file, or anything at all if you are running the session.
    if participant.role != ROLE_HOST and row.participant_id != participant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the host can remove someone else's file",
        )

    storage.remove(row.stored_name)
    await db.delete(row)
    await log_event(
        db,
        kind="attachment.removed",
        message=row.name,
        actor=participant.display_name,
        session=session,
        participant=participant,
    )
    await db.commit()

    await broadcast_attachments(db, session)
    return MessageOut(detail=f"Removed {row.name}")
