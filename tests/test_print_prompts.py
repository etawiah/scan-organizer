"""--print-prompts offline mode: exact outbound prompts, zero network."""
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
from tests.test_outbound_privacy import _write


CLEAN_BILL = "City Electric Co monthly electric bill. kWh used 541. Amount due $40.00."


@pytest.fixture()
def billed_src(tmp_scan_tree, monkeypatch):
    _write(tmp_scan_tree["src"] / "bill.pdf", CLEAN_BILL)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return CLEAN_BILL

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    return tmp_scan_tree


def test_print_prompts_requires_dry_run(tmp_scan_tree):
    with pytest.raises(SystemExit):
        main([
            "--print-prompts",
            "--src", str(tmp_scan_tree["src"]),
            "--dest", str(tmp_scan_tree["dest"]),
            "--log", str(tmp_scan_tree["log"]),
            "--non-interactive",
            "--provider", "gemini",
        ])


def test_print_prompts_never_calls_urlopen_and_writes_mode_600(
    billed_src, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-must-not-be-used")

    def boom(*_a, **_k):
        raise AssertionError("urlopen must not be called in --print-prompts mode")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", boom)
    monkeypatch.setattr("urllib.request.urlopen", boom)

    prompts_path = billed_src["log"] / "prompts-20261016T150405Z.jsonl"
    cfg = runconfig_factory(provider="gemini", dry_run=True)
    cfg.print_prompts = True
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
        assert "Document text" in prompt
    # The bill is a utility doc, so it would have gone to a provider.
    assert any(o.classification.get("print_prompts") for o in result.outcomes)


def test_print_prompts_skips_local_only_files(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*_a, **_k):
        raise AssertionError("urlopen must not be called in --print-prompts mode")

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
    cfg.print_prompts = True
    cfg.prompts_path = prompts_path
    process_batch(cfg)
    if prompts_path.exists():
        body = prompts_path.read_text(encoding="utf-8").strip()
        assert body == ""
    else:
        # Writer is only opened when print_prompts is set; a local-only
        # file must not emit a provider prompt line.
        pass
