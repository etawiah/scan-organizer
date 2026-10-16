"""Allowlist outbound prompts: every audit leak shape stays off the wire.

Fake data only. Nothing in these fixtures should resemble a real person's
PII. Shapes come from the offline audit of main (af833aa): ALL CAPS and
PO Box addresses, lone given names, surnames in emails / law-firm names,
masked last-4, VIN / title / lien numbers, a full name, and medical or
injury text under a non-Medical category.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.classifier import ClassifierSettings, _build_prompt
from scanorganizer.organizer import _classify_file
from scanorganizer.privacy import (
    SensitiveTerms,
    assert_safe_for_outbound,
    gate,
    redact,
)
from scanorganizer.vocabulary import (
    MEDICAL_INJURY_KEYWORDS,
    extract_outbound_signals,
    extract_vocab_hits,
    has_medical_or_injury,
    load_outbound_vocabulary,
)
from tests.test_outbound_privacy import _CallSpy, _write


FAKE_GIVEN = "Zebulon"
FAKE_SURNAME = "Quill"
FAKE_FULL = "Zebulon Quill"
FAKE_EMAIL = "zebulon.quill@example.test"
FAKE_LAW_FIRM = "Quill & Marsh LLP"
FAKE_VIN = "1AAAAAAAAAAA00000"
FAKE_TITLE_NO = "TITLE0001"
FAKE_LIEN_NO = "LIEN0001"
FAKE_LAST4_STAR = "****1234"
FAKE_LAST4_X = "XXXX1234"
FAKE_ADDR_CAPS = "123 FAKEVILLE RD, SUITE 9, NOWHERE, NJ 00000"
FAKE_POBOX = "PO Box 0001"
FAKE_POBOX_DOT = "P.O. Box 0001"

# Injury / physician / hospital / accident / claim — but not the local
# Medical-rule cues (patient / diagnosis / prescription) so the keyword
# pre-pass still lands on Correspondence.
MEDICAL_IN_CORRESPONDENCE = (
    "Dear tenant, this correspondence concerns an injury claim after an "
    "accident. The physician at the hospital reviewed the matter. Sincerely."
)


def _empty_terms() -> SensitiveTerms:
    return SensitiveTerms.load(path=None, include_builtins=False)


def _prompt_for(taxonomy, text: str) -> str:
    return _build_prompt(taxonomy, extract_outbound_signals(text))


def test_vocabulary_is_committed_and_excludes_medical_terms():
    vocab = load_outbound_vocabulary()
    for required in (
        "invoice", "statement", "tax", "1099", "insurance",
        "utility", "receipt", "lease", "permit",
    ):
        assert required in vocab, required
    for forbidden in MEDICAL_INJURY_KEYWORDS:
        assert forbidden not in vocab, forbidden
    # No free-form numbers besides committed form tokens.
    for word in vocab:
        if any(c.isdigit() for c in word):
            assert word in {"1099", "1040", "941", "w2"}, word


def test_signals_never_copy_ocr_names_or_numbers():
    text = (
        f"{FAKE_FULL} invoice {FAKE_EMAIL} {FAKE_ADDR_CAPS} "
        f"card {FAKE_LAST4_STAR} VIN {FAKE_VIN} amount due $40.00 dated 2026-01-15"
    )
    signals = extract_outbound_signals(text)
    blob = " ".join(signals.keywords)
    assert FAKE_GIVEN.lower() not in blob
    assert FAKE_SURNAME.lower() not in blob
    assert "1234" not in blob
    assert "00000" not in blob
    assert FAKE_VIN.lower() not in blob
    assert "example" not in blob
    assert "invoice" in signals.keywords
    assert signals.has_amounts is True
    assert signals.has_dates is True
    assert signals.pages == "unknown"


def test_page_bucket_uses_form_feeds_not_digits():
    signals = extract_outbound_signals("invoice\fstatement\fbill\fletter\fnotice")
    assert signals.pages == "many"
    assert "5" not in signals.as_prompt_block()
    single = extract_outbound_signals("invoice\f", page_count=1)
    assert single.pages == "single"


@pytest.mark.parametrize("raw", (
    FAKE_ADDR_CAPS,
    "123 FAKEVILLE RD",
    "100 FAKEVILLE STREET",
    FAKE_POBOX,
    FAKE_POBOX_DOT,
    "Post Office Box 0001",
))
def test_all_caps_and_pobox_addresses_are_redacted_and_blocked(raw):
    out = redact(f"Service address {raw} on file.")
    assert "Fakeville" not in out
    assert "FAKEVILLE" not in out
    assert "0001" not in out
    assert "[ADDRESS]" in out
    with pytest.raises(ValueError):
        assert_safe_for_outbound(f"Service address {raw} on file.")


@pytest.mark.parametrize("raw", (FAKE_LAST4_STAR, FAKE_LAST4_X, "**** 1234"))
def test_visible_last4_is_masked_and_blocked(raw):
    out = redact(f"Card {raw} charged.")
    assert "1234" not in out
    assert "[CARD]" in out
    with pytest.raises(ValueError):
        assert_safe_for_outbound(f"Card {raw} charged.")


def test_vin_and_title_lien_are_masked_and_blocked():
    vin_text = f"Vehicle registration VIN {FAKE_VIN} on file."
    title_text = f"Title number {FAKE_TITLE_NO} recorded."
    lien_text = f"Lien number {FAKE_LIEN_NO} recorded."
    assert FAKE_VIN not in redact(vin_text)
    assert "[REDACTED_VIN]" in redact(vin_text)
    assert FAKE_TITLE_NO not in redact(title_text)
    assert FAKE_LIEN_NO not in redact(lien_text)
    for raw in (vin_text, title_text, lien_text):
        with pytest.raises(ValueError):
            assert_safe_for_outbound(raw)


def test_email_with_surname_is_stripped_and_blocked():
    text = f"Please reply to {FAKE_EMAIL} about the invoice."
    out = redact(text)
    assert FAKE_EMAIL not in out
    assert FAKE_SURNAME.lower() not in out.lower()
    assert "@" not in out
    assert "[REDACTED_EMAIL]" in out
    with pytest.raises(ValueError):
        assert_safe_for_outbound(text)


def test_law_firm_name_is_redacted_and_blocked():
    text = f"Counsel: {FAKE_LAW_FIRM} filed the notice."
    out = redact(text)
    assert "Quill" not in out
    assert "Marsh" not in out
    assert "[NAME]" in out
    with pytest.raises(ValueError):
        assert_safe_for_outbound(text)


def test_lone_given_name_and_full_name_never_enter_prompt(taxonomy):
    terms = SensitiveTerms.load(
        path=None, extra_terms=[FAKE_FULL], include_builtins=False,
    )
    for raw in (FAKE_GIVEN, "ZEBULON", FAKE_FULL, "ZEBULON QUILL"):
        prompt = _prompt_for(taxonomy, f"Invoice for {raw} amount due.")
        assert FAKE_GIVEN not in prompt
        assert "ZEBULON" not in prompt
        assert FAKE_SURNAME not in prompt
        assert_safe_for_outbound(prompt, terms, taxonomy=taxonomy)
        with pytest.raises(ValueError):
            assert_safe_for_outbound(raw, terms)


def test_prompt_contains_only_vocab_and_taxonomy(taxonomy):
    text = (
        f"{FAKE_FULL} of {FAKE_LAW_FIRM} at {FAKE_ADDR_CAPS} "
        f"{FAKE_POBOX} {FAKE_EMAIL} card {FAKE_LAST4_STAR} "
        f"VIN {FAKE_VIN} Title number {FAKE_TITLE_NO}. "
        "Monthly electric utility invoice statement. Amount due $40.00."
    )
    prompt = _prompt_for(taxonomy, text)
    for leak in (
        FAKE_GIVEN, FAKE_SURNAME, "Marsh", "Fakeville", "FAKEVILLE",
        "0001", "1234", FAKE_VIN, FAKE_TITLE_NO, FAKE_EMAIL,
        "example.test", "Document text", "Original filename",
        "Original path", ".pdf",
    ):
        assert leak not in prompt, leak
    assert "invoice" in prompt
    assert "statement" in prompt
    assert "electric" in prompt or "utility" in prompt
    assert_safe_for_outbound(prompt, taxonomy=taxonomy)


def test_assert_rejects_any_token_outside_allowlist(taxonomy):
    with pytest.raises(ValueError, match="allowlist"):
        assert_safe_for_outbound("Observed keywords: zebra", taxonomy=taxonomy)
    with pytest.raises(ValueError, match="allowlist"):
        assert_safe_for_outbound("invoice 4210", taxonomy=taxonomy)


def test_medical_injury_forces_local_only_on_correspondence(
    monkeypatch, taxonomy,
):
    assert has_medical_or_injury(MEDICAL_IN_CORRESPONDENCE)
    report = gate(MEDICAL_IN_CORRESPONDENCE, _empty_terms())
    assert report.sensitive is True
    assert report.sensitive_reason == "medical-injury"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_anthropic", spy)
    result = _classify_file(
        MEDICAL_IN_CORRESPONDENCE,
        Path("/inbox/letter.pdf"),
        taxonomy,
        ClassifierSettings(provider="anthropic"),
        _empty_terms(),
    )
    assert result["provider"] == "local-keyword"
    assert result.get("privacy_blocked") is True
    assert result.get("doc_type") == "Correspondence"
    assert spy.prompts == []


def test_medical_keywords_never_extracted_as_outbound_hits():
    hits = extract_vocab_hits(MEDICAL_IN_CORRESPONDENCE)
    for keyword in ("injury", "claim", "physician", "hospital", "accident"):
        assert keyword not in hits


def test_classify_prompt_has_no_ocr_filename_or_path(monkeypatch, taxonomy):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spy = _CallSpy()
    monkeypatch.setattr(classifier_mod, "_call_gemini", spy)
    classify = classifier_mod.classify
    classify(
        "City Electric Co monthly electric bill. Amount due $40.00.",
        "Zebulon_Quill_123_Fakeville.pdf",
        taxonomy,
        ClassifierSettings(provider="gemini"),
        terms=_empty_terms(),
        file_path="/scans/Zebulon Quill/bill.pdf",
    )
    assert spy.prompts, "provider should have been called for a utility bill"
    prompt = spy.prompts[0]
    assert "Zebulon" not in prompt
    assert "Quill" not in prompt
    assert "Fakeville" not in prompt
    assert "City" not in prompt
    assert "40.00" not in prompt
    assert "Document text" not in prompt
    assert "Original filename" not in prompt
    assert "Original path" not in prompt
    assert_safe_for_outbound(prompt, taxonomy=taxonomy)


def test_print_prompts_dumps_allowlist_prompt(
    tmp_scan_tree, runconfig_factory, monkeypatch, taxonomy,
):
    from scanorganizer.organizer import process_batch
    import json
    import stat

    body = (
        f"{FAKE_FULL} {FAKE_LAW_FIRM} {FAKE_ADDR_CAPS} {FAKE_POBOX} "
        f"{FAKE_EMAIL} {FAKE_LAST4_STAR} VIN {FAKE_VIN} "
        "monthly electric utility invoice. Amount due $40.00."
    )
    _write(tmp_scan_tree["src"] / "bill.pdf", body)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return body

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*_a, **_k):
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", boom)

    prompts_path = tmp_scan_tree["log"] / "prompts-allowlist.jsonl"
    cfg = runconfig_factory(provider="gemini", dry_run=True)
    cfg.print_prompts = True
    cfg.prompts_path = prompts_path
    process_batch(cfg)
    assert prompts_path.exists()
    assert stat.S_IMODE(prompts_path.stat().st_mode) == 0o600
    recs = [
        json.loads(line)
        for line in prompts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert recs
    prompt = recs[0]["prompt"]
    for leak in (
        FAKE_GIVEN, FAKE_SURNAME, "Marsh", "FAKEVILLE", "Fakeville",
        "0001", "1234", FAKE_VIN, FAKE_EMAIL, "Document text",
    ):
        assert leak not in prompt, leak
    assert_safe_for_outbound(prompt, taxonomy=taxonomy)
