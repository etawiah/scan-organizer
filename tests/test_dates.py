"""Date resolution: text > scanner filename > mtime."""
from __future__ import annotations

import os
import time
from datetime import date, datetime
from pathlib import Path

from scanorganizer.dates import (
    date_from_scanner_filename,
    date_from_text,
    resolve_doc_date,
)


def test_text_takes_latest_plausible_date():
    text = "Issued 03/15/2024. Due date 2025-04-30. Statement period Jan 1 2025."
    assert date_from_text(text) == date(2025, 4, 30)


def test_scanner_filename_pattern():
    assert date_from_scanner_filename("scan10162026_20261016000724.pdf") == date(2026, 10, 16)
    assert date_from_scanner_filename("not_a_scan.pdf") is None


def test_mtime_fallback(tmp_path: Path):
    p = tmp_path / "undated.pdf"
    p.write_bytes(b"x")
    stamp = datetime(2024, 5, 20, 12, 0, 0).timestamp()
    os.utime(p, (stamp, stamp))
    assert resolve_doc_date("", p) == date(2024, 5, 20)


def test_scanner_filename_used_before_mtime(tmp_path: Path):
    p = tmp_path / "scan10162026_20261016000724.pdf"
    p.write_bytes(b"x")
    stamp = datetime(2020, 1, 1).timestamp()
    os.utime(p, (stamp, stamp))
    assert resolve_doc_date("", p) == date(2026, 10, 16)


def test_hinted_date_wins_when_plausible(tmp_path: Path):
    p = tmp_path / "scan10162026_20261016000724.pdf"
    p.write_bytes(b"x")
    assert resolve_doc_date("", p, hinted="2024-02-15") == date(2024, 2, 15)
