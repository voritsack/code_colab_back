"""Application configuration.

Every tunable lives here and is read from the environment (or a local .env
file). Nothing in the codebase should hardcode a host, secret or limit.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_file() -> str:
    """Where to look for a .env file.

    Real environment variables always win over the file, so a host that
    injects its own configuration needs no file at all. ENV_FILE overrides
    the search; otherwise the project root is tried, then the working
    directory.
    """
    explicit = os.environ.get("ENV_FILE")
    if explicit:
        return explicit
    candidate = PROJECT_ROOT / ".env"
    if candidate.exists():
        return str(candidate)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file(),
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

    # Containers hand the port over in an environment variable and expect the
    # process to bind every interface. SERVER_PORT is checked first because
    # that is what game-server style panels set; PORT covers most PaaS hosts.
    host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("HOST", "SERVER_HOST", "BIND_HOST"),
    )
    port: int = Field(
        default=8000,
        validation_alias=AliasChoices("SERVER_PORT", "PORT", "BIND_PORT"),
    )

    # Set only when a reverse proxy sits in front. When the server is exposed
    # directly, trusting X-Forwarded-For would let any caller spoof their
    # address and walk straight past the rate limiter.
    trust_proxy_headers: bool = False

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
    # When true, the admin account's password is reset from ADMIN_PASSWORD on
    # every start. Off by default so a restart cannot quietly undo a password
    # somebody changed elsewhere; turn it on for one boot to rotate, then off.
    admin_reset_password: bool = False

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
    # Generous on purpose: a class signing up together shares one NAT address.
    register_rate_limit: int = 20
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


REQUIRED_HINT = """
CodeColab could not start: its configuration is incomplete.

  Looked for a .env file at: {path}

Every setting can also come from real environment variables, which take
precedence over the file. At minimum you need SECRET_KEY (32+ characters)
and DATABASE_URL. See .env.example for the full list.
"""


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # A pydantic traceback is a poor first impression in a container log.
        print(REQUIRED_HINT.format(path=_env_file()), file=sys.stderr)
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


settings = get_settings()
