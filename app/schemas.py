"""Request and response bodies."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .config import settings
from .models import ROLE_EDITOR, ROLE_VIEWER
from .utils import UnsafePathError, normalize_join_code, sanitize_relative_path


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=4096)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    name: str
    is_admin: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


class SessionCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    workspace_name: str = Field(default="", max_length=200)
    allow_guests: bool | None = None
    require_approval: bool | None = None
    max_participants: int | None = Field(default=None, ge=2, le=200)


class SessionSettingsIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    allow_guests: bool | None = None
    require_approval: bool | None = None


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
    is_guest: bool
    role: str
    state: str
    connected: bool
    active_file: str | None
    edits: int
    requested_at: datetime
    approved_at: datetime | None
    last_seen_at: datetime | None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: str
    join_code: str
    title: str
    workspace_name: str
    status: str
    allow_guests: bool
    require_approval: bool
    max_participants: int
    created_at: datetime
    host_name: str = ""
    participant_count: int = 0
    join_url: str = ""
    vscode_link: str = ""


class SessionDetailOut(SessionOut):
    participants: list[ParticipantOut] = []
    file_count: int = 0


class SessionCreatedOut(SessionOut):
    session_token: str
    participant_id: int


class JoinIn(BaseModel):
    code: str = Field(min_length=3, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)
    client_id: str | None = Field(default=None, max_length=64)

    @field_validator("code")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_join_code(value)


class JoinOut(BaseModel):
    public_id: str
    title: str
    host_name: str
    status: str
    state: str
    role: str
    participant_id: int
    session_token: str
    ws_url: str


class RoleUpdateIn(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _check(cls, value: str) -> str:
        if value not in (ROLE_EDITOR, ROLE_VIEWER):
            raise ValueError("role must be 'editor' or 'viewer'")
        return value


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


class FileIn(BaseModel):
    path: str = Field(min_length=1)
    content: str = ""

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        try:
            return sanitize_relative_path(value)
        except UnsafePathError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("content")
    @classmethod
    def _size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > settings.max_file_bytes:
            raise ValueError(
                f"File exceeds {settings.max_file_bytes} bytes"
            )
        return value


class SnapshotIn(BaseModel):
    files: list[FileIn]

    @field_validator("files")
    @classmethod
    def _limits(cls, value: list[FileIn]) -> list[FileIn]:
        if len(value) > settings.max_files_per_snapshot:
            raise ValueError(
                f"Too many files (max {settings.max_files_per_snapshot})"
            )
        total = sum(len(item.content.encode("utf-8")) for item in value)
        if total > settings.max_snapshot_bytes:
            raise ValueError(
                f"Snapshot exceeds {settings.max_snapshot_bytes} bytes"
            )
        return value


class FileOut(BaseModel):
    path: str
    content: str
    updated_at: datetime


class SnapshotOut(BaseModel):
    public_id: str
    status: str
    files: list[FileOut]


class MessageOut(BaseModel):
    detail: str
