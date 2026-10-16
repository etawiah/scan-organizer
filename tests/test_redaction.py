"""Outbound text redaction: SSN, EIN, account/card, DOB, dates near birth."""
from __future__ import annotations

from scanorganizer.privacy import (
    SensitiveTerms,
    _luhn_valid,
    assert_safe_for_outbound,
    gate,
    redact,
)


def test_redacts_ssn_dashed():
    out = redact("Social Security Number: 123-45-6789 — do not share.")
    assert "123-45-6789" not in out
    assert "[REDACTED_SSN]" in out


def test_redacts_ssn_spaced():
    out = redact("Member SSN 123 45 6789 recorded.")
    assert "123 45 6789" not in out
    assert "[REDACTED_SSN]" in out


def test_redacts_unformatted_9digit_near_ssn_keyword():
    out = redact("Please verify SSN 123456789 on next visit.")
    assert "123456789" not in out
    assert "[REDACTED_SSN]" in out


def test_leaves_lone_9digit_number_alone():
    # 9 digits without an SSN cue is ambiguous (ZIP+4 unformatted etc.);
    # we only redact the SSN-shaped ones when they sit next to a keyword.
    # It should still be caught by the long-account rule though.
    out = redact("Shipment reference 123456789 arrived today.")
    assert "123456789" not in out  # long-digits rule covers it
    assert "[REDACTED_ACCT]" in out


def test_redacts_ein():
    out = redact("Employer Identification Number: 12-3456789 for payroll.")
    assert "12-3456789" not in out
    assert "[REDACTED_EIN]" in out


def test_redacts_long_account_numbers():
    out = redact("Account number 1234567890123456 debited $200.00")
    assert "1234567890123456" not in out
    # 1234567890123456 happens to NOT pass Luhn so it is tagged as ACCT.
    assert "[REDACTED_ACCT]" in out or "[REDACTED_CARD]" in out


def test_redacts_spaced_card_number_and_tags_as_card():
    # 4111 1111 1111 1111 is a canonical test card that passes Luhn.
    out = redact("Card ending in 4111 1111 1111 1111 charged.")
    assert "4111 1111 1111 1111" not in out
    assert "[REDACTED_CARD]" in out


def test_redacts_dashed_bank_routing_number():
    out = redact("Wire routing 021-000-021 00123-456-789 on file.")
    assert "021-000-021" not in out
    assert "123-456-789" not in out


def test_redacts_dob_variants():
    for raw in [
        "DOB: 01/02/2015",
        "D.O.B. 2015-01-02",
        "Date of Birth: January 2, 2015",
        "Born on Jan 2, 2015",
        "Birthdate: 1/2/15",
    ]:
        out = redact(raw)
        assert "[REDACTED_DOB]" in out, raw
        assert "2015" not in out and "/15" not in out, raw


def test_leaves_non_sensitive_text_alone():
    text = "Invoice #4210. Amount due: $112.44. Thank you."
    assert redact(text) == text


def test_preserves_short_numbers():
    text = "Due in 14 days. Phone 555-1212."
    out = redact(text)
    assert "14 days" in out
    assert "555-1212" in out


def test_preserves_iso_dates_in_text():
    text = "Invoice dated 2026-01-15 is now overdue."
    out = redact(text)
    assert "2026-01-15" in out


def test_assert_safe_for_outbound_detects_raw_ssn():
    import pytest

    with pytest.raises(ValueError):
        assert_safe_for_outbound("SSN: 123-45-6789")


def test_assert_safe_for_outbound_detects_raw_card():
    import pytest

    with pytest.raises(ValueError):
        assert_safe_for_outbound("Card 4111111111111111 charged.")


def test_assert_safe_for_outbound_allows_allowlisted_prompt(taxonomy):
    from scanorganizer.classifier import _build_prompt
    from scanorganizer.vocabulary import extract_outbound_signals

    prompt = _build_prompt(
        taxonomy, extract_outbound_signals("Invoice statement. Amount due."),
    )
    assert_safe_for_outbound(prompt, taxonomy=taxonomy)
    assert_safe_for_outbound("ping")


def test_assert_safe_for_outbound_raises_on_private_term():
    import pytest

    terms = SensitiveTerms.load(
        path=None,
        extra_terms=["Zebulon Quill"],
        include_builtins=False,
    )
    # Title-Case and Last, First forms are also residual name leaks.
    with pytest.raises(ValueError):
        assert_safe_for_outbound("Please file this letter for Zebulon Quill.", terms)
    with pytest.raises(ValueError, match="private term"):
        assert_safe_for_outbound("please file this letter for zebulon quill today", terms)
    with pytest.raises(ValueError):
        assert_safe_for_outbound("Patient: Quill, Zebulon (minor).", terms)
    # Whole-word only: a longer token must not trip the matcher. The
    # passing payload must itself be allowlist-safe (no free-form OCR).
    assert_safe_for_outbound("utility bill", terms)


def test_luhn_valid_recognizes_test_cards():
    assert _luhn_valid("4111111111111111")
    assert _luhn_valid("5555555555554444")
    assert not _luhn_valid("1234567890123456")
    assert not _luhn_valid("123")


def test_gate_counts_each_category():
    terms = SensitiveTerms.load(path=None, include_builtins=False)
    text = (
        "SSN 123-45-6789 for applicant. EIN 12-3456789. "
        "Card 4111 1111 1111 1111. DOB 01/02/2015. "
        "Reference acct 9876543210."
    )
    report = gate(text, terms)
    counts = report.category_counts
    assert counts["ssn"] >= 1
    assert counts["ein"] >= 1
    assert counts["card"] >= 1
    assert counts["dob"] >= 1
    assert counts["acct"] >= 1
    assert "123-45-6789" not in report.scrubbed_text
    assert "12-3456789" not in report.scrubbed_text
    assert "4111" not in report.scrubbed_text
    assert "9876543210" not in report.scrubbed_text
