"""Where attachment bytes live.

On disk, under a directory this module owns entirely. Two rules make that
safe: nothing outside is ever touched, and the name on disk is generated
here rather than taken from the uploader - so a file called
``../../etc/passwd`` is stored as ``a3f1….bin`` like everything else.

Nothing here is meant to survive its session. A hosting panel that rebuilds
the container on restart will wipe this directory, which is the correct
outcome for it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import shutil
from pathlib import Path

from .config import PROJECT_ROOT, settings

logger = logging.getLogger("codecolab")

# Anything else in a filename is replaced. Deliberately narrow: this is what
# people see when they save the file, so it has to be harmless on every OS.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]+")
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def root() -> Path:
    """The attachment directory, anchored to the project rather than the cwd.

    A relative ATTACHMENT_DIR resolved against the working directory would
    point somewhere different depending on where the server was launched
    from, silently orphaning everything already stored.
    """
    configured = Path(settings.attachment_dir)
    path = (
        configured if configured.is_absolute() else PROJECT_ROOT / configured
    ).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def display_name(raw: str) -> str:
    """A filename safe to show, and safe for the receiver to save under."""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = _UNSAFE.sub("_", name)
    name = name.strip(". ") or "file"
    if name.split(".", 1)[0].lower() in _RESERVED:
        name = "_" + name
    return name[:255]


def new_stored_name() -> str:
    """An opaque on-disk name, unrelated to whatever the uploader called it."""
    return secrets.token_hex(16) + ".bin"


def path_for(stored_name: str) -> Path | None:
    """Resolve a stored name, refusing anything that escapes the directory."""
    if not stored_name or "/" in stored_name or "\\" in stored_name:
        return None
    base = root()
    candidate = (base / stored_name).resolve()
    if candidate.parent != base:
        return None
    return candidate


def write(stored_name: str, chunks) -> tuple[int, str]:
    """Write an attachment, returning its size and digest."""
    target = path_for(stored_name)
    if target is None:
        raise ValueError("Refusing to write outside the attachment directory")

    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as handle:
        for chunk in chunks:
            size += len(chunk)
            digest.update(chunk)
            handle.write(chunk)
    return size, digest.hexdigest()


def remove(stored_name: str) -> bool:
    target = path_for(stored_name)
    if target is None or not target.exists():
        return False
    try:
        target.unlink()
        return True
    except OSError as exc:
        logger.warning("Could not delete attachment %s: %s", stored_name, exc)
        return False


def remove_many(stored_names) -> int:
    return sum(1 for name in stored_names if remove(name))


def usage_bytes() -> int:
    try:
        return sum(f.stat().st_size for f in root().iterdir() if f.is_file())
    except OSError:
        return 0


def sweep_orphans(known: set[str]) -> int:
    """Delete files with no row pointing at them.

    A crash between writing the file and committing the row leaves one
    behind; without this they accumulate silently until the disk fills.
    """
    removed = 0
    try:
        for item in root().iterdir():
            if item.is_file() and item.name not in known:
                item.unlink()
                removed += 1
    except OSError as exc:
        logger.warning("Orphan sweep failed: %s", exc)
    if removed:
        logger.info("Removed %s orphaned attachment file(s)", removed)
    return removed


def wipe() -> None:
    """Remove the whole directory. Used when nothing should be left."""
    try:
        shutil.rmtree(root(), ignore_errors=True)
    except OSError:
        pass
