"""LOCAL_ONLY_CATEGORIES: Finance-Bank / ID-Legal / Medical stay on-host."""
from __future__ import annotations

from pathlib import Path

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.config import (
    DEFAULT_LOCAL_ONLY_CATEGORIES,
    is_local_only_category,
    local_only_categories,
)
from scanorganizer.organizer import process_batch
from tests.test_outbound_privacy import _CallSpy, _write


def test_default_local_only_categories():
    assert local_only_categories() == frozenset(DEFAULT_LOCAL_ONLY_CATEGORIES)
    assert is_local_only_category("Finance-Bank")
    assert is_local_only_category("ID-Legal")
    assert is_local_only_category("Medical")
    assert not is_local_only_category("Correspondence")
    assert not is_local_only_category("Taxes")


def test_local_only_categories_env_override(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY_CATEGORIES", "Taxes,Receipts")
    assert local_only_categories() == frozenset({"Taxes", "Receipts"})
    assert is_local_only_category("Taxes")
    assert not is_local_only_category("Finance-Bank")


def test_local_only_categories_empty_disables(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY_CATEGORIES", "")
    assert local_only_categories() == frozenset()
    assert not is_local_only_category("Medical")


def test_finance_bank_prepass_is_local_only(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)

    _write(
        tmp_scan_tree["src"] / "bank.pdf",
        "checking account monthly statement deposit withdrawal",
    )

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "checking account monthly statement deposit withdrawal"

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    cfg = runconfig_factory(provider="gemini")
    result = process_batch(cfg)
    outcome = next(o for o in result.outcomes if o.src.name == "bank.pdf")
    assert outcome.provider == "local-keyword"
    assert outcome.classification.get("privacy") == "local-only: category"
    assert outcome.classification.get("privacy_blocked") is True
    assert spy.prompts == []


def test_id_legal_prepass_is_local_only(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)

    _write(tmp_scan_tree["src"] / "id.pdf", "driver license")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "driver's license and notary stamp on the passport page."

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    cfg = runconfig_factory(provider="anthropic")
    result = process_batch(cfg)
    outcome = next(o for o in result.outcomes if o.src.name == "id.pdf")
    assert outcome.provider == "local-keyword"
    assert outcome.classification.get("doc_type") == "ID-Legal"
    assert outcome.classification.get("privacy") == "local-only: category"
    assert spy.prompts == []


def test_medical_prepass_is_local_only(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)

    _write(tmp_scan_tree["src"] / "med.pdf", "patient")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "patient diagnosis and prescription refill."

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    cfg = runconfig_factory(provider="anthropic")
    result = process_batch(cfg)
    outcome = next(o for o in result.outcomes if o.src.name == "med.pdf")
    assert outcome.provider == "local-keyword"
    assert outcome.classification.get("privacy_blocked") is True
    assert spy.prompts == []


def test_empty_local_only_allows_bank_outbound(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("LOCAL_ONLY_CATEGORIES", "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)

    _write(
        tmp_scan_tree["src"] / "bank.pdf",
        "checking account monthly statement deposit withdrawal",
    )

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "checking account monthly statement deposit withdrawal"

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    cfg = runconfig_factory(provider="gemini")
    result = process_batch(cfg)
    outcome = next(o for o in result.outcomes if o.src.name == "bank.pdf")
    assert spy.prompts, "bank statement should go outbound when policy is empty"
    assert outcome.classification.get("privacy") != "local-only: category"
