"""JSONL manifest writer / reader + undo.

Each line is a self-contained JSON object with a schema version. The manifest
is append-only, which makes it safe to tail while a batch is running.
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .hashing import sha256_file

MANIFEST_VERSION = 1


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ManifestEntry:
    ts: str
    action: str  # "move" | "plan" | "skip-duplicate" | "review" | "unsorted" | "error" | "undo"
    src: str
    dest: str
    sha256: str
    classification: Dict[str, object] = field(default_factory=dict)
    provider: str = ""
    dry_run: bool = False
    notes: str = ""
    schema_version: int = MANIFEST_VERSION

    @classmethod
    def new(
        cls,
        action: str,
        src: Path,
        dest: Path,
        sha256: str,
        classification: Optional[Dict[str, object]] = None,
        provider: str = "",
        dry_run: bool = False,
        notes: str = "",
    ) -> "ManifestEntry":
        return cls(
            ts=_utcnow_iso(),
            action=action,
            src=str(src),
            dest=str(dest),
            sha256=sha256,
            classification=classification or {},
            provider=provider,
            dry_run=dry_run,
            notes=notes,
        )


class ManifestWriter:
    """Line-buffered JSONL writer that is safe for the whole batch run."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: Optional[io.TextIOWrapper] = None

    def __enter__(self) -> "ManifestWriter":
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def write(self, entry: ManifestEntry) -> None:
        if not self._fh:
            raise RuntimeError("ManifestWriter not opened")
        line = json.dumps(asdict(entry), ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()


def read_entries(path: Path) -> Iterator[ManifestEntry]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            yield ManifestEntry(
                ts=data.get("ts", ""),
                action=data.get("action", ""),
                src=data.get("src", ""),
                dest=data.get("dest", ""),
                sha256=data.get("sha256", ""),
                classification=data.get("classification", {}) or {},
                provider=data.get("provider", ""),
                dry_run=bool(data.get("dry_run", False)),
                notes=data.get("notes", ""),
                schema_version=int(data.get("schema_version", 1)),
            )


def seen_hashes(manifest_dir: Path) -> Dict[str, Path]:
    """Return ``{sha256: dest_path}`` across every ``*.jsonl`` in *manifest_dir*."""
    seen: Dict[str, Path] = {}
    if not manifest_dir.exists():
        return seen
    for jsonl in sorted(manifest_dir.glob("*.jsonl")):
        for entry in read_entries(jsonl):
            if entry.dry_run or entry.action not in {"move", "review", "unsorted"}:
                continue
            if entry.sha256 and entry.sha256 not in seen:
                seen[entry.sha256] = Path(entry.dest)
    return seen


@dataclass
class UndoResult:
    reversed_moves: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)


def undo(manifest_path: Path, dry_run: bool = False) -> UndoResult:
    """Replay a manifest in reverse to put files back where they came from."""
    result = UndoResult()
    entries = list(read_entries(manifest_path))
    for entry in reversed(entries):
        if entry.dry_run:
            continue
        if entry.action not in {"move", "review", "unsorted"}:
            continue
        dest = Path(entry.dest)
        src = Path(entry.src)
        if not dest.exists():
            result.skipped += 1
            continue
        if src.exists():
            result.skipped += 1
            result.errors.append(f"source already exists: {src}")
            continue
        if entry.sha256:
            try:
                if sha256_file(dest) != entry.sha256:
                    result.skipped += 1
                    result.errors.append(f"hash mismatch, skipping {dest}")
                    continue
            except Exception as exc:
                result.skipped += 1
                result.errors.append(f"hash check failed on {dest}: {exc}")
                continue
        if dry_run:
            result.reversed_moves += 1
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, src)
            result.reversed_moves += 1
        except Exception as exc:
            result.errors.append(f"undo failed for {dest} -> {src}: {exc}")
    return result
