"""Small shared helpers: path safety and human-friendly join codes."""

from __future__ import annotations

import secrets
import string

from .config import settings

# Meet-style code: three groups of letters, easy to read aloud.
_CODE_ALPHABET = "abcdefghijkmnopqrstuvwxyz"  # no 'l', it reads as '1'
_CODE_SHAPE = (3, 4, 3)

_RESERVED_WINDOWS_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def generate_join_code() -> str:
    groups = [
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(size))
        for size in _CODE_SHAPE
    ]
    return "-".join(groups)


def normalize_join_code(raw: str) -> str:
    """Accept 'ABC-DEFG-HIJ', 'abcdefghij' or a full join URL."""
    value = (raw or "").strip().lower()
    if "/" in value:
        value = value.rstrip("/").rsplit("/", 1)[-1]
    if "?" in value:
        value = value.split("?", 1)[0]

    letters = [ch for ch in value if ch.isalnum()]
    if len(letters) == sum(_CODE_SHAPE):
        parts = []
        index = 0
        for size in _CODE_SHAPE:
            parts.append("".join(letters[index : index + size]))
            index += size
        return "-".join(parts)
    return value


class UnsafePathError(ValueError):
    """Raised when a peer sends a path we refuse to touch."""


def sanitize_relative_path(raw: str) -> str:
    """Return a workspace-relative POSIX path, or raise.

    This is the single choke point protecting every file write. A malicious
    or buggy peer must not be able to escape the workspace via ``..``, an
    absolute path, a drive letter or a UNC prefix.
    """
    if not raw or not isinstance(raw, str):
        raise UnsafePathError("Empty path")

    value = raw.replace("\\", "/").strip()

    if len(value) > settings.max_path_length:
        raise UnsafePathError("Path too long")
    if "\x00" in value:
        raise UnsafePathError("Path contains a null byte")
    if value.startswith("//"):
        raise UnsafePathError("UNC paths are not allowed")
    if value.startswith("/"):
        raise UnsafePathError("Absolute paths are not allowed")
    if len(value) >= 2 and value[1] == ":" and value[0] in string.ascii_letters:
        raise UnsafePathError("Drive-qualified paths are not allowed")

    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePathError("Parent directory traversal is not allowed")
        if part.endswith((" ", ".")):
            raise UnsafePathError("Path segment ends with a space or dot")
        if part.split(".", 1)[0].lower() in _RESERVED_WINDOWS_NAMES:
            raise UnsafePathError("Reserved filename")
        parts.append(part)

    if not parts:
        raise UnsafePathError("Path resolves to nothing")

    return "/".join(parts)


def is_safe_path(raw: str) -> bool:
    try:
        sanitize_relative_path(raw)
    except UnsafePathError:
        return False
    return True
