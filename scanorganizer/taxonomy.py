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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from .config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_REVIEW_NAME,
    DEFAULT_UNSORTED_NAME,
)


class TaxonomyError(ValueError):
    """Raised when a taxonomy file is malformed."""


# Historical classifier output used "Tax"; the canonical category is "Taxes".
DEFAULT_DOC_TYPE_ALIASES: Dict[str, str] = {
    "Tax": "Taxes",
}


_PATH_SPLIT_RE = re.compile(r"[/\\]+")


def normalize_path_segments(path: str | Sequence[str] | None) -> Tuple[str, ...]:
    """Trim, drop blank segments, and collapse consecutive duplicates.

    ``'A//A/B'`` → ``('A', 'B')``. ``'Finance/Finance'`` → ``('Finance',)``.
    ``''`` → ``()``. Comparison for collapsing is case-insensitive; the first
    spelling is kept so a later case-insensitive taxonomy match can still
    recover the canonical names.
    """
    if path is None:
        return ()
    if isinstance(path, str):
        raw_parts = _PATH_SPLIT_RE.split(path.strip())
    else:
        raw_parts = []
        for item in path:
            if item is None:
                continue
            raw_parts.extend(_PATH_SPLIT_RE.split(str(item).strip()))
    parts = [p.strip() for p in raw_parts if p and p.strip()]
    collapsed: List[str] = []
    for part in parts:
        if collapsed and collapsed[-1].lower() == part.lower():
            continue
        collapsed.append(part)
    return tuple(collapsed)


def join_category_path(*segments: object) -> str:
    """Join taxonomy segments into a canonical path with no empty ``//``.

    Owners that have no subgroup (YAML area key ``""``) must be
    ``Personal/Medical``, never ``Personal//Medical``. Blank segments,
    surrounding whitespace, and consecutive slashes are dropped.
    """
    return "/".join(normalize_path_segments(tuple("" if s is None else str(s) for s in segments)))


def alias_doc_type(
    doc_type: str,
    aliases: Optional[Mapping[str, str]] = None,
) -> str:
    """Map a legacy doc_type (e.g. ``Tax``) onto its canonical name."""
    if not doc_type:
        return doc_type
    table = dict(DEFAULT_DOC_TYPE_ALIASES)
    if aliases:
        table.update(dict(aliases))
    if doc_type in table:
        return table[doc_type]
    lowered = {k.lower(): v for k, v in table.items()}
    return lowered.get(doc_type.lower(), doc_type)


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

    def as_canonical_path(self) -> str:
        """Leaf path used in prompts, validation, folders, and the manifest."""
        return join_category_path(*self.parts)

    def as_relpath(self) -> Path:
        return Path(*self.parts)


@dataclass(frozen=True)
class PathMatch:
    """A taxonomy leaf resolved from a model path, with optional suffix snap."""

    node: TaxonomyNode
    snapped: bool = False
    original: str = ""


