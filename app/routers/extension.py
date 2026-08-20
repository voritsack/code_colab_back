"""Serving the VS Code extension itself.

A sideloaded extension never updates on its own, and telling people to
install a .vsix you emailed them is not a distribution story. The server that
already hosts the sessions hosts the build too, with a manifest the extension
polls so it can update itself.

Publish a new build with ``python scripts/publish_extension.py <file.vsix>``.
"""

from __future__ import annotations

import json
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


def package_path(manifest: dict) -> Path | None:
    """Resolve the manifest's file, refusing anything outside the directory."""
    name = str(manifest.get("file") or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = (DOWNLOAD_DIR / name).resolve()
    if DOWNLOAD_DIR.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


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
        "url": f"{settings.public_base_url}/download/extension",
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
