"""Application configuration.

Every tunable lives here and is read from the environment (or a local .env
file). Nothing in the codebase should hardcode a host, secret or limit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Resolved from the package so the server can be started from any directory.
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # -- General ---------------------------------------------------------
    app_name: str = "CodeColab"
    environment: str = "development"
    debug: bool = False

    # Public origin used to build shareable join links. Must be reachable by
    # the people you invite (use a tunnel or a real host, not 127.0.0.1).
    public_base_url: str = "http://127.0.0.1:8000"

    host: str = "127.0.0.1"
    port: int = 8000

    # -- Security --------------------------------------------------------
    secret_key: str = Field(..., min_length=32)
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30
    session_token_ttl_hours: int = 12
    admin_session_ttl_minutes: int = 480

    # Comma-separated. Empty means "no browser origin may call the API",
    # which is the right default: the VS Code extension is not a browser.
    # Kept as raw strings because pydantic-settings tries to JSON-decode any
    # list-typed field before a validator gets a chance to split it.
    cors_origins_raw: str = Field(default="", alias="CORS_ORIGINS")
    trusted_hosts_raw: str = Field(default="", alias="TRUSTED_HOSTS")

    # Force HTTPS-only cookies. Leave unset to derive it from public_base_url.
    secure_cookies: bool | None = None

    # -- Accounts --------------------------------------------------------
    allow_registration: bool = True
    admin_email: str | None = None
    admin_password: str | None = None
    admin_name: str = "Administrator"

    # -- Sessions --------------------------------------------------------
    allow_guests_default: bool = True
    require_approval_default: bool = True
    max_participants: int = 25
    session_idle_timeout_minutes: int = 240

    # -- Payload limits --------------------------------------------------
    max_file_bytes: int = 512_000
    max_files_per_snapshot: int = 2_000
    max_snapshot_bytes: int = 25_000_000
    max_ws_message_bytes: int = 2_000_000
    max_path_length: int = 400

    # -- Rate limits (requests per window) -------------------------------
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300
    join_rate_limit: int = 20
    join_rate_window_seconds: int = 300
    register_rate_limit: int = 5
    register_rate_window_seconds: int = 3600
    ws_message_rate_limit: int = 240
    ws_message_rate_window_seconds: int = 10

    # -- Database --------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./codecolab.db"
    database_echo: bool = False

    # -- VS Code deep link ----------------------------------------------
    # <publisher>.<name> from the extension's package.json.
    vscode_extension_id: str = "local.codecolab"
    vscode_marketplace_url: str = ""

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return self._split_csv(self.cors_origins_raw)

    @property
    def trusted_hosts(self) -> list[str]:
        return self._split_csv(self.trusted_hosts_raw)

    @property
    def use_secure_cookies(self) -> bool:
        if self.secure_cookies is not None:
            return self.secure_cookies
        return self.public_base_url.startswith("https://")

    def join_url(self, code: str) -> str:
        return f"{self.public_base_url}/j/{code}"

    def vscode_deep_link(self, code: str) -> str:
        return (
            f"vscode://{self.vscode_extension_id}/join"
            f"?code={code}&server={self.public_base_url}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
