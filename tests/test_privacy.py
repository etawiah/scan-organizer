"""Privacy filter: term matching + outbound gating."""
from __future__ import annotations

from scanorganizer.privacy import SensitiveTerms, evaluate


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
        "Lincoln Elementary\n"
        "Jane Placeholder-Doe\n",
        encoding="utf-8",
    )
    terms = SensitiveTerms.load(path=f, include_builtins=False)
    assert terms.contains_sensitive("Lincoln elementary parent newsletter")
    assert terms.contains_sensitive("From the desk of jane placeholder-doe")
    assert not terms.contains_sensitive("Vehicle registration renewal")


def test_evaluate_blocks_outbound_and_reports_matches():
    terms = SensitiveTerms.load(path=None, extra_terms=["Lincoln Elementary"],
                                include_builtins=True)
    text = "Lincoln Elementary report card — grade 4."
    decision = evaluate(text, terms)
    assert decision.outbound_ok is False
    assert "Lincoln Elementary" in decision.matched_terms
    assert "report card" in decision.matched_terms
    assert decision.scrubbed_text == ""


def test_evaluate_allows_benign_text():
    terms = SensitiveTerms.load(path=None, include_builtins=True)
    text = "Electric company monthly statement for 12 Main Street."
    decision = evaluate(text, terms)
    assert decision.outbound_ok is True
    assert decision.scrubbed_text == text
