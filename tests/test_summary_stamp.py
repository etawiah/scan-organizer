"""Summary logs use a UTC timestamp matching the manifest naming scheme."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from scanorganizer.cli import _write_summary
from scanorganizer.config import utc_run_stamp
from scanorganizer.summary import write_summary


STAMP_RE = re.compile(r"^summary-\d{8}T\d{6}Z\.md$")
MANIFEST_RE = re.compile(r"^manifest-\d{8}T\d{6}Z\.jsonl$")


def test_utc_run_stamp_format():
    stamp = utc_run_stamp(datetime(2026, 10, 16, 15, 4, 5, tzinfo=timezone.utc))
    assert stamp == "20261016T150405Z"
    assert MANIFEST_RE.match(f"manifest-{stamp}.jsonl")
    assert STAMP_RE.match(f"summary-{stamp}.md")


def test_two_runs_do_not_overwrite_summaries(tmp_path):
    summary = {
        "counts": {},
        "folder_counts": {},
        "review": [],
        "unsorted": [],
        "errors": [],
        "providers": {},
    }
    a = write_summary(summary, tmp_path / "summary-20261016T150405Z.md")
    b = write_summary(summary, tmp_path / "summary-20261016T150406Z.md")
    assert a != b
    assert a.exists() and b.exists()
    assert STAMP_RE.match(a.name)
    assert STAMP_RE.match(b.name)


def test_write_summary_cli_uses_run_stamp(tmp_path):
    class _Args:
        no_summary = False
        log = tmp_path
        run_stamp = "20261016T031700Z"

    (tmp_path / "manifest-20261016T031700Z.jsonl").write_text("", encoding="utf-8")
    out = _write_summary(_Args(), outcomes=[], stamp="20261016T031700Z")
    assert out is not None
    assert out.name == "summary-20261016T031700Z.md"
    assert out.exists()
