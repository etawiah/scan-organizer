"""Undo round-trip: process a sample batch, then reverse it from the manifest."""
from __future__ import annotations

from pathlib import Path

import pytest

from scanorganizer import ocr as ocr_mod
from scanorganizer.manifest import undo
from scanorganizer.organizer import process_batch


SAMPLE_TEXTS = {
    "irs_2025.pdf": "IRS Form 1040 — tax return for tax year 2025. Filing status: single.",
    "electric_bill.pdf": "City Electric Co — monthly electric bill. kWh used 541. Amount due $112.44.",
    "dmv_renewal.pdf": "State DMV — vehicle registration renewal. VIN 1HGCM82633A004352.",
}


@pytest.fixture()
def seeded_src(tmp_scan_tree, monkeypatch):
    for name in SAMPLE_TEXTS:
        (tmp_scan_tree["src"] / name).write_bytes(b"%PDF-1.4\n" + name.encode() + b"\n")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return SAMPLE_TEXTS.get(path.name, "")
    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    return tmp_scan_tree


def _actions(outcomes):
    return [o.action for o in outcomes]


def test_round_trip_batch_then_undo(seeded_src, runconfig_factory):
    cfg = runconfig_factory(provider="local-keyword", threshold=0.5)
    result = process_batch(cfg)

    assert len(result.outcomes) == len(SAMPLE_TEXTS)
    assert set(_actions(result.outcomes)) == {"move"}

    # Every classified file now lives under <dest>/<owner>/[<area>/]<doc_type>/
    for outcome in result.outcomes:
        assert outcome.dest is not None and outcome.dest.exists()
        assert seeded_src["dest"] in outcome.dest.parents
        assert not outcome.src.exists(), f"source {outcome.src} should be gone"

    # Undo reads the manifest and puts files back exactly where they were.
    undo_result = undo(cfg.manifest_path)
    assert undo_result.reversed_moves == len(SAMPLE_TEXTS)
    assert undo_result.errors == []

    for outcome in result.outcomes:
        assert outcome.src.exists(), f"undo must restore {outcome.src}"
        assert not outcome.dest.exists(), f"dest {outcome.dest} should be empty after undo"


def test_dry_run_undo_does_not_move_files(seeded_src, runconfig_factory):
    cfg = runconfig_factory(provider="local-keyword", threshold=0.5)
    result = process_batch(cfg)
    for o in result.outcomes:
        assert o.dest.exists()

    before = {o.dest for o in result.outcomes}
    dry = undo(cfg.manifest_path, dry_run=True)
    assert dry.reversed_moves == len(SAMPLE_TEXTS)
    for p in before:
        assert p.exists(), "dry-run undo must not move anything"


def test_duplicates_are_skipped_on_second_pass(seeded_src, runconfig_factory):
    cfg = runconfig_factory(provider="local-keyword", threshold=0.5)
    process_batch(cfg)

    for name, body in SAMPLE_TEXTS.items():
        (seeded_src["src"] / name).write_bytes(b"%PDF-1.4\n" + name.encode() + b"\n")

    cfg2 = runconfig_factory(provider="local-keyword", threshold=0.5)
    cfg2.manifest_path = seeded_src["log"] / "manifest-test-2.jsonl"
    second = process_batch(cfg2)
    actions = _actions(second.outcomes)
    assert actions and all(a == "skip-duplicate" for a in actions), actions
