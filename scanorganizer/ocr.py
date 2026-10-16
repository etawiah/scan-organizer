"""OCR helpers.

Imports of the heavy OCR libs are deferred so the rest of the package (and the
test suite) can run without ``tesseract`` or ``poppler`` installed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class OCRUnavailable(RuntimeError):
    """Raised when the OCR toolchain cannot be loaded."""


def extract_text(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
    """Return OCR text for an image or PDF, or ``""`` on failure.

    Never raises. The caller treats empty text as a signal to route the file
    to ``_Unsorted/``.
    """
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _ocr_pdf(path, max_pages=max_pages, dpi=dpi)
        return _ocr_image(path)
    except OCRUnavailable:
        raise
    except Exception as exc:
        log.warning("OCR failed for %s: %s", path, exc)
        return ""


def _ocr_image(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise OCRUnavailable(str(exc)) from exc
    with Image.open(path) as img:
        return pytesseract.image_to_string(img).strip()


def _ocr_pdf(path: Path, max_pages: int, dpi: int) -> str:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise OCRUnavailable(str(exc)) from exc
    images = convert_from_path(path, dpi=dpi, first_page=1, last_page=max_pages)
    parts = []
    for img in images[:max_pages]:
        try:
            parts.append(pytesseract.image_to_string(img))
        except Exception as exc:
            log.warning("OCR page failed for %s: %s", path, exc)
    return "\n\n".join(p for p in parts if p).strip()


def check_toolchain() -> Optional[str]:
    """Return ``None`` if OCR is usable, else a human-readable reason."""
    try:
        import pytesseract
    except ImportError as exc:
        return f"pytesseract missing: {exc}"
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        return f"tesseract CLI missing: {exc}"
    try:
        import subprocess
        subprocess.run(["pdftoppm", "-v"], check=False, capture_output=True, timeout=5)
    except FileNotFoundError:
        return "poppler (pdftoppm) missing from PATH"
    except Exception as exc:
        return f"poppler check failed: {exc}"
    return None
