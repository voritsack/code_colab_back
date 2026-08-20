"""Serving the VS Code extension itself.

A sideloaded extension never updates on its own, and telling people to
install a .vsix you emailed them is not a distribution story. The server that
already hosts the sessions hosts the build too, with a manifest the extension
polls so it can update itself.

Publish a new build with ``python scripts/publish_extension.py <file.vsix>``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from ..config import settings
from ..templating import STATIC_DIR

router = APIRouter(tags=["extension"])

DOWNLOAD_DIR = STATIC_DIR / "downloads"
MANIFEST = DOWNLOAD_DIR / "latest.json"


def read_manifest() -> dict | None:
    if not MANIFEST.exists():
        return None
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("version") or not data.get("file"):
        return None
    return data


def manifest_urls() -> dict:
    """The two addresses the extension talks to, absolute so they can be shared."""
    return {
        "manifest": f"{settings.public_base_url}/api/extension/latest",
        "download": f"{settings.public_base_url}/download/extension",
    }


def package_path(manifest: dict) -> Path | None:
    """Resolve the manifest's file, refusing anything outside the directory."""
    name = str(manifest.get("file") or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = (DOWNLOAD_DIR / name).resolve()
    if DOWNLOAD_DIR.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


# --------------------------------------------------------------------------
# Publishing
#
# Installing a VSIX runs the code inside it on every machine that pulls it,
# so a build is only accepted if it really is a VS Code extension package
# with a version we can compare against. The admin dashboard posts here; the
# command line script writes the same two things directly.
# --------------------------------------------------------------------------

VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}([-+][0-9A-Za-z.-]+)?$")
MAX_PACKAGE_BYTES = 80 * 1024 * 1024


class PublishError(Exception):
    """A rejected build, with a message meant for whoever uploaded it."""


def read_package_version(data: bytes) -> str:
    """Pull the version out of a .vsix, refusing anything that is not one."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = next(
                (n for n in archive.namelist() if n.endswith("extension/package.json")),
                None,
            )
            if name is None:
                raise PublishError("That file is not a .vsix (no extension manifest)")
            manifest = json.loads(archive.read(name).decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise PublishError("That file is not a .vsix (not a zip archive)") from exc
    except (ValueError, KeyError) as exc:
        raise PublishError("The packaged manifest could not be read") from exc

    version = str(manifest.get("version") or "").strip()
    if not VERSION_RE.match(version):
        raise PublishError(f"The package has no usable version ({version or 'missing'})")
    return version


def publish_package(data: bytes, notes: str = "") -> dict:
    """Store a build and point the manifest at it. Older builds are removed."""
    if not data:
        raise PublishError("The upload was empty")
    if len(data) > MAX_PACKAGE_BYTES:
        raise PublishError("That build is too large to publish")

    version = read_package_version(data)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    target = DOWNLOAD_DIR / f"codecolab-{version}.vsix"
    target.write_bytes(data)

    payload = {
        "version": version,
        "file": target.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "notes": (notes or "").strip()[:300],
        "published_at": datetime.now(timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "+00:00",
    }
    MANIFEST.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # One build is enough - the extension only ever asks for the current one.
    for stale in DOWNLOAD_DIR.glob("codecolab-*.vsix"):
        if stale != target:
            stale.unlink(missing_ok=True)
    return payload


def unpublish() -> bool:
    """Stop handing out a build. Clients keep whatever they already have."""
    removed = False
    if MANIFEST.exists():
        MANIFEST.unlink(missing_ok=True)
        removed = True
    for stale in DOWNLOAD_DIR.glob("codecolab-*.vsix"):
        stale.unlink(missing_ok=True)
        removed = True
    return removed


@router.get("/api/extension/latest")
async def latest() -> dict:
    """What the extension polls to decide whether it is out of date."""
    manifest = read_manifest()
    if manifest is None:
        return {"available": False}

    target = package_path(manifest)
    if target is None:
        return {"available": False}

    return {
        "available": True,
        "version": manifest["version"],
        "url": manifest_urls()["download"],
        # The extension checks this before installing, so a tampered download
        # cannot be installed even if it arrives from somewhere unexpected.
        "sha256": manifest.get("sha256", ""),
        "size": target.stat().st_size,
        "notes": manifest.get("notes", ""),
        "published_at": manifest.get("published_at", ""),
    }


@router.get("/download/extension")
async def download() -> FileResponse:
    manifest = read_manifest()
    target = package_path(manifest) if manifest else None
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No extension build has been published on this server",
        )
    return FileResponse(
        target,
        media_type="application/octet-stream",
        filename=target.name,
        headers={"Cache-Control": "no-cache"},
    )
