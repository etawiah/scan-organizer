"""Filename sanitization and path construction."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .taxonomy import TaxonomyNode

_SAFE_RE = re.compile(r"[^A-Za-z0-9._\-]+")


def sanitize_short_name(name: str, default: str = "document") -> str:
    if not name:
        return default
    name = name.strip().lower()
    name = _SAFE_RE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("._-")
    if not name:
        return default
    if len(name) > 80:
        name = name[:80].rstrip("._-")
    return name or default


def build_filename(short_name: str, doc_date: date, ext: str) -> str:
    short = sanitize_short_name(short_name)
    ext = ext.lower()
    if not ext.startswith("."):
        ext = "." + ext
    return f"{short}_{doc_date.isoformat()}{ext}"


def build_dest_path(dest_root: Path, node: TaxonomyNode, filename: str) -> Path:
    return dest_root / node.as_relpath() / filename


def disambiguate(path: Path) -> Path:
    """Return a non-colliding sibling by appending ``_1``, ``_2`` … to the stem."""
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    parent = path.parent
    counter = 1
    while True:
        cand = parent / f"{stem}_{counter}{ext}"
        if not cand.exists():
            return cand
        counter += 1
