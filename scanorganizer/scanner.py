"""File discovery / filtering for scan-organizer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

from .config import (
    IGNORE_DIR_NAMES,
    IGNORE_FILENAMES,
    REVIEW_ONLY_EXTS,
    SUPPORTED_IMAGE_EXTS,
    SUPPORTED_PDF_EXT,
)


def _is_ignored_path(p: Path) -> bool:
    if p.name in IGNORE_FILENAMES:
        return True
    if p.name.startswith("."):
        return True
    for part in p.parts:
        if part in IGNORE_DIR_NAMES:
            return True
    return False


def is_supported_payload(p: Path) -> bool:
    ext = p.suffix.lower()
    return ext == SUPPORTED_PDF_EXT or ext in SUPPORTED_IMAGE_EXTS


def is_review_only(p: Path) -> bool:
    return p.suffix.lower() in REVIEW_ONLY_EXTS


def is_processable(p: Path) -> bool:
    return is_supported_payload(p) or is_review_only(p)


@dataclass
class CandidateFile:
    path: Path
    review_only: bool
    reason: str = ""


def walk_inbox(
    root: Path,
    recursive: bool = False,
    skip_dirs: Optional[Set[Path]] = None,
) -> Iterator[CandidateFile]:
    """Yield candidate files from an inbox folder.

    When ``recursive`` is False only the direct children of ``root`` are
    considered (matches the original single-folder-watch behavior). When
    ``recursive`` is True we descend but still prune ignored directory names.
    """
    skip_dirs = skip_dirs or set()
    if not root.exists():
        return
    if recursive:
        iterator: Iterable[Path] = _recursive_iter(root)
    else:
        iterator = sorted(root.iterdir())
    for p in iterator:
        if not p.is_file():
            continue
        if _is_ignored_path(p):
            continue
        if p.parent in skip_dirs:
            continue
        if is_review_only(p):
            yield CandidateFile(path=p, review_only=True, reason="unsupported-ext-send-to-review")
        elif is_supported_payload(p):
            yield CandidateFile(path=p, review_only=False)


def _recursive_iter(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in IGNORE_DIR_NAMES or entry.name.startswith("."):
                    continue
                stack.append(entry)
            else:
                yield entry
