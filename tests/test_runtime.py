"""Per-file runtime is logged and the summary prints the average."""
from __future__ import annotations

from pathlib import Path

from scanorganizer import ocr as ocr_mod
from scanorganizer.cli import _print_outcomes
from scanorganizer.manifest import read_entries
from scanorganizer.organizer import FileOutcome, process_batch
from scanorganizer.summary import summarize, write_summary
from tests.test_outbound_privacy import _write


CLEAN_BILL = "City Electric Co monthly electric bill. kWh used 541. Amount due $40.00."


def test_process_batch_records_elapsed_and_summary_average(
    tmp_scan_tree, runconfig_factory, monkeypatch, capsys,
):
    _write(tmp_scan_tree["src"] / "a.pdf", CLEAN_BILL)
    _write(tmp_scan_tree["src"] / "b.pdf", CLEAN_BILL)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return CLEAN_BILL

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    cfg = runconfig_factory(provider="local-keyword", dry_run=True)
    result = process_batch(cfg)
    assert len(result.outcomes) == 2
    for outcome in result.outcomes:
        assert outcome.elapsed_s > 0

    entries = list(read_entries(cfg.manifest_path))
    assert entries
    assert all(e.elapsed_s > 0 for e in entries)

    summary = summarize(entries)
    assert summary["timed_files"] == 2
    assert summary["avg_elapsed_s"] is not None
    assert summary["avg_elapsed_s"] > 0

    out = write_summary(summary, tmp_scan_tree["log"] / "summary-runtime.md")
    body = out.read_text(encoding="utf-8")
    assert "## Runtime" in body
    assert "Average per-file:" in body

    _print_outcomes(result.outcomes, dry_run=True)
    printed = capsys.readouterr().out
    assert "s)" in printed


def test_print_outcomes_includes_elapsed(capsys):
    outcomes = [
        FileOutcome(
            src=Path("a.pdf"), dest=Path("x"), action="plan",
            elapsed_s=1.25,
        ),
        FileOutcome(
            src=Path("b.pdf"), dest=Path("y"), action="review",
            elapsed_s=0.5,
        ),
    ]
    _print_outcomes(outcomes, dry_run=True)
    printed = capsys.readouterr().out
    assert "1.25s" in printed
    assert "0.50s" in printed
