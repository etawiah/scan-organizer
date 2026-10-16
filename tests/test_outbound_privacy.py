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
from scanorganizer.organizer import _classify_file, process_batch
from scanorganizer.privacy import (
    SensitiveTerms,
    assert_safe_for_outbound,
    gate,
    sanitize_outbound_label,
)


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

    def __call__(self, prompt: str, settings: ClassifierSettings, *args, **kwargs) -> str:
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


FAKE_PRIVATE_NAME = "Zebulon Quill"
FAKE_PRIVATE_FILENAME = "notes Zebulon Quill.pdf"


def test_filename_with_private_term_is_local_only_and_makes_no_network_call(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)

    _write(tmp_scan_tree["src"] / FAKE_PRIVATE_FILENAME, "utility bill")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "City Electric Co monthly electric bill. Amount due $40.00."

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)

    cfg = runconfig_factory(provider="anthropic", extra_terms=[FAKE_PRIVATE_NAME])
    result = process_batch(cfg)
    outcome = next(o for o in result.outcomes if o.src.name == FAKE_PRIVATE_FILENAME)
    assert outcome.provider == "local-keyword"
    assert outcome.classification.get("privacy") == "local-only: sensitive"
    assert outcome.classification.get("privacy_blocked") is True
    report = outcome.classification.get("privacy_report") or {}
    assert report.get("sensitive") is True
    assert report.get("sensitive_reason") == "private-term-match"
    assert spy.prompts == [], "provider must not be called when filename has a private term"


def test_path_with_private_term_is_local_only_and_makes_no_network_call(
    monkeypatch, taxonomy,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)
    terms = SensitiveTerms.load(
        path=None, extra_terms=[FAKE_PRIVATE_NAME], include_builtins=False,
    )
    result = _classify_file(
        "City Electric Co monthly electric bill. Amount due $40.00.",
        Path(f"/scans/{FAKE_PRIVATE_NAME}/invoice.pdf"),
        taxonomy,
        ClassifierSettings(provider="anthropic"),
        terms,
    )
    assert result["provider"] == "local-keyword"
    assert result.get("privacy") == "local-only: sensitive"
    assert spy.prompts == [], "provider must not be called when path has a private term"


def test_prompt_never_contains_raw_filename_when_it_has_a_private_term(
    monkeypatch, taxonomy,
):
    terms = SensitiveTerms.load(
        path=None, extra_terms=[FAKE_PRIVATE_NAME], include_builtins=False,
    )
    raw_name = FAKE_PRIVATE_FILENAME
    raw_path = f"/inbox/{FAKE_PRIVATE_FILENAME}"
    safe_name = sanitize_outbound_label(raw_name, terms)
    safe_path = sanitize_outbound_label(raw_path, terms)
    assert safe_name == ""
    assert safe_path == ""

    prompt = classifier_mod._build_prompt(
        taxonomy,
        "City Electric Co monthly electric bill. Amount due $40.00.",
        filename=safe_name,
        path=safe_path,
    )
    assert FAKE_PRIVATE_NAME not in prompt
    assert "Zebulon" not in prompt
    assert raw_name not in prompt
    assert "Original filename:" not in prompt

    # classify() itself must also refuse the cloud path and never interpolate
    # the raw filename into a prompt that would go on the wire.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    built: List[str] = []
    orig_build = classifier_mod._build_prompt

    def wrapped_build(*args, **kwargs):
        prompt_out = orig_build(*args, **kwargs)
        built.append(prompt_out)
        return prompt_out

    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_build_prompt", wrapped_build)
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)
    result = classify(
        "City Electric Co monthly electric bill. Amount due $40.00.",
        raw_name,
        taxonomy,
        ClassifierSettings(provider="anthropic"),
        terms=terms,
        file_path=raw_path,
    )
    assert result["provider"] == "local-keyword"
    assert result.get("privacy") == "local-only: sensitive"
    assert spy.prompts == []
    for assembled in built:
        assert FAKE_PRIVATE_NAME not in assembled
        assert raw_name not in assembled


def test_gemini_sends_api_key_in_header_not_query(monkeypatch, taxonomy):
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret-key-xyz")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            payload = {
                "owner": "Personal",
                "area": "",
                "doc_type": "Correspondence",
                "doc_date": "",
                "confidence": 0.9,
                "short_name": "letter",
            }
            body = {
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(payload)}]}}
                ]
            }
            return json.dumps(body).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {str(k).lower(): v for k, v in req.headers.items()}
        unredirected = getattr(req, "unredirected_hdrs", {}) or {}
        captured["unredirected"] = {str(k).lower(): v for k, v in unredirected.items()}
        return _Resp()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    terms = SensitiveTerms.load(
        path=None, extra_terms=[FAKE_PRIVATE_NAME], include_builtins=False,
    )
    result = classify(
        "City Electric Co monthly statement. Amount due $40.00.",
        "bill.pdf",
        taxonomy,
        ClassifierSettings(provider="gemini"),
        terms=terms,
    )
    assert captured.get("url"), "Gemini HTTP was not attempted"
    assert "key=" not in captured["url"]
    assert "test-secret-key-xyz" not in captured["url"]
    header_val = (
        captured["headers"].get("x-goog-api-key")
        or captured["unredirected"].get("x-goog-api-key")
    )
    assert header_val == "test-secret-key-xyz"
    assert result["provider"] == "gemini"


def test_assert_safe_for_outbound_raises_on_private_term_in_prompt():
    terms = SensitiveTerms.load(
        path=None, extra_terms=[FAKE_PRIVATE_NAME], include_builtins=False,
    )
    with pytest.raises(ValueError, match="private term"):
        assert_safe_for_outbound(
            f"Original filename: notes {FAKE_PRIVATE_NAME}.pdf\nDocument text",
            terms,
        )
