"""Run output files (manifest, summary, logs) are owner-only (mode 0600)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from scanorganizer.cli import main
from scanorganizer.manifest import ManifestEntry, ManifestWriter
from scanorganizer.secureio import (
    OWNER_ONLY_FILE_MODE,
    RESTRICTIVE_UMASK,
    apply_restrictive_umask,
    chmod_owner_only,
)
from scanorganizer.summary import write_summary


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


def test_main_applies_restrictive_umask(tmp_path, monkeypatch):
    previous = os.umask(0o022)
    os.umask(previous)
    try:
        monkeypatch.chdir(tmp_path)
        try:
            main(["--help"])
        except SystemExit as exc:
            assert exc.code in (0, None)
        assert os.umask(previous) == RESTRICTIVE_UMASK
        os.umask(RESTRICTIVE_UMASK)
        probe = tmp_path / "created-after-main.txt"
        probe.write_text("x", encoding="utf-8")
        assert _mode(probe) == 0o600
    finally:
        os.umask(previous)


def test_apply_restrictive_umask_constant():
    assert RESTRICTIVE_UMASK == 0o077
    assert OWNER_ONLY_FILE_MODE == 0o600
    previous = os.umask(0o022)
    try:
        prior = apply_restrictive_umask()
        assert prior == 0o022
        assert os.umask(RESTRICTIVE_UMASK) == RESTRICTIVE_UMASK
    finally:
        os.umask(previous)
