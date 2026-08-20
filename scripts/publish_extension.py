"""Publish a VS Code extension build so the server can hand it out.

    python scripts/publish_extension.py ../VScode-ex/codecolab-2.1.0.vsix

Copies the file into app/static/downloads/, records its sha256 in a manifest,
and removes older builds. Commit the result: the deployment folder is rebuilt
from git on every start, so anything not committed disappears.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = ROOT / "app" / "static" / "downloads"
MANIFEST = DOWNLOAD_DIR / "latest.json"


def read_version(vsix: Path) -> str:
    """Pull the version out of the packaged extension's own manifest."""
    with zipfile.ZipFile(vsix) as archive:
        name = next(
            (n for n in archive.namelist() if n.endswith("extension/package.json")),
            None,
        )
        if name is None:
            raise SystemExit("That file does not look like a .vsix (no package.json)")
        data = json.loads(archive.read(name).decode("utf-8"))
    version = data.get("version")
    if not version:
        raise SystemExit("The packaged package.json has no version")
    return str(version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vsix", type=Path, help="path to the built .vsix")
    parser.add_argument("--notes", default="", help="short changelog line")
    args = parser.parse_args()

    vsix: Path = args.vsix
    if not vsix.is_file():
        raise SystemExit(f"No such file: {vsix}")

    version = read_version(vsix)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    target = DOWNLOAD_DIR / f"codecolab-{version}.vsix"
    shutil.copyfile(vsix, target)

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "version": version,
        "file": target.name,
        "sha256": digest,
        "notes": args.notes,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # One build is enough; old ones only bloat the repository.
    for stale in DOWNLOAD_DIR.glob("codecolab-*.vsix"):
        if stale != target:
            stale.unlink()
            print("removed", stale.name)

    print(f"published {target.name}")
    print(f"  version {version}")
    print(f"  sha256  {digest}")
    print(f"  size    {target.stat().st_size} bytes")
    print("\nCommit app/static/downloads/ so the deployment picks it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
