"""Shared test fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scanorganizer.classifier import ClassifierSettings  # noqa: E402
from scanorganizer.organizer import RunConfig  # noqa: E402
from scanorganizer.privacy import SensitiveTerms  # noqa: E402
from scanorganizer.taxonomy import Taxonomy  # noqa: E402


TAXONOMY_PATH = ROOT / "taxonomy.yaml"


@pytest.fixture(autouse=True)
def _clear_private_terms_env(monkeypatch):
    """Tests must never pick up a developer's real PRIVATE_TERMS."""
    monkeypatch.delenv("PRIVATE_TERMS", raising=False)
    monkeypatch.delenv("SCAN_PRIVATE_TERMS", raising=False)


@pytest.fixture(scope="session")
def taxonomy() -> Taxonomy:
    return Taxonomy.load(TAXONOMY_PATH)


@pytest.fixture()
def tmp_scan_tree(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "Unsorted Docs"
    dest = tmp_path / "dest"
    review = dest / "_Review"
    unsorted_dir = dest / "_Unsorted"
    log = dest / "_logs"
    for d in (src, dest, review, unsorted_dir, log):
        d.mkdir(parents=True, exist_ok=True)
    return {
        "root": tmp_path,
        "src": src,
        "dest": dest,
        "review": review,
        "unsorted": unsorted_dir,
        "log": log,
    }


@pytest.fixture()
def runconfig_factory(tmp_scan_tree, taxonomy):
    def _make(
        provider: str = "local-keyword",
        dry_run: bool = False,
        limit: int | None = None,
        extra_terms: list[str] | None = None,
        threshold: float | None = None,
    ) -> RunConfig:
        settings = ClassifierSettings(provider=provider)
        terms = SensitiveTerms.load(
            path=None, extra_terms=extra_terms, include_builtins=True,
        )
        manifest = tmp_scan_tree["log"] / "manifest-test.jsonl"
        tax = Taxonomy.load(TAXONOMY_PATH)
        if threshold is not None:
            tax.confidence_threshold = threshold
        return RunConfig(
            src=tmp_scan_tree["src"],
            dest=tmp_scan_tree["dest"],
            review_dir=tmp_scan_tree["review"],
            unsorted_dir=tmp_scan_tree["unsorted"],
            manifest_path=manifest,
            taxonomy=tax,
            settings=settings,
            sensitive_terms=terms,
            dry_run=dry_run,
            limit=limit,
            recursive=False,
        )
    return _make
