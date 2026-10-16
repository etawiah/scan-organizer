"""Owner-rule guarantees around every provider call.

* No children's PII reaches Anthropic *or* Gemini.
* No SSN, EIN, account/card number or similar ID reaches either provider.
* Private-term matches short-circuit all provider calls (``local-only:
  sensitive``) and never appear in logs or manifests.

Fake names only — nothing in these fixtures should resemble a real person's
PII.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.classifier import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    ClassifierSettings,
    classify,
)
from scanorganizer.manifest import read_entries
from scanorganizer.organizer import process_batch
from scanorganizer.privacy import SensitiveTerms, gate


FORBIDDEN_FRAGMENTS = (
    "123-45-6789",
    "123456789",
    "12-3456789",
    "4111 1111 1111 1111",
    "4111111111111111",
    "9876543210",
    "01/02/2015",
    "Jane Fictional",
    "Fictional, Jane",
    "Placeholder Elementary",
)


class _CallSpy:
    """Capture prompts sent to a provider without ever hitting the network."""

    def __init__(self) -> None:
        self.prompts: List[str] = []

    def __call__(self, prompt: str, settings: ClassifierSettings) -> str:
        self.prompts.append(prompt)
        return json.dumps({
            "owner": "Personal",
            "area": "",
            "doc_type": "Correspondence",
            "doc_date": "",
            "confidence": 0.9,
            "short_name": "letter",
        })


def _write(path: Path, body: str) -> None:
    path.write_bytes(b"%PDF-1.4\n" + body.encode() + b"\n")


# -- Direct classifier-level assertions --------------------------------------


def test_default_models_are_cheap_older_tiers():
    assert DEFAULT_ANTHROPIC_MODEL == "claude-3-haiku-20240307"
    assert DEFAULT_GEMINI_MODEL == "gemini-2.0-flash"
    # Belt-and-suspenders: must not point at a 'sonnet', 'opus', or
    # 2.5-flash tier by default.
    for forbidden in ("sonnet", "opus", "2.5", "pro"):
        assert forbidden not in DEFAULT_ANTHROPIC_MODEL.lower()
        assert forbidden not in DEFAULT_GEMINI_MODEL.lower()


def test_anthropic_prompt_never_contains_pii(monkeypatch, taxonomy):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)

    # Caller is responsible for passing through the gate first.
    terms = SensitiveTerms.load(path=None, include_builtins=False)
    text = (
        "Invoice #4210 for consulting. SSN 123-45-6789 was on the form. "
        "Card 4111 1111 1111 1111 charged $112.44."
    )
    report = gate(text, terms)
    settings = ClassifierSettings(provider="anthropic")
    classify(report.scrubbed_text, "invoice.pdf", taxonomy, settings)

    assert spy.prompts, "provider spy was not invoked"
    for prompt in spy.prompts:
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in prompt, f"leaked {fragment!r} to Anthropic"


def test_gemini_prompt_never_contains_pii(monkeypatch, taxonomy):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)

    terms = SensitiveTerms.load(path=None, include_builtins=False)
    text = (
        "Vehicle registration renewal. EIN 12-3456789. "
        "Account number 9876543210 on file for payment."
    )
    report = gate(text, terms)
    settings = ClassifierSettings(provider="gemini")
    classify(report.scrubbed_text, "renewal.pdf", taxonomy, settings)

    assert spy.prompts, "provider spy was not invoked"
    for prompt in spy.prompts:
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in prompt, f"leaked {fragment!r} to Gemini"


def test_sanitize_calls_run_before_wire(monkeypatch, taxonomy):
    """If a buggy future caller skips the gate the provider must still refuse."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_opener(*_a, **_kw):
        raise AssertionError("HTTP must not be invoked when prompt has PII")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_opener)
    settings = ClassifierSettings(provider="anthropic")
    unscrubbed = "SSN on record: 123-45-6789. Tax year 2025."
    result = classify(unscrubbed, "leak.pdf", taxonomy, settings)
    # Fallback must have fired because the defensive guard refused the call.
    assert result["provider"] == "local-keyword"
    assert result.get("fallback_from") == "anthropic"


