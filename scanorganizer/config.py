"""Static defaults, config-path resolution, and small shared constants."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Iterable, Optional, Tuple

DEFAULT_SCANS_ROOT = Path("/scans")

DEFAULT_SRC_NAME = "Unsorted Docs"
DEFAULT_REVIEW_NAME = "_Review"
DEFAULT_UNSORTED_NAME = "_Unsorted"
DEFAULT_LOG_DIRNAME = "_logs"

DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_OCR_PAGES = 5

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif"}
SUPPORTED_PDF_EXT = ".pdf"
REVIEW_ONLY_EXTS = {".docx"}

IGNORE_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORE_DIR_NAMES = {"#recycle", "@eaDir", ".DS_Store", "@tmp"}

REFILE_LEGACY_DIRNAMES = {
    "receipts",
    "invoices",
    "bills",
    "letters",
    "documents",
    "mail",
    "pictures",
    "other",
    "Labeled-Unsorted",
    "Tax Docs",
    "MAB Docs",
    "Family Docs",
}

PROVIDERS = ("anthropic", "gemini", "local-keyword")

# Config files resolved in this order: $SCAN_CONFIG_DIR, /config,
# /app/config, ./config. The host bind-mount in docker-compose is /config.
VOCABULARY_FILENAME = "outbound_vocabulary.txt"
TAXONOMY_FILENAME = "taxonomy.yaml"
PRIVATE_TERMS_FILENAME = "private_terms.txt"

_log = logging.getLogger(__name__)


def package_root() -> Path:
    """Repository / image root that contains ``scanorganizer/``."""
    return Path(__file__).resolve().parent.parent


def _builtin_config_dirs() -> Tuple[Path, ...]:
    return (Path("/config"), Path("/app/config"), Path("config"))


def config_search_dirs() -> Tuple[Path, ...]:
    """Directories searched for taxonomy, vocabulary, and private terms."""
    dirs: list[Path] = []
    env = os.environ.get("SCAN_CONFIG_DIR")
    if env and env.strip():
        dirs.append(Path(env.strip()))
    dirs.extend(_builtin_config_dirs())
    return _unique_paths(dirs)


def _unique_paths(paths: Iterable[Path]) -> Tuple[Path, ...]:
    seen: set[str] = set()
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        try:
            key = str(path.expanduser().resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return tuple(out)


def _legacy_taxonomy_files() -> Tuple[Path, ...]:
    return (Path("taxonomy.yaml"), package_root() / "taxonomy.yaml")


def _legacy_private_terms_files() -> Tuple[Path, ...]:
    return (Path("private_terms.txt"), package_root() / "private_terms.txt")


@lru_cache(maxsize=32)
def resolve_config_file(
    filename: str,
    *,
    required: bool = False,
    extra_files: Tuple[Path, ...] = (),
) -> Optional[Path]:
    """Return the first existing ``filename`` on the config search path.

    Logs the chosen path (never file contents). ``required`` raises
    ``RuntimeError`` when nothing is found — used for the outbound
    vocabulary so a missing file fails closed.
    """
    candidates: list[Path] = [directory / filename for directory in config_search_dirs()]
    candidates.extend(extra_files)
    for path in _unique_paths(candidates):
        try:
            if path.is_file():
                _log.info("using %s: %s", filename, path)
                return path
        except OSError:
            continue
    if required:
        raise RuntimeError(f"{filename} is missing or empty")
    _log.info("no %s found on the config search path", filename)
    return None


def resolve_vocabulary_path() -> Path:
    """Fail-closed: the outbound vocabulary must exist."""
    found = resolve_config_file(VOCABULARY_FILENAME, required=True)
    assert found is not None
    return found


def resolve_taxonomy_path() -> Path:
    found = resolve_config_file(
        TAXONOMY_FILENAME,
        extra_files=_legacy_taxonomy_files(),
    )
    if found is None:
        raise RuntimeError(f"{TAXONOMY_FILENAME} is missing or empty")
    return found


def resolve_private_terms_path() -> Optional[Path]:
    return resolve_config_file(
        PRIVATE_TERMS_FILENAME,
        extra_files=_legacy_private_terms_files(),
    )


def clear_config_resolution_cache() -> None:
    """Drop cached path lookups. Tests call this after env / dir changes."""
    resolve_config_file.cache_clear()

# Documents whose local pre-pass (or classification) lands in these
# doc_types stay on-host by default. Override with LOCAL_ONLY_CATEGORIES
# (comma-separated). An empty value disables the category block-list.
DEFAULT_LOCAL_ONLY_CATEGORIES = ("Finance-Bank", "ID-Legal", "Medical")


def utc_run_stamp(when: datetime | None = None) -> str:
    """UTC stamp matching manifest filenames: ``YYYYMMDDTHHMMSSZ``."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ")


def local_only_categories(raw: Optional[str] = None) -> FrozenSet[str]:
    """Return the doc_types that must not leave the host.

    ``None`` (unset env) → the default Finance-Bank / ID-Legal / Medical
    set. A set-but-empty string disables the policy so tests can send a
    bank statement outbound on purpose.
    """
    if raw is None:
        raw = os.environ.get("LOCAL_ONLY_CATEGORIES")
    if raw is None:
        return frozenset(DEFAULT_LOCAL_ONLY_CATEGORIES)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_local_only_category(doc_type: str, raw: Optional[str] = None) -> bool:
    return bool(doc_type) and doc_type in local_only_categories(raw)
