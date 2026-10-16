"""--prompts-only (offline) and --print-prompts (dump then classify)."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.cli import main
from scanorganizer.organizer import process_batch
from scanorganizer.privacy import assert_safe_for_outbound
from tests.test_outbound_privacy import _CallSpy, _write


CLEAN_BILL = "City Electric Co monthly electric bill. kWh used 541. Amount due $40.00."


@pytest.fixture()
def billed_src(tmp_scan_tree, monkeypatch):
    _write(tmp_scan_tree["src"] / "bill.pdf", CLEAN_BILL)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return CLEAN_BILL

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    return tmp_scan_tree


def test_prompts_only_requires_dry_run(tmp_scan_tree):
    with pytest.raises(SystemExit):
        main([
            "--prompts-only",
            "--src", str(tmp_scan_tree["src"]),
            "--dest", str(tmp_scan_tree["dest"]),
            "--log", str(tmp_scan_tree["log"]),
            "--non-interactive",
            "--provider", "gemini",
        ])


def test_print_prompts_does_not_require_dry_run(tmp_scan_tree):
    code = main([
        "--print-prompts",
        "--src", str(tmp_scan_tree["src"]),
        "--dest", str(tmp_scan_tree["dest"]),
        "--log", str(tmp_scan_tree["log"]),
        "--non-interactive",
        "--provider", "local-keyword",
    ])
    assert code == 0


def test_prompts_only_never_calls_urlopen_and_writes_mode_600(
    billed_src, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-must-not-be-used")

    def boom(*_a, **_k):
        raise AssertionError("urlopen must not be called in --prompts-only mode")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", boom)
    monkeypatch.setattr("urllib.request.urlopen", boom)

    prompts_path = billed_src["log"] / "prompts-20261016T150405Z.jsonl"
    cfg = runconfig_factory(provider="gemini", dry_run=True)
    cfg.prompts_only = True
    cfg.prompts_path = prompts_path
    result = process_batch(cfg)

    assert result.outcomes, "expected at least one planned file"
    assert prompts_path.exists()
    mode = stat.S_IMODE(prompts_path.stat().st_mode)
    assert mode == 0o600, oct(mode)

    lines = [json.loads(line) for line in prompts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "expected at least one dumped prompt"
    for rec in lines:
        prompt = rec["prompt"]
        assert_safe_for_outbound(prompt)
        assert "Zebulon" not in prompt
        assert "Document text" not in prompt
        assert "Original filename" not in prompt
        assert "City" not in prompt
        assert "541" not in prompt
        assert "40" not in prompt
        assert "electric" in prompt.lower() or "bill" in prompt.lower()
        assert "Observed keywords" in prompt
    assert any(o.classification.get("prompts_only") for o in result.outcomes)


def test_print_prompts_dumps_then_classifies(
    billed_src, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)

    prompts_path = billed_src["log"] / "prompts-then-classify.jsonl"
    cfg = runconfig_factory(provider="gemini", dry_run=True)
    cfg.print_prompts = True
    cfg.prompts_only = False
    cfg.prompts_path = prompts_path
    result = process_batch(cfg)

    assert prompts_path.exists()
    lines = [json.loads(line) for line in prompts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    assert spy.prompts, "--print-prompts must still call the provider"
    assert not any(o.classification.get("prompts_only") for o in result.outcomes)


def test_prompts_only_skips_local_only_files(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*_a, **_k):
        raise AssertionError("urlopen must not be called in --prompts-only mode")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", boom)

    _write(
        tmp_scan_tree["src"] / "bank.pdf",
        "checking account monthly statement deposit withdrawal",
    )

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "checking account monthly statement deposit withdrawal"

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    prompts_path = tmp_scan_tree["log"] / "prompts-test.jsonl"
    cfg = runconfig_factory(provider="gemini", dry_run=True)
    cfg.prompts_only = True
    cfg.prompts_path = prompts_path
    process_batch(cfg)
    if prompts_path.exists():
        body = prompts_path.read_text(encoding="utf-8").strip()
        assert body == ""
