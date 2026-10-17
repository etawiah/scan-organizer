"""Taxonomy loader + validator."""
from __future__ import annotations

from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.organizer import _resolve_node, process_batch
from scanorganizer.taxonomy import (
    Taxonomy,
    TaxonomyError,
    join_category_path,
    normalize_path_segments,
)
from tests.test_outbound_privacy import _write


def test_default_taxonomy_loads(taxonomy):
    assert "Business" in taxonomy.owners()
    assert "Personal" in taxonomy.owners()
    assert "Household" in taxonomy.owners()
    assert "Campaign" in taxonomy.owners()
    assert "Family" in taxonomy.owners()


def test_valid_nodes_cover_spec(taxonomy):
    assert taxonomy.is_valid_node("Business", "ComplexTech", "Invoices-Out")
    assert taxonomy.is_valid_node("Business", "432SouthStVT", "PropertyTax")
    assert taxonomy.is_valid_node("Personal", "", "Taxes")
    assert taxonomy.is_valid_node("Personal", None, "Medical")
    assert taxonomy.is_valid_node("Household", "Vehicles", "Registration")


def test_invalid_nodes_rejected(taxonomy):
    assert not taxonomy.is_valid_node("Business", "ComplexTech", "Nope")
    assert not taxonomy.is_valid_node("Business", "WrongArea", "Invoices-Out")
    assert not taxonomy.is_valid_node("Nonsense", "", "X")


def test_personal_has_no_area(taxonomy):
    node = taxonomy.node("Personal", "", "Taxes")
    assert node.as_relpath().as_posix() == "Personal/Taxes"


def test_business_has_area(taxonomy):
    node = taxonomy.node("Business", "ComplexTech", "Invoices-Out")
    assert node.as_relpath().as_posix() == "Business/ComplexTech/Invoices-Out"


def test_threshold_bounds():
    with pytest.raises(TaxonomyError):
        Taxonomy.from_mapping({"tree": {"X": {"": ["a"]}}, "confidence_threshold": 2.0})


def test_tree_required():
    with pytest.raises(TaxonomyError):
        Taxonomy.from_mapping({})


def test_tax_alias_maps_to_taxes(taxonomy):
    assert "Tax" not in taxonomy.tree["Business"]["ComplexTech"]
    assert "Taxes" in taxonomy.tree["Business"]["ComplexTech"]
    assert "Tax" not in taxonomy.tree["Business"]["ElJetSit"]
    assert "Taxes" in taxonomy.tree["Business"]["ElJetSit"]

    personal = taxonomy.node("Personal", "", "Tax")
    assert personal.doc_type == "Taxes"
    assert personal.as_relpath().as_posix() == "Personal/Taxes"

    business = taxonomy.node("Business", "ComplexTech", "Tax")
    assert business.doc_type == "Taxes"
    assert business.as_relpath().as_posix() == "Business/ComplexTech/Taxes"
    assert taxonomy.is_valid_node("Business", "ElJetSit", "Tax")


def test_tax_in_mapping_is_merged_to_taxes():
    tax = Taxonomy.from_mapping({
        "tree": {"Business": {"X": ["Tax", "Taxes"]}},
        "confidence_threshold": 0.5,
    })
    assert tax.tree["Business"]["X"] == ["Taxes"]
    assert tax.node("Business", "X", "Tax").doc_type == "Taxes"


def test_join_category_path_skips_empty_area_segment():
    assert join_category_path("Personal", "", "Medical") == "Personal/Medical"
    assert join_category_path("Personal", None, "Medical") == "Personal/Medical"
    assert join_category_path("Business", "ComplexTech", "Taxes") == (
        "Business/ComplexTech/Taxes"
    )
    assert join_category_path("Personal", "  ", "Medical") == "Personal/Medical"
    assert "//" not in join_category_path("Personal", "", "Medical")


def test_canonical_leaf_paths_have_no_empty_segments(taxonomy):
    leaves = taxonomy.leaf_paths()
    assert "Personal/Medical" in leaves
    assert "Personal/Taxes" in leaves
    assert "Business/ComplexTech/Taxes" in leaves
    assert "Household/Vehicles/Registration" in leaves
    assert "Personal//Medical" not in leaves
    listing = taxonomy.describe_for_prompt()
    assert "//" not in listing
    assert "- Personal/Medical" in listing
    assert "- Business/ComplexTech/Taxes" in listing
    for node in taxonomy.iter_nodes():
        path = node.as_canonical_path()
        assert path
        assert "//" not in path
        assert path == node.as_relpath().as_posix()


