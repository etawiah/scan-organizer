"""Card fragments, US addresses, and Title-Case name heuristic.

Fake data only — nothing in these fixtures should resemble a real person's
PII. The two dry-run leak shapes are reproduced as regressions.
"""
from __future__ import annotations

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer.privacy import (
    SensitiveTerms,
    assert_safe_for_outbound,
    gate,
    redact,
)


FAKE_NAME = "Zebulon Quill"
FAKE_NAME_OTHER = "Marta Quill"
FAKE_ADDRESS = "123 Fakeville Rd, Suite 9, Nowhere, NJ 00000"
FAKE_CARD_FRAG = "card ending 0000"

REGRESSION_NAME_AND_CARD = "Payment from Zebulon Quill, card ending 0000"
REGRESSION_NAMES_AND_ADDRESS = (
    "Zebulon and Marta Quill, 123 Fakeville Rd, Suite 9, Nowhere, NJ 00000"
)

CARD_FRAGMENT_SHAPES = (
    "ending in 0000",
    "ending 0000",
    "x-0000",
    "xxxx0000",
    "****0000",
    "...0000",
    "acct #0000",
    "last 4: 0000",
    "card ending 0000",
    "last 4:0000",
    "acct#0000",
)

ADDRESS_SHAPES = (
    "100 Fakeville St",
    "100 Fakeville Street",
    "100 Fakeville Ave",
    "100 Fakeville Rd",
    "100 Fakeville Blvd",
    "100 Fakeville Ln",
    "100 Fakeville Dr",
    "100 Fakeville Ct",
    "100 Fakeville Way",
    "100 Fakeville Pl",
    "100 Fakeville Ter",
    "100 Fakeville Cir",
    "100 Fakeville Hwy",
    "100 Fakeville Route",
    "100 Fakeville Rte",
    "100 US Route 9",
    "100 Fakeville Rd, Suite 9",
    "100 Fakeville Rd, Apt 2",
    "100 Fakeville Rd, Unit B",
    "100 Fakeville Rd, PMB 12",
    FAKE_ADDRESS,
    "NJ 00000",
    "NJ 00000-0000",
)

NAME_SHAPES = (
    "Zebulon Quill",
    "Mr. Zebulon Quill",
    "Mrs. Marta Quill",
    "Ms Marta Quill",
    "Dr. Zebulon Quill",
    "Quill, Zebulon",
    "Zebulon Marta Quill",
    "Zebulon and Marta Quill",
)


def _empty_terms() -> SensitiveTerms:
    return SensitiveTerms.load(path=None, include_builtins=False)


@pytest.mark.parametrize("raw", CARD_FRAGMENT_SHAPES)
def test_redacts_each_card_fragment_shape(raw):
    out = redact(f"Payment {raw} posted.")
    assert "0000" not in out, raw
    assert "[CARD]" in out, raw
    with pytest.raises(ValueError):
        assert_safe_for_outbound(f"Payment {raw} posted.")


@pytest.mark.parametrize("raw", ADDRESS_SHAPES)
def test_redacts_each_address_shape(raw):
    out = redact(f"Service address {raw} on file.")
    assert "Fakeville" not in out, raw
    if raw.startswith("NJ"):
        assert "00000" not in out, raw
    else:
        assert "100" not in out and "123" not in out, raw
    assert "[ADDRESS]" in out, raw
    with pytest.raises(ValueError):
        assert_safe_for_outbound(f"Service address {raw} on file.")


@pytest.mark.parametrize("raw", NAME_SHAPES)
def test_redacts_each_name_shape(raw):
    out = redact(f"Filed for {raw} today.")
    assert "Zebulon" not in out, raw
    assert "Marta" not in out, raw
    assert "Quill" not in out, raw
    assert "[NAME]" in out, raw
    with pytest.raises(ValueError):
        assert_safe_for_outbound(f"Filed for {raw} today.")


def test_allowlist_preserves_category_signals():
    text = (
        "DEPARTMENT OF THE TREASURY — Internal Revenue Service. "
        "Bank Insurance Statement from the Department. "
        "January Monday New Jersey correspondence."
    )
    out = redact(text)
    assert "Internal Revenue Service" in out
    assert "Bank" in out
    assert "Insurance" in out
    assert "Statement" in out
    assert "Department" in out
    assert "[NAME]" not in out


def test_filename_and_path_are_redacted():
    name = redact("Zebulon_Quill_statement.pdf")
    assert "Zebulon" not in name
    assert "Quill" not in name
    assert "[NAME]" in name

    path = redact("/scans/Zebulon Quill/123 Fakeville Rd.pdf")
    assert "Zebulon" not in path
    assert "Fakeville" not in path
    assert "[NAME]" in path
    assert "[ADDRESS]" in path


def test_regression_name_plus_card_fragment_final_prompt(taxonomy):
    from scanorganizer.vocabulary import extract_outbound_signals

    terms = _empty_terms()
    report = gate(REGRESSION_NAME_AND_CARD, terms)
    prompt = classifier_mod._build_prompt(
        taxonomy, extract_outbound_signals(REGRESSION_NAME_AND_CARD),
    )
    assert FAKE_NAME not in prompt
    assert "Zebulon" not in prompt
    assert "Quill" not in prompt
    assert "0000" not in prompt
    assert "Document text" not in prompt
    assert "Original filename" not in prompt
    assert "[NAME]" in report.scrubbed_text
    assert "[CARD]" in report.scrubbed_text
    assert_safe_for_outbound(prompt, taxonomy=taxonomy)
    with pytest.raises(ValueError):
        assert_safe_for_outbound(REGRESSION_NAME_AND_CARD)


def test_regression_names_plus_address_final_prompt(taxonomy):
    from scanorganizer.vocabulary import extract_outbound_signals

    terms = _empty_terms()
    report = gate(REGRESSION_NAMES_AND_ADDRESS, terms)
    prompt = classifier_mod._build_prompt(
        taxonomy, extract_outbound_signals(REGRESSION_NAMES_AND_ADDRESS),
    )
    assert "Zebulon" not in prompt
    assert "Marta" not in prompt
    assert "Quill" not in prompt
    assert "Fakeville" not in prompt
    assert "00000" not in prompt
    assert "Document text" not in prompt
    assert "Original filename" not in prompt
    assert "Original path" not in prompt
    assert "[NAME]" in report.scrubbed_text
    assert "[ADDRESS]" in report.scrubbed_text
    assert_safe_for_outbound(prompt, taxonomy=taxonomy)
    with pytest.raises(ValueError):
        assert_safe_for_outbound(REGRESSION_NAMES_AND_ADDRESS)
    with pytest.raises(ValueError):
        assert_safe_for_outbound(
            f"{FAKE_NAME} and {FAKE_NAME_OTHER}, {FAKE_ADDRESS}"
        )
