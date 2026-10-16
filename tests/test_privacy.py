"""Private-term matching and the gate's local-only verdict.

These tests use obviously-fake names on purpose. Nothing resembling a real
person's PII should ever appear in a test fixture.
"""
from __future__ import annotations

from scanorganizer.privacy import SensitiveTerms, evaluate, gate


def test_builtin_terms_match_minor_keywords():
    terms = SensitiveTerms.load(path=None, include_builtins=True)
    assert terms.contains_sensitive("Immunization record for well-child visit.")
    assert terms.contains_sensitive("Report Card — Fall 2026")
    assert terms.contains_sensitive("DOB: 01/02/2015")
    assert not terms.contains_sensitive("Monthly electric bill for 12 Main St.")


def test_user_terms_file(tmp_path):
    f = tmp_path / "private_terms.txt"
    f.write_text(
        "# user terms\n"
        "Lincoln Placeholder Elementary\n"
        "Jane Placeholder-Doe\n",
        encoding="utf-8",
    )
    terms = SensitiveTerms.load(path=f, include_builtins=False)
    assert terms.contains_sensitive("Lincoln placeholder elementary parent newsletter")
    assert terms.contains_sensitive("From the desk of jane placeholder-doe")
    assert not terms.contains_sensitive("Vehicle registration renewal")


def test_user_terms_whole_word_only():
    terms = SensitiveTerms.load(
        path=None,
        extra_terms=["Smithson"],
        include_builtins=False,
    )
    # Substring but inside a different word should NOT match.
    assert not terms.contains_sensitive("The blacksmithson quarterly review.")
    assert terms.contains_sensitive("Mr. Smithson signed the form.")


def test_last_first_variant_matched():
    terms = SensitiveTerms.load(
        path=None,
        extra_terms=["Jane Fictional"],
        include_builtins=False,
    )
    assert terms.contains_sensitive("Patient: Jane Fictional, age 9.")
    assert terms.contains_sensitive("Patient: Fictional, Jane (minor).")


def test_evaluate_blocks_outbound_and_reports_count_only():
    terms = SensitiveTerms.load(
        path=None,
        extra_terms=["Lincoln Placeholder Elementary"],
        include_builtins=True,
    )
    text = "Lincoln Placeholder Elementary report card — grade 4."
    decision = evaluate(text, terms)
    assert decision.outbound_ok is False
    assert decision.scrubbed_text == ""
    # ``matched_terms`` must be count-only; never contain the real strings.
    assert all(m == "<redacted>" for m in decision.matched_terms)
    # The report still exposes counts/categories for the manifest.
    assert decision.report is not None
    assert decision.report.sensitive is True
    assert decision.report.sensitive_reason == "private-term-match"
    assert decision.report.category_counts["private_term"] >= 2


def test_evaluate_allows_benign_text():
    terms = SensitiveTerms.load(path=None, include_builtins=True)
    text = "Electric company monthly statement for 12 Main Street."
    decision = evaluate(text, terms)
    assert decision.outbound_ok is True
    assert decision.scrubbed_text == text


def test_gate_classifies_as_sensitive_on_residual_pii_only(monkeypatch):
    terms = SensitiveTerms.load(path=None, include_builtins=False)
    # No private-term hits, no topic keywords — but a pure 9-digit sequence
    # near "ITIN" should be caught as an SSN-like value and both scrubbed
    # AND flagged sensitive (because the gate's residual check still fires
    # for the raw value if a future regression ever stops scrubbing it).
    text = "Vendor ITIN 987654321 on file."
    report = gate(text, terms)
    assert report.category_counts["ssn"] >= 1
    assert "987654321" not in report.scrubbed_text


def test_gate_topic_density_without_private_terms():
    terms = SensitiveTerms.load(path=None, include_builtins=False)
    text = (
        "This is a report card for homeroom B. The pediatric immunization "
        "schedule is up to date."
    )
    report = gate(text, terms)
    assert report.sensitive is True
    assert report.sensitive_reason in {"topic-density", "residual-pii"}


def test_gate_private_term_values_never_in_scrubbed_text():
    terms = SensitiveTerms.load(
        path=None,
        extra_terms=["Jane Fictional", "Placeholder Elementary"],
        include_builtins=False,
    )
    text = (
        "Jane Fictional attends Placeholder Elementary and the "
        "guardian signed the field trip form."
    )
    report = gate(text, terms)
    assert report.sensitive is True
    assert "Jane Fictional" not in report.scrubbed_text
    assert "Placeholder Elementary" not in report.scrubbed_text
    assert "[REDACTED_TERM]" in report.scrubbed_text


def test_count_hits_is_count_only():
    terms = SensitiveTerms.load(
        path=None,
        extra_terms=["Jane Fictional"],
        include_builtins=False,
    )
    assert terms.count_hits("Jane Fictional met Fictional, Jane.") == 2
    assert terms.count_hits("Nothing to see here.") == 0
