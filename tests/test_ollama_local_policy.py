"""Ollama stays on-LAN for private-term / kids files; never falls back to cloud."""
from __future__ import annotations

import json
from pathlib import Path

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.classifier import ClassifierSettings, classify
from scanorganizer.organizer import process_batch
from tests.test_outbound_privacy import _write


PRIVATE_TERM_KIDS = (
    "Placeholder Elementary Report Card — grade 4 — immunization record. "
    "Student Zebulon Quill."
)
PRIVATE_TERM = "Zebulon Quill"


def _seed_private_file(tmp_scan_tree, monkeypatch, name="zebulon_report_card.pdf"):
    _write(tmp_scan_tree["src"] / name, PRIVATE_TERM_KIDS)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return PRIVATE_TERM_KIDS

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    return PRIVATE_TERM_KIDS


def test_ollama_private_term_file_reaches_ollama_with_full_text(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.3.10:11434")
    _seed_private_file(tmp_scan_tree, monkeypatch)
    captured = []

    def fake_ollama(prompt, settings):
        captured.append(prompt)
        return json.dumps({
            "owner": "Family",
            "area": "Kids",
            "doc_type": "School",
            "confidence": 0.91,
            "short_name": "report_card",
            "doc_date": "",
        })

    monkeypatch.setattr(classifier_mod, "_call_ollama", fake_ollama)
    cfg = runconfig_factory(provider="ollama", extra_terms=[PRIVATE_TERM])
    result = process_batch(cfg)
    assert captured, "expected the private-term file to reach Ollama"
    assert PRIVATE_TERM_KIDS in captured[0]
    assert "Zebulon Quill" in captured[0]
    assert "[REDACTED_TERM]" not in captured[0]
    outcome = result.outcomes[0]
    assert outcome.provider == "ollama"
    assert not outcome.classification.get("privacy_blocked")
    report = outcome.classification.get("privacy_report") or {}
    counts = report.get("redaction_counts") or {}
    assert counts.get("private_term", 0) >= 1


def test_same_private_term_file_under_gemini_never_reaches_provider(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-must-not-be-used")
    _seed_private_file(tmp_scan_tree, monkeypatch)

    def boom(*_a, **_k):
        raise AssertionError("cloud provider must not be called for a private-term file")

    monkeypatch.setattr(classifier_mod, "_call_gemini", boom)
    monkeypatch.setattr(classifier_mod, "_call_anthropic", boom)
    cfg = runconfig_factory(provider="gemini", extra_terms=[PRIVATE_TERM])
    result = process_batch(cfg)
    assert result.outcomes
    outcome = result.outcomes[0]
    assert outcome.provider == "local-keyword"
    assert outcome.classification.get("privacy_blocked") is True
    assert outcome.classification.get("privacy") == "local-only: sensitive"


def test_ollama_failure_never_invokes_cloud_even_when_keys_set(
    tmp_scan_tree, runconfig_factory, monkeypatch, taxonomy,
):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-must-not-be-used")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-must-not-be-used")
    _seed_private_file(tmp_scan_tree, monkeypatch)

    def ollama_boom(*_a, **_k):
        raise TimeoutError("ollama timed out")

    def cloud_boom(*_a, **_k):
        raise AssertionError("gemini/anthropic must not be invoked when ollama fails")

    monkeypatch.setattr(classifier_mod, "_call_ollama", ollama_boom)
    monkeypatch.setattr(classifier_mod, "_call_gemini", cloud_boom)
    monkeypatch.setattr(classifier_mod, "_call_anthropic", cloud_boom)

    cfg = runconfig_factory(provider="ollama", extra_terms=[PRIVATE_TERM])
    result = process_batch(cfg)
    assert result.outcomes
    outcome = result.outcomes[0]
    assert outcome.action == "review"
    assert outcome.reason == "ollama-error"
    assert outcome.provider == "ollama"
    assert outcome.classification.get("ollama_error") is True
    assert cfg.review_dir in outcome.dest.parents

    # Direct classify() path as well: no cloud functions, review payload.
    settings = ClassifierSettings(provider="ollama")
    direct = classify(PRIVATE_TERM_KIDS, "kids.pdf", taxonomy, settings)
    assert direct.get("ollama_error") is True
    assert direct["provider"] == "ollama"
