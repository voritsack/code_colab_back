"""Shared Jinja2 environment.

Paths are resolved from the package rather than the working directory, so
``uvicorn app.main:app`` behaves the same whatever directory you launch it
from.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
