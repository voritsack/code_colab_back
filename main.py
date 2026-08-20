"""Entry point.

Hosting panels run one named file, so this is it. It reads the port the host
assigned, binds every interface, and starts a single uvicorn worker.

One worker is not a shortcut: the WebSocket hub lives in this process's
memory, so two workers would scatter the participants of one session across
processes that cannot see each other.

Locally you can still use `python -m uvicorn app.main:app --reload`.
"""

from __future__ import annotations

import logging
import sys

from app.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("codecolab.boot")


def banner() -> None:
    logger.info("%s starting", settings.app_name)
    logger.info("  environment   %s", settings.environment)
    logger.info("  binding       %s:%s", settings.host, settings.port)
    logger.info("  public url    %s", settings.public_base_url)
    logger.info("  admin         %s/admin", settings.public_base_url)
    logger.info("  database      %s", _safe_database_url())
    # Printed because a mismatch between this and the published extension is
    # invisible at runtime: invite links simply stop opening anything.
    logger.info("  extension id  %s", settings.vscode_extension_id)
    logger.info("  proxy headers %s", "trusted" if settings.trust_proxy_headers else "ignored")

    if not settings.public_base_url.startswith("https://"):
        logger.warning(
            "PUBLIC_BASE_URL is not https. Admin passwords, tokens and the "
            "shared source code all travel in cleartext, and cookies cannot "
            "be marked Secure. Put TLS in front before real use."
        )
    if settings.debug:
        logger.warning("DEBUG is on: /api/docs is public. Turn it off in production.")
    if not settings.admin_email or not settings.admin_password:
        logger.warning("No ADMIN_EMAIL/ADMIN_PASSWORD: the dashboard will have no account.")
    if settings.vscode_extension_id.startswith("local."):
        logger.warning(
            "VSCODE_EXTENSION_ID is still %s. If the extension has been "
            "published under a real publisher, every 'Open in VS Code' link "
            "this server hands out will silently do nothing.",
            settings.vscode_extension_id,
        )


def _safe_database_url() -> str:
    """The database URL with the password removed, for logging."""
    url = settings.database_url
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    credentials, _, location = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{location}"


def main() -> int:
    import uvicorn

    banner()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="debug" if settings.debug else "info",
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips="*" if settings.trust_proxy_headers else None,
        # Frames larger than the app's own cap are rejected at the protocol
        # layer, before any of our code allocates for them.
        ws_max_size=settings.max_ws_message_bytes + 8192,
        timeout_graceful_shutdown=10,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
