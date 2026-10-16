"""Static defaults and small shared constants."""
from __future__ import annotations

from pathlib import Path

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
