"""Database models.

Three ideas: a collaboration session, the people inside it, and the files
they are editing. Everything else is analytics.

Accounts exist only for the admin dashboard. Hosting and joining need no
account at all - identity inside a session is a display name plus a
session-scoped token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# MySQL/MariaDB TEXT tops out at 64 KiB, which is below MAX_FILE_BYTES.
# Everywhere else plain TEXT is unbounded, so only the MySQL dialect needs
# the wider type.
FileText = Text().with_variant(LONGTEXT(), "mysql")


def utcnow() -> datetime:
    """Naive UTC timestamp.

    Stored naive so SQLite and MySQL behave like PostgreSQL here; every
    comparison in the codebase is naive-UTC too.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --- Roles and states, kept as plain strings so SQLite stays readable ----

ROLE_HOST = "host"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_HOST, ROLE_EDITOR, ROLE_VIEWER)

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"
STATE_REMOVED = "removed"
STATE_LEFT = "left"

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_ENDED = "ended"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CollabSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    join_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(200))
    workspace_name: Mapped[str] = mapped_column(String(200), default="")

    # Whoever started it, by the name they typed. There is no account behind
    # this: the host is identified by holding a session token with role=host.
    host_name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)

    allow_guests: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_participants: Mapped[int] = mapped_column(Integer, default=25, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, index=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    files: Mapped[list["SessionFile"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    @property
    def is_live(self) -> bool:
        return self.status in (STATUS_ACTIVE, STATUS_PAUSED)


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(10), default=ROLE_VIEWER, nullable=False)
    state: Mapped[str] = mapped_column(
        String(12), default=STATE_PENDING, nullable=False, index=True
    )

    connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Live analytics: what this person is looking at, and how busy they are.
    active_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    edits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    session: Mapped[CollabSession] = relationship(back_populates="participants")

    @property
    def can_edit(self) -> bool:
        return self.state == STATE_APPROVED and self.role in (ROLE_HOST, ROLE_EDITOR)


class SessionFile(Base):
    __tablename__ = "session_files"
    __table_args__ = (
        UniqueConstraint("session_id", "path", name="uq_session_file_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(FileText, default="")
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )

    session: Mapped[CollabSession] = relationship(back_populates="files")


class ChatMessage(Base):
    """Session chat. Kept so somebody admitted late can read what they missed."""

    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_session_created", "session_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class BoardStroke(Base):
    """One pen stroke on the shared board.

    Stored rather than kept in memory so the board is still there for someone
    admitted halfway through, and survives a server restart.
    """

    __tablename__ = "board_strokes"
    __table_args__ = (Index("ix_stroke_session_id", "session_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    color: Mapped[str] = mapped_column(String(9), default="#1ABCFE")
    width: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    tool: Mapped[str] = mapped_column(String(10), default="pen")
    # A JSON array of [x, y] pairs in board coordinates (0..1 of the canvas),
    # so the drawing lands in the same place whatever size the panel is.
    points: Mapped[str] = mapped_column(FileText, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Attachment(Base):
    """A file handed round the session that is not part of the project.

    An image, a zip, a PDF - things nobody wants merged into the workspace
    but everybody needs a copy of. The bytes live on disk rather than in the
    database: this server talks to shared hosting with a 16 MB packet limit,
    and a row per megabyte is the wrong shape for it.

    Deleted with the session, by design. Nothing here is meant to outlive it.
    """

    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachment_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by: Mapped[str] = mapped_column(String(120), default="")

    # What the uploader called it, sanitised for display and for saving.
    name: Mapped[str] = mapped_column(String(255))
    # What it is called on disk: opaque, so a crafted name cannot escape.
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ActivityEvent(Base):
    """Append-only feed powering the admin dashboard."""

    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_session_created", "session_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(String(500), default="")
    actor: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, index=True
    )
