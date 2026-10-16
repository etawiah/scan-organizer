"""OCR helpers.

Imports of the heavy OCR libs are deferred so the rest of the package (and the
test suite) can run without ``tesseract`` or ``poppler`` installed.

Mirrored or flipped scans (common when a page goes through the ADF upside
down or is photographed through a glass) produce low Tesseract confidence
or a poor word-hit rate. When that happens we retry the same page
horizontally flipped and rotated 180°, and keep the best result.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)


class OCRUnavailable(RuntimeError):
    """Raised when the OCR toolchain cannot be loaded."""


# Tokens of 3+ letters count as "words" for the hit-rate heuristic.
_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")

# Tesseract reports per-word confidence 0–100. Below this (or a weak
# word-hit rate) we treat the page as possibly mirrored/flipped.
LOW_TESSERACT_CONF = 40.0
LOW_WORD_HIT_RATE = 0.25
MIN_WORD_HITS = 3


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


def word_hit_rate(text: str) -> float:
    """Fraction of tokens that look like real words (3+ letters)."""
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return 0.0
    words = _WORD_RE.findall(text or "")
    return len(words) / len(tokens)


def ocr_quality_low(text: str, confidence: Optional[float] = None) -> bool:
    """True when the first-pass OCR looks mirrored, flipped, or empty."""
    if confidence is not None and confidence < LOW_TESSERACT_CONF:
        return True
    if word_hit_rate(text) < LOW_WORD_HIT_RATE:
        return True
    if len(_WORD_RE.findall(text or "")) < MIN_WORD_HITS:
        return True
    return False


def score_ocr(text: str, confidence: Optional[float] = None) -> float:
    """Higher is better. Combines Tesseract confidence, word-hit rate, length."""
    conf = (confidence or 0.0) / 100.0
    return (conf * 2.0) + word_hit_rate(text) + min(len(text or ""), 200) / 200.0


def _tesseract_confidence(pytesseract, img) -> Optional[float]:
    try:
        output = getattr(getattr(pytesseract, "Output", None), "DICT", None)
        kwargs = {"output_type": output} if output is not None else {}
        data = pytesseract.image_to_data(img, **kwargs)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    confs = []
    for raw in data.get("conf", []) or []:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        confs.append(value)
    if not confs:
        return None
    return sum(confs) / len(confs)


def _ocr_one(pytesseract, img) -> Tuple[str, Optional[float]]:
    text = pytesseract.image_to_string(img) or ""
    conf = _tesseract_confidence(pytesseract, img)
    return text.strip(), conf


def ocr_image_best(img, pytesseract=None) -> str:
    """OCR ``img``, retrying flip + 180° when the first pass looks weak."""
    if pytesseract is None:
        import pytesseract as _pt
        pytesseract = _pt
    from PIL import Image as PILImage

    if img.mode not in {"RGB", "L"}:
        working = img.convert("RGB")
    else:
        working = img

    variants = (
        ("original", working),
        ("flip", working.transpose(PILImage.FLIP_LEFT_RIGHT)),
        ("rot180", working.rotate(180, expand=True)),
    )
    best_text = ""
    best_score = float("-inf")
    best_label = "original"
    first_text = ""
    first_conf: Optional[float] = None
    for index, (label, candidate) in enumerate(variants):
        if index > 0 and not ocr_quality_low(first_text, first_conf):
            break
        text, conf = _ocr_one(pytesseract, candidate)
        if index == 0:
            first_text = text
            first_conf = conf
        score = score_ocr(text, conf)
        if score > best_score:
            best_text, best_score, best_label = text, score, label
    if best_label != "original":
        log.info("OCR kept %s orientation (score=%.3f)", best_label, best_score)
    return best_text


def _ocr_image(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise OCRUnavailable(str(exc)) from exc
    with Image.open(path) as img:
        return ocr_image_best(img, pytesseract=pytesseract)


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
            parts.append(ocr_image_best(img, pytesseract=pytesseract))
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
