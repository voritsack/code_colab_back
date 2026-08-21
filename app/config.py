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
    # The only accounts that exist are administrators, created from these on
    # startup. Hosting and joining a session need no account at all.
    admin_email: str | None = None
    admin_password: str | None = None
    admin_name: str = "Administrator"
    # When true, the admin account's password is reset from ADMIN_PASSWORD on
    # every start. Off by default so a restart cannot quietly undo a password
    # somebody changed elsewhere; turn it on for one boot to rotate, then off.
    admin_reset_password: bool = False

    # -- Sessions --------------------------------------------------------
    # Optional shared secret required to *create* a session (never to join
    # one). Empty means anybody who can reach the server can host, which is
    # the open default. Set it if the address is public and you would rather
    # not hand strangers a file-transfer service.
    host_access_code: str = ""

    allow_guests_default: bool = True
    require_approval_default: bool = True
    max_participants: int = 25
    session_idle_timeout_minutes: int = 240

    # A session with nobody connected ends itself after this long. The delay
    # matters: without it a dropped wifi connection would end the session
    # before the host's laptop had finished reconnecting.
    empty_session_grace_seconds: int = 120

    # Housekeeping, run in the background rather than by hand. Ended sessions
    # keep the files that were shared into them, so they should not live
    # forever; 0 disables the purge.
    # Two stages, because the two things cost differently. A finished
    # session's contents are the bulk of the storage and nobody needs them
    # once everyone has their copy, so they go first. The session row itself
    # is a few hundred bytes and is what the dashboard reports on, so it
    # lingers. 0 disables either stage.
    artefact_retention_hours: int = 6
    retention_days: int = 30
    sweep_interval_minutes: int = 60

    # -- Payload limits --------------------------------------------------
    max_file_bytes: int = 512_000
    max_files_per_snapshot: int = 2_000
    max_snapshot_bytes: int = 25_000_000
    max_ws_message_bytes: int = 2_000_000
    max_path_length: int = 400

    # -- Chat and the shared board ---------------------------------------
    max_chat_length: int = 2000
    max_chat_history: int = 300
    max_board_strokes: int = 4000
    max_stroke_points: int = 600

    # How long one person keeps a file to themselves after typing in it.
    # Long enough to cover a pause for thought, short enough that stepping
    # away does not block anyone.
    file_lock_seconds: int = 12

    # -- Attachments ------------------------------------------------------
    # Files passed round a session that are not part of the project. Stored
    # on disk, not in the database - shared hosting caps a single row write
    # at 16 MB and would not thank us for the traffic either.
    attachment_dir: str = "./data/attachments"
    max_attachment_bytes: int = 25_000_000
    max_session_attachment_bytes: int = 150_000_000
    max_attachments_per_session: int = 60

    # -- Rate limits (requests per window) -------------------------------
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300
    join_rate_limit: int = 20
    join_rate_window_seconds: int = 300
    # Generous on purpose: a class signing up together shares one NAT address.
    session_create_rate_limit: int = 20
    session_create_rate_window_seconds: int = 3600
    ws_message_rate_limit: int = 240
    ws_message_rate_window_seconds: int = 10

    # -- Database --------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./codecolab.db"
    database_echo: bool = False

    # -- VS Code deep link ----------------------------------------------
    # <publisher>.<name> from the extension's package.json.
    vscode_extension_id: str = "code-colab.codecolab"
    vscode_marketplace_url: str = ""

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("vscode_extension_id")
    @classmethod
    def _lowercase_extension_id(cls, value: str) -> str:
        """An extension id is case-insensitive; the deep link is not quite.

        `<publisher>.<name>` sits in the authority of `vscode://<id>/join`,
        and hosting panels have a habit of shouting environment values back.
        VS Code lowercases the authority when it parses the URI, so an
        upper-case id still resolves - but the link we print, log and hand to
        people reads wrong, and anything comparing it as a string disagrees
        with the manifest. Normalise once, here.
        """
        return value.strip().lower()

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
