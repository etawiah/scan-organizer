"""Run output files that hold filenames are owner-only; dest folders stay normal."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.manifest import ManifestEntry, ManifestWriter
from scanorganizer.organizer import process_batch
from scanorganizer.secureio import OWNER_ONLY_FILE_MODE, chmod_owner_only
from scanorganizer.summary import write_summary
from tests.test_outbound_privacy import _write


CLEAN_BILL = "City Electric Co monthly electric bill. kWh used 541. Amount due $40.00."


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_manifest_and_summary_are_owner_only(tmp_path):
    manifest = tmp_path / "manifest-test.jsonl"
    with ManifestWriter(manifest) as writer:
        writer.write(ManifestEntry.new(
            action="plan",
            src=tmp_path / "src.pdf",
            dest=tmp_path / "dest.pdf",
            sha256="abc",
            dry_run=True,
        ))
    assert manifest.exists()
    assert _mode(manifest) == OWNER_ONLY_FILE_MODE

    summary_path = tmp_path / "summary-test.md"
    write_summary(
        {
            "counts": {},
            "folder_counts": {},
            "review": [],
            "unsorted": [],
            "errors": [],
            "providers": {},
        },
        summary_path,
    )
    assert summary_path.exists()
    assert _mode(summary_path) == OWNER_ONLY_FILE_MODE


def test_chmod_owner_only_tightens_existing_world_readable_file(tmp_path):
    path = tmp_path / "already-644.txt"
    path.write_text("secret", encoding="utf-8")
    path.chmod(0o644)
    assert _mode(path) == 0o644
    chmod_owner_only(path)
    assert _mode(path) == 0o600


@pytest.mark.parametrize("umask_value,expected_dir_mode", [(0o022, 0o755), (0o002, 0o775)])
def test_dest_folders_keep_normal_permissions_outputs_are_600(
    tmp_scan_tree, runconfig_factory, monkeypatch, umask_value, expected_dir_mode,
):
    """Taxonomy / review dirs follow the process umask; outputs stay 0600."""
    _write(tmp_scan_tree["src"] / "bill.pdf", CLEAN_BILL)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return CLEAN_BILL

    def fake_classify(*_a, **_k):
        return {
            "category": "Household/Home/Utilities",
            "confidence": 0.99,
            "reason": "electric bill",
            "short_name": "electric_bill",
            "doc_date": "2026-09-01",
            "provider": "ollama",
            "is_local": True,
        }

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setattr(classifier_mod, "classify", fake_classify)
    cfg = runconfig_factory(provider="ollama")
    shutil.rmtree(cfg.review_dir)
    shutil.rmtree(cfg.unsorted_dir)

    previous = os.umask(umask_value)
    try:
        result = process_batch(cfg)
        outcome = result.outcomes[0]
        assert outcome.action == "move"
        assert outcome.dest is not None
        dest_folder = outcome.dest.parent
        assert dest_folder.relative_to(cfg.dest).parts == ("Household", "Home", "Utilities")
        assert dest_folder.is_dir()
        assert _mode(dest_folder) == expected_dir_mode
        assert _mode(dest_folder.parent) == expected_dir_mode
        assert _mode(cfg.review_dir) == expected_dir_mode
        assert _mode(cfg.unsorted_dir) == expected_dir_mode
        assert _mode(dest_folder) != 0o700
        assert _mode(cfg.manifest_path) == OWNER_ONLY_FILE_MODE

        summary_path = write_summary(
            {
                "counts": {},
                "folder_counts": {},
                "review": [],
                "unsorted": [],
                "errors": [],
                "providers": {},
            },
            tmp_scan_tree["log"] / "summary-perms.md",
        )
        assert _mode(summary_path) == OWNER_ONLY_FILE_MODE
    finally:
        os.umask(previous)
