"""Taxonomy loader and validator.

The YAML schema is:

    confidence_threshold: 0.75
    review_folder: _Review
    unsorted_folder: _Unsorted
    tree:
      <owner>:
        <area>:            # "" when the owner has no area layer
          - <doc_type>

Owners that have no area layer (e.g. ``Personal``) use the empty string key
``""`` so that every path in the taxonomy is still described uniformly as a
3-tuple ``(owner, area, doc_type)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from .config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_REVIEW_NAME,
    DEFAULT_UNSORTED_NAME,
)


class TaxonomyError(ValueError):
    """Raised when a taxonomy file is malformed."""


@dataclass(frozen=True)
class TaxonomyNode:
    owner: str
    area: str
    doc_type: str

    @property
    def parts(self) -> Tuple[str, ...]:
        if self.area:
            return (self.owner, self.area, self.doc_type)
        return (self.owner, self.doc_type)

    def as_relpath(self) -> Path:
        return Path(*self.parts)


@dataclass
class Taxonomy:
    tree: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    review_folder: str = DEFAULT_REVIEW_NAME
    unsorted_folder: str = DEFAULT_UNSORTED_NAME

    @classmethod
    def load(cls, path: Path | str) -> "Taxonomy":
        p = Path(path)
        if not p.exists():
            raise TaxonomyError(f"Taxonomy file not found: {p}")
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise TaxonomyError(f"Invalid YAML in {p}: {exc}") from exc
        return cls.from_mapping(data or {})

    @classmethod
    def from_mapping(cls, data: Mapping) -> "Taxonomy":
        tree_raw = data.get("tree")
        if not isinstance(tree_raw, Mapping) or not tree_raw:
            raise TaxonomyError("Taxonomy must have a non-empty top-level 'tree:' mapping.")

        tree: Dict[str, Dict[str, List[str]]] = {}
        for owner, areas in tree_raw.items():
            if not isinstance(owner, str) or not owner:
                raise TaxonomyError(f"Invalid owner key: {owner!r}")
            if not isinstance(areas, Mapping) or not areas:
                raise TaxonomyError(f"Owner {owner!r} must map to a non-empty area mapping.")
            tree[owner] = {}
            for area, doc_types in areas.items():
                area_key = "" if area is None else str(area)
                if not isinstance(doc_types, (list, tuple)) or not doc_types:
                    raise TaxonomyError(
                        f"Taxonomy node {owner}/{area_key!r} must have a non-empty list of doc_types."
                    )
                clean_types = []
                for dt in doc_types:
                    if not isinstance(dt, str) or not dt:
                        raise TaxonomyError(
                            f"Invalid doc_type {dt!r} under {owner}/{area_key!r}"
                        )
                    clean_types.append(dt)
                tree[owner][area_key] = clean_types

        threshold = data.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
        try:
            threshold = float(threshold)
        except (TypeError, ValueError) as exc:
            raise TaxonomyError(f"confidence_threshold must be numeric, got {threshold!r}") from exc
        if not (0.0 <= threshold <= 1.0):
            raise TaxonomyError("confidence_threshold must be between 0 and 1")

        return cls(
            tree=tree,
            confidence_threshold=threshold,
            review_folder=str(data.get("review_folder", DEFAULT_REVIEW_NAME)),
            unsorted_folder=str(data.get("unsorted_folder", DEFAULT_UNSORTED_NAME)),
        )

    def is_valid_node(self, owner: str, area: Optional[str], doc_type: str) -> bool:
        area_key = "" if area is None else str(area)
        return (
            owner in self.tree
            and area_key in self.tree[owner]
            and doc_type in self.tree[owner][area_key]
        )

    def node(self, owner: str, area: Optional[str], doc_type: str) -> TaxonomyNode:
        area_key = "" if area is None else str(area)
        if not self.is_valid_node(owner, area_key, doc_type):
            raise TaxonomyError(f"Not a valid taxonomy node: {owner}/{area_key}/{doc_type}")
        return TaxonomyNode(owner=owner, area=area_key, doc_type=doc_type)

    def iter_nodes(self) -> Iterable[TaxonomyNode]:
        for owner, areas in self.tree.items():
            for area, doc_types in areas.items():
                for dt in doc_types:
                    yield TaxonomyNode(owner=owner, area=area, doc_type=dt)

    def owners(self) -> List[str]:
        return list(self.tree.keys())

    def describe_for_prompt(self) -> str:
        """Human-readable listing of valid (owner, area, doc_type) tuples."""
        lines = []
        for owner, areas in self.tree.items():
            for area, doc_types in areas.items():
                prefix = f"{owner}/" if not area else f"{owner}/{area}/"
                for dt in doc_types:
                    lines.append(f"- {prefix}{dt}")
        return "\n".join(lines)
