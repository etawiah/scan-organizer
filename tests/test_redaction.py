"""Outbound text redaction."""
from __future__ import annotations

from scanorganizer.privacy import redact


def test_redacts_ssn():
    out = redact("Social Security Number: 123-45-6789 — do not share.")
    assert "123-45-6789" not in out
    assert "[REDACTED_SSN]" in out


def test_redacts_long_account_numbers():
    out = redact("Account number 1234567890123456 debited $200.00")
    assert "1234567890123456" not in out
    assert "[REDACTED_ACCT]" in out


def test_redacts_spaced_card_numbers():
    out = redact("Card ending in 4111 1111 1111 1111 charged.")
    assert "4111 1111 1111 1111" not in out
    assert "[REDACTED_ACCT]" in out


def test_redacts_dob_variants():
    for raw in [
        "DOB: 01/02/2015",
        "D.O.B. 2015-01-02",
        "Date of Birth: January 2, 2015",
        "Born on Jan 2, 2015",
    ]:
        out = redact(raw)
        assert "[REDACTED_DOB]" in out, raw
        assert "2015" not in out, raw


def test_leaves_non_sensitive_text_alone():
    text = "Invoice #4210. Amount due: $112.44. Thank you."
    assert redact(text) == text


def test_preserves_short_numbers():
    text = "Due in 14 days. Phone 555-1212."
    out = redact(text)
    assert "14 days" in out
    assert "555-1212" in out
