"""Database maintenance.

    python scripts/cleanup.py --stale
    python scripts/cleanup.py --purge-ended 30 --yes
    python scripts/cleanup.py --all --yes

Nothing is deleted without ``--yes``: every run is a dry run first and prints
exactly what it would touch. Rows are removed in dependency order rather than
leaning on ON DELETE CASCADE, so the result is the same whatever the schema
was actually created with.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

# Allow `python scripts/cleanup.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    STATUS_ENDED,
    ActivityEvent,
    CollabSession,
    Participant,
    SessionFile,
    User,
    utcnow,
)


async def counts(db: AsyncSession) -> dict[str, int]:
    out = {}
    for model in (User, CollabSession, Participant, SessionFile, ActivityEvent):
        out[model.__tablename__] = int(
            await db.scalar(select(func.count()).select_from(model)) or 0
        )
    return out


def show(label: str, before: dict[str, int], after: dict[str, int]) -> None:
    print(f"\n{label}")
    for table, was in before.items():
        now = after[table]
        change = f"  -{was - now}" if was != now else ""
        print(f"  {table:<18} {was:>6} -> {now:>6}{change}")


async def drop_sessions(db: AsyncSession, session_ids: list[int]) -> None:
    """Remove sessions and everything hanging off them, children first."""
    if not session_ids:
        return
    participant_ids = list(
        (
            await db.scalars(
                select(Participant.id).where(Participant.session_id.in_(session_ids))
            )
        ).all()
    )

    await db.execute(delete(ActivityEvent).where(ActivityEvent.session_id.in_(session_ids)))
    if participant_ids:
        await db.execute(
            delete(ActivityEvent).where(ActivityEvent.participant_id.in_(participant_ids))
        )
    await db.execute(delete(SessionFile).where(SessionFile.session_id.in_(session_ids)))
    await db.execute(delete(Participant).where(Participant.session_id.in_(session_ids)))
    await db.execute(delete(CollabSession).where(CollabSession.id.in_(session_ids)))


async def close_stale(db: AsyncSession, minutes: int, apply: bool) -> int:
    """End sessions nobody has touched for a while.

    A session only stops being live when somebody ends it, so a host who
    closes their laptop leaves one sitting on the dashboard for good.
    """
    cutoff = utcnow() - timedelta(minutes=minutes)
    stale = (
        await db.scalars(
            select(CollabSession).where(
                CollabSession.status != STATUS_ENDED,
                CollabSession.last_activity_at < cutoff,
            )
        )
    ).all()

    for item in stale:
        print(f"  stale: {item.join_code}  {item.title!r}  idle since {item.last_activity_at:%Y-%m-%d %H:%M}")
        if apply:
            item.status = STATUS_ENDED
            item.ended_at = utcnow()
            for person in await db.scalars(
                select(Participant).where(Participant.session_id == item.id)
            ):
                person.connected = False
    return len(stale)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--stale",
        action="store_true",
        help=f"mark sessions idle longer than --idle-minutes as ended "
             f"(default {settings.session_idle_timeout_minutes})",
    )
    parser.add_argument("--idle-minutes", type=int, default=settings.session_idle_timeout_minutes)
    parser.add_argument(
        "--purge-ended",
        type=int,
        metavar="DAYS",
        help="delete sessions that ended more than DAYS ago",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="delete every session, participant, file and event (keeps admins)",
    )
    parser.add_argument("--yes", action="store_true", help="actually make the changes")
    args = parser.parse_args()

    if not any((args.stale, args.purge_ended, args.all)):
        parser.print_help()
        return 1

    apply = args.yes
    print(f"database: {settings.database_url.rsplit('@', 1)[-1]}")
    print("mode:    ", "APPLY" if apply else "dry run (pass --yes to apply)")

    async with SessionLocal() as db:
        before = await counts(db)

        if args.stale:
            print("\nStale sessions:")
            found = await close_stale(db, args.idle_minutes, apply)
            if not found:
                print("  none")

        if args.purge_ended is not None:
            cutoff = utcnow() - timedelta(days=args.purge_ended)
            ids = list(
                (
                    await db.scalars(
                        select(CollabSession.id).where(
                            CollabSession.status == STATUS_ENDED,
                            CollabSession.ended_at.isnot(None),
                            CollabSession.ended_at < cutoff,
                        )
                    )
                ).all()
            )
            print(f"\nEnded sessions older than {args.purge_ended} day(s): {len(ids)}")
            if apply:
                await drop_sessions(db, ids)

        if args.all:
            remaining = list((await db.scalars(select(CollabSession.id))).all())
            print(f"\nSessions to remove: {len(remaining)}")
            if apply:
                await drop_sessions(db, remaining)
                # Administrator logins survive; anything tied to a session
                # went with the session.
                await db.execute(
                    delete(ActivityEvent).where(
                        ActivityEvent.session_id.is_(None),
                        ActivityEvent.user_id.is_(None),
                    )
                )

        if apply:
            await db.commit()
        else:
            await db.rollback()

        after = await counts(db)
        show("APPLIED" if apply else "WOULD CHANGE (nothing written)", before, after if apply else before)

        if not apply:
            print("\nNothing was written. Re-run with --yes to apply.")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
