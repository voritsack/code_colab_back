"""One-off migration: sessions no longer belong to user accounts.

    python scripts/migrate_accountless.py --yes

`sessions.host_id` was a NOT NULL foreign key into `users`, and participants
carried a `user_id`. Neither exists any more - a session is owned by whoever
holds its host token, and the host is just a display name. There is no
migration framework here, so the affected tables are dropped and rebuilt from
the models on the next start.

This deletes every session, participant, stored file and activity event.
Administrator accounts in `users` are left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine  # noqa: E402

# Children first: dropping a parent while a foreign key still points at it
# fails, and not every deployment created the constraints the same way.
DOOMED = [
    "activity_events",
    "session_files",
    "participants",
    "sessions",
    "refresh_tokens",
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually drop the tables")
    args = parser.parse_args()

    print("database:", settings.database_url.rsplit("@", 1)[-1])
    print("tables to drop:", ", ".join(DOOMED))
    print("kept:          users (administrator accounts)")

    if not args.yes:
        print("\nDry run. Re-run with --yes to drop them.")
        return 0

    async with engine.begin() as conn:
        # MySQL will not drop a table another table still references, and the
        # order above is only correct if every constraint was created as
        # expected. Suspending the check makes the drop deterministic.
        if settings.database_url.startswith(("mysql", "mariadb")):
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in DOOMED:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            print("  dropped", table)
        if settings.database_url.startswith(("mysql", "mariadb")):
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    await engine.dispose()
    print("\nDone. Start the server and it will recreate them from the models.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
