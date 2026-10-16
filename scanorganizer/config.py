"""Static defaults and small shared constants."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import FrozenSet, Optional

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
