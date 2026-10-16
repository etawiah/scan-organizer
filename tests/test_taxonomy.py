"""Taxonomy loader + validator."""
from __future__ import annotations

import pytest

from scanorganizer.taxonomy import Taxonomy, TaxonomyError


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