@dataclass
class Taxonomy:
    tree: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    review_folder: str = DEFAULT_REVIEW_NAME
    unsorted_folder: str = DEFAULT_UNSORTED_NAME
    doc_type_aliases: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DOC_TYPE_ALIASES))

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
        aliases = dict(DEFAULT_DOC_TYPE_ALIASES)
        raw_aliases = data.get("doc_type_aliases") or data.get("aliases") or {}
        if isinstance(raw_aliases, Mapping):
            for src, dst in raw_aliases.items():
                if isinstance(src, str) and isinstance(dst, str) and src and dst:
                    aliases[src] = dst

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
                    canonical = alias_doc_type(dt, aliases)
                    if canonical not in clean_types:
                        clean_types.append(canonical)
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
            doc_type_aliases=aliases,
        )

    def canonicalize_doc_type(self, doc_type: str) -> str:
        return alias_doc_type(doc_type, self.doc_type_aliases)

    @staticmethod
    def _ci_key(mapping: Mapping[str, object], key: str) -> Optional[str]:
        if key in mapping:
            return key
        lowered = key.lower()
        for existing in mapping:
            if existing.lower() == lowered:
                return existing
        return None

    def _ci_doc_type(self, candidates: Sequence[str], doc_type: str) -> Optional[str]:
        canonical = self.canonicalize_doc_type(doc_type)
        hit = self._ci_key({name: True for name in candidates}, canonical)
        if hit is not None:
            return hit
        if canonical != doc_type:
            return self._ci_key({name: True for name in candidates}, doc_type)
        return None

    def is_valid_node(self, owner: str, area: Optional[str], doc_type: str) -> bool:
        return self.match_node(owner, area, doc_type) is not None

    def match_node(
        self,
        owner: str,
        area: Optional[str],
        doc_type: str,
    ) -> Optional[TaxonomyNode]:
        """Case-insensitive lookup; returns the canonical tree names or None."""
        owner_key = self._ci_key(self.tree, str(owner or ""))
        if owner_key is None:
            return None
        area_key = "" if area is None else str(area)
        areas = self.tree[owner_key]
        area_hit = self._ci_key(areas, area_key)
        if area_hit is None:
            return None
        dt_hit = self._ci_doc_type(areas[area_hit], doc_type)
        if dt_hit is None:
            return None
        return TaxonomyNode(owner=owner_key, area=area_hit, doc_type=dt_hit)

    def match_path(self, path: str | Sequence[str] | None) -> Optional[TaxonomyNode]:
        """Normalize a path and match it case-insensitively against the tree.

        2 segments → ``(owner, '', doc_type)``. 3 segments →
        ``(owner, area, doc_type)``. Anything else (empty, 1, or 4+) is
        invalid and never filed.
        """
        parts = normalize_path_segments(path)
        if len(parts) == 2:
            return self.match_node(parts[0], "", parts[1])
        if len(parts) == 3:
            return self.match_node(parts[0], parts[1], parts[2])
        return None

    def match_classification(self, classification: Mapping[str, object]) -> Optional[TaxonomyNode]:
        """Resolve a model payload to a taxonomy node, or None if invalid.

        Exact leaf only. Trailing doc-type suffixes are handled by
        :meth:`match_or_snap_classification`.
        """
        for raw in self._classification_raw_paths(classification):
            node = self.match_path(raw)
            if node is not None:
                return node
        return None

    def _classification_raw_paths(
        self,
        classification: Mapping[str, object],
    ) -> List[str]:
        """Candidate path strings from a model payload, in priority order."""
        raws: List[str] = []
        for key in ("category", "path", "node"):
            raw = classification.get(key)
            if raw:
                raws.append(str(raw))
        owner = str(classification.get("owner") or "")
        area = classification.get("area")
        doc_type = str(classification.get("doc_type") or "")
        joined = join_category_path(owner, "" if area is None else str(area), doc_type)
        if joined:
            raws.append(joined)
        # Last chance: treat owner itself as a full path (e.g. ``A//A/B``).
        if owner and not str(area or "").strip() and not doc_type:
            raws.append(owner)
        return raws

    def match_or_snap_path(self, path: str | Sequence[str] | None) -> Optional[PathMatch]:
        """Exact leaf match after normalize, or snap a trailing doc-type suffix.

        Normalization trims whitespace, is case-insensitive, and collapses
        ``//``. Extra segments after a valid leaf (``Taxes//W-2``,
        ``Taxes/W-2``) snap to that leaf. Prefixes that are not themselves
        leaves are never used, so this will not jump from
        ``Business/432SouthStVT/Vehicles`` to ``Household/Vehicles`` or
        across top-level owners. Anything else is invalid.
        """
        if isinstance(path, str):
            original = path
        elif path is None:
            original = ""
        else:
            original = join_category_path(*path)
        parts = normalize_path_segments(path)
        if not parts:
            return None
        exact = self.match_path(parts)
        if exact is not None:
            return PathMatch(node=exact, snapped=False, original=original)
        # Longest prefix that is a leaf. Only drop trailing segments;
        # never rewrite owner or area to a sibling business.
        for end in range(len(parts) - 1, 0, -1):
            node = self.match_path(parts[:end])
            if node is not None:
                return PathMatch(node=node, snapped=True, original=original)
        return None

    def match_or_snap_classification(
        self,
        classification: Mapping[str, object],
    ) -> Optional[PathMatch]:
        """Resolve a model payload, snapping a trailing doc-type suffix if needed."""
        for raw in self._classification_raw_paths(classification):
            hit = self.match_or_snap_path(raw)
            if hit is not None:
                return hit
        return None

    def node(self, owner: str, area: Optional[str], doc_type: str) -> TaxonomyNode:
        area_key = "" if area is None else str(area)
        matched = self.match_node(owner, area_key, doc_type)
        if matched is None:
            raise TaxonomyError(f"Not a valid taxonomy node: {owner}/{area_key}/{doc_type}")
        return matched

    def iter_nodes(self) -> Iterable[TaxonomyNode]:
        for owner, areas in self.tree.items():
            for area, doc_types in areas.items():
                for dt in doc_types:
                    yield TaxonomyNode(owner=owner, area=area, doc_type=dt)

    def owners(self) -> List[str]:
        return list(self.tree.keys())

    def leaf_paths(self) -> List[str]:
        """Canonical leaf category paths (no empty segments)."""
        return [node.as_canonical_path() for node in self.iter_nodes()]

    def describe_for_prompt(self) -> str:
        """Human-readable listing of valid leaf category paths."""
        return "\n".join(f"- {path}" for path in self.leaf_paths())
