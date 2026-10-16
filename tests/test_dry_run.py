"""Dry-run behavior: manifest is written, nothing moves."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scanorganizer import ocr as ocr_mod
from scanorganizer.manifest import read_entries
from scanorganizer.organizer import process_batch


SAMPLE_TEXTS = {
    "irs_2025.pdf": "IRS 1040 tax return for tax year 2025 filed by taxpayer.",
    "mystery.pdf": "",  # empty OCR -> _Unsorted/
    "report_card.pdf": "Placeholder Elementary Report Card — grade 4 — immunization record attached.",
}


@pytest.fixture()
def seeded_src(tmp_scan_tree, monkeypatch):
    for name in SAMPLE_TEXTS:
        (tmp_scan_tree["src"] / name).write_bytes(b"%PDF-1.4\n" + name.encode() + b"\n")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return SAMPLE_TEXTS.get(path.name, "")
    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    return tmp_scan_tree


def test_dry_run_leaves_sources_in_place_and_writes_manifest(
    seeded_src, runconfig_factory,
):
    cfg = runconfig_factory(provider="local-keyword", dry_run=True)
    result = process_batch(cfg)

    for outcome in result.outcomes:
        assert outcome.src.exists(), f"dry-run must not move {outcome.src}"
        if outcome.dest and outcome.action == "plan":
            assert not outcome.dest.exists(), f"planned dest {outcome.dest} should not exist"

    assert cfg.manifest_path.exists()
    entries = list(read_entries(cfg.manifest_path))
    assert entries
    assert all(e.dry_run for e in entries), "every entry in a dry-run must flag dry_run=True"


def test_empty_ocr_goes_to_unsorted(seeded_src, runconfig_factory):
    cfg = runconfig_factory(provider="local-keyword")
    result = process_batch(cfg)
    unsorted = [o for o in result.outcomes if o.src.name == "mystery.pdf"]
    assert unsorted and unsorted[0].action == "unsorted"
    assert cfg.unsorted_dir in unsorted[0].dest.parents


def test_sensitive_doc_classified_locally_and_routed(
    seeded_src, runconfig_factory,
):
    cfg = runconfig_factory(
        provider="local-keyword",
        extra_terms=["Placeholder Elementary"],
    )
    result = process_batch(cfg)
    sensitive = next(o for o in result.outcomes if o.src.name == "report_card.pdf")
    assert sensitive.action in {"move", "review"}
    if sensitive.action == "move":
        parts = sensitive.dest.relative_to(cfg.dest).parts
        assert parts[0] == "Family" and parts[1] == "Kids"


def test_docx_routes_to_review_without_touching_api(tmp_scan_tree, runconfig_factory, monkeypatch):
    docx = tmp_scan_tree["src"] / "proposal.docx"
    docx.write_bytes(b"PK\x03\x04 not really a docx")
    def fail(*a, **kw):
        raise AssertionError("OCR must not be invoked for review-only extensions")
    monkeypatch.setattr(ocr_mod, "extract_text", fail)
    cfg = runconfig_factory(provider="anthropic")
    result = process_batch(cfg)
    assert len(result.outcomes) == 1
    assert result.outcomes[0].action == "review"
    assert cfg.review_dir in result.outcomes[0].dest.parents


def test_ignored_filenames_are_not_processed(tmp_scan_tree, runconfig_factory, monkeypatch):
    (tmp_scan_tree["src"] / ".DS_Store").write_bytes(b"junk")
    (tmp_scan_tree["src"] / "Thumbs.db").write_bytes(b"junk")
    (tmp_scan_tree["src"] / "#recycle").mkdir()
    (tmp_scan_tree["src"] / "@eaDir").mkdir()

    def fail_ocr(*a, **kw):
        raise AssertionError("OCR must not be invoked for ignored files")
    monkeypatch.setattr(ocr_mod, "extract_text", fail_ocr)

    cfg = runconfig_factory(provider="local-keyword")
    result = process_batch(cfg)
    assert result.outcomes == []
