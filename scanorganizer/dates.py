"""Extract a best-guess document date from OCR text or a scanner filename.

Order of preference:
  1. A date mined from the document text itself (prefer the latest plausible one).
  2. The ``MMDDYYYY`` embedded in a Brother-style scanner filename
     (e.g. ``scan10162026_20261016000724.pdf``).
  3. ``file.stat().st_mtime`` as the ultimate fallback.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

_YMD_RE = re.compile(r"\b(?P<y>19|20\d{2}|19\d{2}|20\d{2})-(?P<m>0?[1-9]|1[0-2])-(?P<d>0?[1-9]|[12]\d|3[01])\b")

_MDY_SLASH_RE = re.compile(
    r"(?<!\d)(?P<m>0?[1-9]|1[0-2])[/\-](?P<d>0?[1-9]|[12]\d|3[01])[/\-](?P<y>\d{2}|\d{4})(?!\d)"
)

_MONTH_NAME_RE = re.compile(
    r"\b(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(?P<d>0?[1-9]|[12]\d|3[01])(?:,|\s)+(?P<y>\d{4})\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_SCAN_FILENAME_RE = re.compile(
    r"(?i)scan"
    r"(?P<m>0[1-9]|1[0-2])"
    r"(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?P<y>\d{4})"
    r"_?"
)

_MIN_YEAR = 1990
_MAX_FUTURE_DAYS = 180


def _plausible(d: date, today: Optional[date] = None) -> bool:
    today = today or date.today()
    if d.year < _MIN_YEAR:
        return False
    if (d - today).days > _MAX_FUTURE_DAYS:
        return False
    return True


def _normalize_year(y: str) -> int:
    y_int = int(y)
    if y_int < 100:
        return 2000 + y_int if y_int < 70 else 1900 + y_int
    return y_int


def _find_dates(text: str) -> Iterable[date]:
    if not text:
        return []
    found: list[date] = []

    for match in _YMD_RE.finditer(text):
        try:
            found.append(date(_normalize_year(match["y"]), int(match["m"]), int(match["d"])))
        except ValueError:
            continue

    for match in _MDY_SLASH_RE.finditer(text):
        try:
            found.append(date(_normalize_year(match["y"]), int(match["m"]), int(match["d"])))
        except ValueError:
            continue

    for match in _MONTH_NAME_RE.finditer(text):
        mon = _MONTHS.get(match["mon"][:3].lower())
        if not mon:
            continue
        try:
            found.append(date(_normalize_year(match["y"]), mon, int(match["d"])))
        except ValueError:
            continue

    return found


def date_from_text(text: str, today: Optional[date] = None) -> Optional[date]:
    plausible = [d for d in _find_dates(text) if _plausible(d, today)]
    if not plausible:
        return None
    return max(plausible)


def date_from_scanner_filename(name: str) -> Optional[date]:
    match = _SCAN_FILENAME_RE.search(name)
    if not match:
        return None
    try:
        return date(int(match["y"]), int(match["m"]), int(match["d"]))
    except ValueError:
        return None


def date_from_mtime(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def resolve_doc_date(
    text: str,
    file_path: Path,
    hinted: Optional[str] = None,
    today: Optional[date] = None,
) -> date:
    """Resolve the final document date using the preference order."""
    if hinted:
        try:
            d = datetime.strptime(hinted, "%Y-%m-%d").date()
            if _plausible(d, today):
                return d
        except ValueError:
            pass
    d = date_from_text(text, today=today)
    if d:
        return d
    d = date_from_scanner_filename(file_path.name)
    if d and _plausible(d, today):
        return d
    return date_from_mtime(file_path)