def test_normalize_path_collapses_blanks_and_repeats():
    assert normalize_path_segments("A//A/B") == ("A", "B")
    assert normalize_path_segments("") == ()
    assert normalize_path_segments("   ") == ()
    assert normalize_path_segments("Finance/Finance") == ("Finance",)
    assert normalize_path_segments("Personal//Taxes") == ("Personal", "Taxes")
    assert normalize_path_segments("Personal/Personal/Taxes") == ("Personal", "Taxes")
    assert normalize_path_segments("  Business / / ComplexTech / Invoices-Out ") == (
        "Business", "ComplexTech", "Invoices-Out",
    )


def test_malformed_paths_do_not_match_taxonomy(taxonomy):
    assert taxonomy.match_path("A//A/B") is None
    assert taxonomy.match_path("") is None
    assert taxonomy.match_path("Finance/Finance") is None
    assert _resolve_node({"owner": "A//A/B", "area": "", "doc_type": "", "confidence": 0.99}, taxonomy) is None
    assert _resolve_node({"owner": "", "area": "", "doc_type": "", "confidence": 0.99}, taxonomy) is None
    assert _resolve_node({"category": "Finance/Finance", "confidence": 0.99}, taxonomy) is None


def test_match_path_is_case_insensitive(taxonomy):
    node = taxonomy.match_path("personal/taxes")
    assert node is not None
    assert node.owner == "Personal"
    assert node.area == ""
    assert node.doc_type == "Taxes"
    node = taxonomy.match_path("BUSINESS/complextech/invoices-out")
    assert node is not None
    assert (node.owner, node.area, node.doc_type) == (
        "Business", "ComplexTech", "Invoices-Out",
    )


def test_malformed_model_paths_go_to_review_never_filed(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    """Every provider: empty / A//A/B / Finance/Finance land in _Review/."""
    _write(tmp_scan_tree["src"] / "bill.pdf", "City Electric Co monthly electric bill. kWh 40.")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "City Electric Co monthly electric bill. kWh 40."

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)

    for raw in ("A//A/B", "", "Finance/Finance"):
        def fake_classify(*_a, path=raw, **_k):
            return {
                "owner": path,
                "area": "",
                "doc_type": "",
                "confidence": 0.99,
                "short_name": "should_not_file",
                "provider": "gemini",
            }

        monkeypatch.setattr(classifier_mod, "classify", fake_classify)
        # unique dest so leftover files from a prior iteration don't collide
        cfg = runconfig_factory(provider="gemini", dry_run=True)
        result = process_batch(cfg)
        assert result.outcomes, raw
        outcome = result.outcomes[0]
        assert outcome.action == "review", raw
        assert cfg.review_dir in outcome.dest.parents, raw
        # Never a taxonomy folder.
        if outcome.dest is not None:
            assert "_Review" in outcome.dest.parts, raw
            assert "should_not_file" not in outcome.dest.name


def test_case_insensitive_valid_path_is_filed(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    _write(tmp_scan_tree["src"] / "bill.pdf", "City Electric Co monthly electric bill. kWh 40.")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "City Electric Co monthly electric bill. kWh 40."

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)

    def fake_classify(*_a, **_k):
        return {
            "owner": "household",
            "area": "home",
            "doc_type": "utilities",
            "confidence": 0.99,
            "short_name": "electric_bill",
            "provider": "gemini",
        }

    monkeypatch.setattr(classifier_mod, "classify", fake_classify)
    cfg = runconfig_factory(provider="gemini")
    result = process_batch(cfg)
    outcome = result.outcomes[0]
    assert outcome.action == "move"
    assert outcome.dest is not None
    assert outcome.dest.relative_to(cfg.dest).parts[:3] == ("Household", "Home", "Utilities")


def test_personal_double_slash_is_exact_canonical_not_snapped(taxonomy):
    hit = taxonomy.match_or_snap_path("Personal//Medical")
    assert hit is not None
    assert hit.snapped is False
    assert hit.node.as_canonical_path() == "Personal/Medical"
    node = _resolve_node(
        {"category": "Personal//Medical", "confidence": 0.99},
        taxonomy,
    )
    assert node is not None
    assert node.as_canonical_path() == "Personal/Medical"