# -- End-to-end organizer flow -----------------------------------------------


SAMPLE_TEXTS = {
    "anthropic_candidate.pdf": (
        "Invoice #4210 from ACME contractor. SSN 123-45-6789 appeared on page 2. "
        "Card 4111 1111 1111 1111 was used. Amount due $112.44."
    ),
    "sensitive_child.pdf": (
        "Jane Fictional attends Placeholder Elementary. Report card for "
        "homeroom B — grade 4. Immunization records attached."
    ),
}


@pytest.fixture()
def seeded_src(tmp_scan_tree, monkeypatch):
    for name, body in SAMPLE_TEXTS.items():
        _write(tmp_scan_tree["src"] / name, body)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return SAMPLE_TEXTS.get(path.name, "")

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    return tmp_scan_tree


def test_organizer_private_terms_route_local_and_manifest_hides_values(
    seeded_src, runconfig_factory, monkeypatch, caplog,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)

    cfg = runconfig_factory(
        provider="anthropic",
        extra_terms=["Jane Fictional", "Placeholder Elementary"],
    )
    caplog.set_level(logging.DEBUG)
    result = process_batch(cfg)

    # Both the sensitive-child doc stayed local; the clean (non-child) doc
    # with scrubbed numbers was allowed to call the provider.
    outcomes = {o.src.name: o for o in result.outcomes}
    assert outcomes["sensitive_child.pdf"].provider == "local-keyword"

    # Manifest must flag the local-only verdict and record only counts.
    entries = list(read_entries(cfg.manifest_path))
    sensitive_entries = [
        e for e in entries if e.src.endswith("sensitive_child.pdf")
    ]
    assert sensitive_entries, "no manifest entry for sensitive_child.pdf"
    sentinel = sensitive_entries[-1]
    assert sentinel.classification.get("privacy") == "local-only: sensitive"
    report = sentinel.classification.get("privacy_report") or {}
    assert report.get("sensitive") is True
    assert report.get("sensitive_reason") == "private-term-match"
    counts = report.get("redaction_counts") or {}
    assert counts.get("private_term", 0) >= 1
    # The manifest line must not contain any private term values.
    line = json.dumps(sentinel.classification, ensure_ascii=False)
    for fragment in ("Jane Fictional", "Placeholder Elementary"):
        assert fragment not in line, f"manifest leaked {fragment!r}"

    # Nothing in captured logs may contain the private terms either.
    for record in caplog.records:
        msg = record.getMessage()
        for fragment in ("Jane Fictional", "Placeholder Elementary",
                         "123-45-6789", "4111 1111 1111 1111"):
            assert fragment not in msg, f"log leaked {fragment!r}"

    # And the only prompt(s) sent to a provider must not have the PII of
    # either document.
    for prompt in spy.prompts:
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in prompt, f"prompt leaked {fragment!r}"


def test_sensitive_topic_density_blocks_outbound_even_without_term(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)

    _write(
        tmp_scan_tree["src"] / "well_child.pdf",
        "Pediatric well-child visit — immunization schedule reviewed and updated.",
    )

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return (
            "Pediatric well-child visit — immunization schedule reviewed "
            "and updated."
        )

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)

    cfg = runconfig_factory(provider="anthropic", extra_terms=None)
    cfg.sensitive_terms = SensitiveTerms.load(path=None, include_builtins=False)
    result = process_batch(cfg)
    outcome = next(o for o in result.outcomes if o.src.name == "well_child.pdf")
    # Even though no user-supplied private term matched, the topical signals
    # (pediatric + well-child + immunization) are over threshold -> local.
    assert outcome.provider == "local-keyword"
    assert spy.prompts == [], "provider must not be called for topic-density doc"
