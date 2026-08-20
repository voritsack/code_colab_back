"""In-memory registry of live WebSocket connections.

One process, one hub. That is a deliberate constraint: a collaboration
session is a small, short-lived thing, and keeping the fan-out in memory
avoids dragging Redis into the deployment. Run the server with a single
worker; if you outgrow that, replace this module with a pub/sub backend and
nothing else has to change.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

from .models import ROLE_HOST, utcnow


@dataclass
class Connection:
    websocket: WebSocket
    participant_id: int
    session_public_id: str
    display_name: str
    role: str
    approved: bool = False
    active_file: str | None = None
    edits: int = 0
    connected_at: datetime = field(default_factory=utcnow)
    last_seen_at: datetime = field(default_factory=utcnow)

    @property
    def is_host(self) -> bool:
        return self.role == ROLE_HOST

    def describe(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "display_name": self.display_name,
            "role": self.role,
            "approved": self.approved,
            "active_file": self.active_file,
            "edits": self.edits,
        }


class Hub:
    def __init__(self) -> None:
        self._rooms: dict[str, dict[int, Connection]] = {}
        self._status: dict[str, str] = {}
        # room -> path -> (participant_id, expires at monotonic seconds)
        self._file_locks: dict[str, dict[str, tuple[int, float]]] = {}
        # room -> the task waiting to end it because nobody is left
        self._empty_timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # -- room status cache -----------------------------------------------
    #
    # Kept in memory so the hot path (a keystroke arriving over the socket)
    # can check "is this session paused?" without a database round trip.

    def set_room_status(self, session_public_id: str, status: str) -> None:
        self._status[session_public_id] = status

    def room_status(self, session_public_id: str, default: str = "active") -> str:
        return self._status.get(session_public_id, default)

    def forget_room_status(self, session_public_id: str) -> None:
        self._status.pop(session_public_id, None)

    # -- ending a session nobody is in ------------------------------------

    def arm_empty_timer(self, session_public_id: str, task: asyncio.Task) -> None:
        self.cancel_empty_timer(session_public_id)
        self._empty_timers[session_public_id] = task

    def cancel_empty_timer(self, session_public_id: str) -> None:
        """Called whenever somebody connects: the room is not empty after all."""
        task = self._empty_timers.pop(session_public_id, None)
        if task is not None and not task.done():
            task.cancel()

    def forget_empty_timer(self, session_public_id: str) -> None:
        self._empty_timers.pop(session_public_id, None)

    # -- soft file locks -------------------------------------------------
    #
    # Sync is whole-file, so two people typing in one file would overwrite
    # each other silently. Whoever types first holds the file for a few
    # seconds; everyone else is told who has it instead of losing their work.

    def claim_file(
        self, session_public_id: str, path: str, participant_id: int, ttl: float
    ) -> int | None:
        """Take or renew the lock. Returns the holder if it is someone else."""
        now = time.monotonic()
        room = self._file_locks.setdefault(session_public_id, {})

        holder = room.get(path)
        if holder is not None and holder[0] != participant_id and holder[1] > now:
            return holder[0]

        room[path] = (participant_id, now + ttl)
        return None

    def release_other_files(
        self, session_public_id: str, participant_id: int, keep: str | None
    ) -> list[str]:
        """Free every file this person holds except the one they are in now.

        Without this a lock outlives the reason for it: you type one line,
        move to another file, and nobody can touch the first one until the
        lease runs out.
        """
        room = self._file_locks.get(session_public_id)
        if not room:
            return []
        freed = [
            path
            for path, (owner, _) in room.items()
            if owner == participant_id and path != keep
        ]
        for path in freed:
            del room[path]
        return freed

    def release_files(self, session_public_id: str, participant_id: int) -> list[str]:
        """Drop every lock held by someone, e.g. when they disconnect."""
        room = self._file_locks.get(session_public_id)
        if not room:
            return []
        freed = [path for path, (owner, _) in room.items() if owner == participant_id]
        for path in freed:
            del room[path]
        return freed

    def file_locks(self, session_public_id: str) -> dict[str, int]:
        """Currently held locks, expired ones pruned."""
        now = time.monotonic()
        room = self._file_locks.get(session_public_id, {})
        for path in [p for p, (_, expires) in room.items() if expires <= now]:
            del room[path]
        return {path: owner for path, (owner, _) in room.items()}

    def forget_locks(self, session_public_id: str) -> None:
        self._file_locks.pop(session_public_id, None)

    # -- registry --------------------------------------------------------

    async def register(self, conn: Connection) -> None:
        """Add a connection, replacing any previous one for the same person."""
        async with self._lock:
            room = self._rooms.setdefault(conn.session_public_id, {})
            previous = room.get(conn.participant_id)
            room[conn.participant_id] = conn

        if previous is not None and previous is not conn:
            await self._close(previous, code=4004, reason="Replaced by a newer connection")

    async def unregister(self, conn: Connection) -> None:
        async with self._lock:
            room = self._rooms.get(conn.session_public_id)
            if not room:
                return
            # Only drop it if this exact connection is still the registered one.
            if room.get(conn.participant_id) is conn:
                del room[conn.participant_id]
            if not room:
                self._rooms.pop(conn.session_public_id, None)

    def connections(self, session_public_id: str) -> list[Connection]:
        return list(self._rooms.get(session_public_id, {}).values())

    def get(self, session_public_id: str, participant_id: int) -> Connection | None:
        return self._rooms.get(session_public_id, {}).get(participant_id)

    def hosts(self, session_public_id: str) -> list[Connection]:
        return [c for c in self.connections(session_public_id) if c.is_host]

    def room_count(self) -> int:
        return len(self._rooms)

    def connection_count(self) -> int:
        return sum(len(room) for room in self._rooms.values())

    def live_session_ids(self) -> set[str]:
        return set(self._rooms)

    # -- delivery --------------------------------------------------------

    async def send(self, conn: Connection, payload: dict[str, Any]) -> bool:
        if conn.websocket.client_state is not WebSocketState.CONNECTED:
            return False
        try:
            await conn.websocket.send_json(payload)
            return True
        except Exception:
            # A dead socket is not an error worth propagating; drop it.
            await self.unregister(conn)
            with contextlib.suppress(Exception):
                await conn.websocket.close()
            return False

    async def broadcast(
        self,
        session_public_id: str,
        payload: dict[str, Any],
        *,
        exclude_participant: int | None = None,
        approved_only: bool = True,
    ) -> None:
        targets = [
            conn
            for conn in self.connections(session_public_id)
            if conn.participant_id != exclude_participant
            and (conn.approved or not approved_only)
        ]
        if not targets:
            return
        await asyncio.gather(
            *(self.send(conn, payload) for conn in targets),
            return_exceptions=True,
        )

    async def send_to_participant(
        self, session_public_id: str, participant_id: int, payload: dict[str, Any]
    ) -> bool:
        conn = self.get(session_public_id, participant_id)
        if conn is None:
            return False
        return await self.send(conn, payload)

    async def notify_hosts(
        self, session_public_id: str, payload: dict[str, Any]
    ) -> None:
        hosts = self.hosts(session_public_id)
        if not hosts:
            return
        await asyncio.gather(
            *(self.send(conn, payload) for conn in hosts),
            return_exceptions=True,
        )

    async def disconnect_participant(
        self, session_public_id: str, participant_id: int, *, code: int, reason: str
    ) -> None:
        conn = self.get(session_public_id, participant_id)
        if conn is None:
            return
        await self.unregister(conn)
        await self._close(conn, code=code, reason=reason)

    async def close_room(self, session_public_id: str, *, code: int, reason: str) -> None:
        async with self._lock:
            room = self._rooms.pop(session_public_id, {})
        await asyncio.gather(
            *(self._close(conn, code=code, reason=reason) for conn in room.values()),
            return_exceptions=True,
        )

    async def _close(self, conn: Connection, *, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await conn.websocket.close(code=code, reason=reason[:120])

    # -- analytics -------------------------------------------------------

    def live_overview(self) -> dict[str, Any]:
        return {
            "live_rooms": self.room_count(),
            "live_connections": self.connection_count(),
        }


hub = Hub()