def test_snap_doc_type_suffix_to_longest_leaf(taxonomy):
    hit = taxonomy.match_or_snap_path("Business/ComplexTech/Taxes//W-2")
    assert hit is not None
    assert hit.snapped is True
    assert hit.node.as_canonical_path() == "Business/ComplexTech/Taxes"
    assert hit.original == "Business/ComplexTech/Taxes//W-2"

    hit = taxonomy.match_or_snap_path("Business/ComplexTech/Taxes/W-2")
    assert hit is not None and hit.snapped
    assert hit.node.as_canonical_path() == "Business/ComplexTech/Taxes"

    classification = {
        "category": "  business / complextech / taxes // w-2 ",
        "confidence": 0.99,
    }
    node = _resolve_node(classification, taxonomy)
    assert node is not None
    assert node.as_canonical_path() == "Business/ComplexTech/Taxes"
    assert classification["snapped"] is True
    assert classification["snapped_from"]
    assert classification["category"] == "Business/ComplexTech/Taxes"
    assert classification["confidence"] == 0.99


def test_snap_does_not_cross_owners_or_sibling_businesses(taxonomy):
    assert taxonomy.match_or_snap_path("Business//Bills-In") is None
    assert taxonomy.match_or_snap_path("Business/432SouthStVT/Vehicles//OrderForm") is None
    assert taxonomy.match_or_snap_path("Business/432SouthStVT/Vehicles/OrderForm") is None
    # Must not become Household/Vehicles or another business.
    assert taxonomy.match_or_snap_path("Personal/ComplexTech/Taxes") is None
    assert taxonomy.match_or_snap_path("Campaign/ComplexTech/Taxes") is None
    assert _resolve_node(
        {"category": "Business/432SouthStVT/Vehicles//OrderForm", "confidence": 0.99},
        taxonomy,
    ) is None


def test_snapped_suffix_goes_to_review_and_is_marked(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    _write(tmp_scan_tree["src"] / "w2.pdf", "City Electric Co monthly electric bill. kWh 40.")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "City Electric Co monthly electric bill. kWh 40."

    def fake_classify(*_a, **_k):
        return {
            "category": "Business/ComplexTech/Taxes//W-2",
            "confidence": 0.99,
            "reason": "form looks like a w2",
            "provider": "ollama",
            "is_local": True,
        }

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setattr(classifier_mod, "classify", fake_classify)
    cfg = runconfig_factory(provider="ollama")
    result = process_batch(cfg)
    outcome = result.outcomes[0]
    assert outcome.action == "review"
    assert cfg.review_dir in outcome.dest.parents
    assert outcome.classification.get("snapped") is True
    assert outcome.classification.get("snapped_from") == "Business/ComplexTech/Taxes//W-2"
    assert outcome.classification.get("category") == "Business/ComplexTech/Taxes"
    assert outcome.reason == "snapped category suffix"


def test_snapped_goes_to_review_even_when_threshold_is_zero(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    _write(tmp_scan_tree["src"] / "w2.pdf", "City Electric Co monthly electric bill. kWh 40.")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "City Electric Co monthly electric bill. kWh 40."

    def fake_classify(*_a, **_k):
        return {
            "category": "Business/ComplexTech/Taxes//W-2",
            "confidence": 0.99,
            "reason": "form looks like a w2",
            "provider": "ollama",
            "is_local": True,
        }

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setattr(classifier_mod, "classify", fake_classify)
    cfg = runconfig_factory(provider="ollama", threshold=0.0)
    assert cfg.taxonomy.confidence_threshold == 0.0
    result = process_batch(cfg)
    outcome = result.outcomes[0]
    assert outcome.action == "review"
    assert cfg.review_dir in outcome.dest.parents
    assert outcome.classification.get("snapped") is True
    assert outcome.classification.get("confidence") == 0.99
    assert outcome.reason == "snapped category suffix"


def test_personal_double_slash_category_files_when_confident(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    _write(tmp_scan_tree["src"] / "letter.pdf", "City Electric Co monthly electric bill. kWh 40.")

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "City Electric Co monthly electric bill. kWh 40."

    def fake_classify(*_a, **_k):
        return {
            "category": "Household//Home//Utilities",
            "confidence": 0.99,
            "reason": "electric bill",
            "provider": "ollama",
            "is_local": True,
        }

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setattr(classifier_mod, "classify", fake_classify)
    cfg = runconfig_factory(provider="ollama")
    result = process_batch(cfg)
    outcome = result.outcomes[0]
    assert outcome.action == "move"
    assert outcome.dest is not None
    assert outcome.dest.relative_to(cfg.dest).parts[:3] == ("Household", "Home", "Utilities")
    assert outcome.classification.get("snapped") is False
    assert outcome.classification.get("category") == "Household/Home/Utilities"
