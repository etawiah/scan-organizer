"""Local keyword classifier over representative OCR text samples."""
from __future__ import annotations

import pytest

from scanorganizer import local_rules
from scanorganizer.classifier import ClassifierSettings, _coerce, classify
from scanorganizer.taxonomy import Taxonomy


SAMPLES = [
    (
        """DEPARTMENT OF THE TREASURY — INTERNAL REVENUE SERVICE
        Form 1040 — U.S. Individual Income Tax Return
        Tax Year 2025. Filing status: single. Total income $84,210.00
        Refund due: $1,230.00""",
        ("Personal", "", "Taxes"),
    ),
    (
        """CITY ELECTRIC CO — Monthly electric bill
        Service address: 12 Main St.  Billing period: Sep 2026
        kWh used: 541.  Amount due: $112.44.""",
        ("Household", "Home", "Utilities"),
    ),
    (
        """STATE DMV — VEHICLE REGISTRATION RENEWAL
        VIN 1HGCM82633A004352.  Plate ABC-1234.
        Registration fee: $86.00.""",
        ("Household", "Vehicles", "Registration"),
    ),
    (
        """BR Kids First — Campaign Finance Filing
        FEC quarterly disclosure report.
        Total contributions this quarter: $4,210.""",
        ("Campaign", "BRKidsFirst", "Filings-Compliance"),
    ),
    (
        """COMPLEX TECH LLC — Statement of Work
        Master Service Agreement reference MSA-2026-04.
        SOW scope: platform integration deliverables.""",
        ("Business", "ComplexTech", "Contracts"),
    ),
    (
        """432 South Street VT LLC — Tenant Lease Agreement
        Monthly rent $1,800.  Term: 12 months.""",
        ("Business", "432SouthStVT", "Lease-Tenants"),
    ),
]


@pytest.mark.parametrize("text,expected", SAMPLES, ids=[e[2] for _, e in SAMPLES])
def test_local_classifier_routes_sample_text(taxonomy, text, expected):
    result = local_rules.classify(text, taxonomy, "scan.pdf")
    assert (result["owner"], result["area"], result["doc_type"]) == expected
    assert 0.5 <= result["confidence"] <= 0.9


def test_unknown_text_routes_to_low_confidence(taxonomy):
    result = local_rules.classify(
        "lorem ipsum dolor sit amet consectetur adipiscing elit",
        taxonomy, "scan.pdf",
    )
    assert result["confidence"] < taxonomy.confidence_threshold


def test_classifier_dispatch_local_keyword(taxonomy):
    settings = ClassifierSettings(provider="local-keyword")
    result = classify(
        "The IRS requested additional 1040 schedule information for tax year 2025.",
        "irs.pdf", taxonomy, settings,
    )
    assert result["owner"] == "Personal"
    assert result["doc_type"] == "Taxes"
    assert result["provider"] == "local-keyword"


def test_coerce_maps_tax_alias_to_taxes():
    out = _coerce({
        "owner": "Business",
        "area": "ComplexTech",
        "doc_type": "Tax",
        "confidence": 0.9,
        "short_name": "corp_return",
    })
    assert out["doc_type"] == "Taxes"


def test_classifier_falls_back_when_remote_missing(taxonomy, monkeypatch):
    for env_var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)
    settings = ClassifierSettings(provider="anthropic")
    result = classify(
        "Vehicle registration VIN 1HGCM82633A004352 — DMV renewal.",
        "scan.pdf", taxonomy, settings,
    )
    assert result["provider"] == "local-keyword"
    assert result["owner"] == "Household"
    assert result.get("fallback_from") == "anthropic"
