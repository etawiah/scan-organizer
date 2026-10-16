"""Committed outbound allowlist: generic vocabulary + coarse document features.

A provider prompt may contain only:

* taxonomy-category names
* keywords from the committed vocabulary that were found in the document
* coarse features (page bucket, has-dates, has-amounts)
* the fixed prompt-scaffold words

OCR text, filenames, and paths are never interpolated. Numbers, names,
emails, and addresses are never emitted. Medical / injury keywords are
absent from the vocabulary and force a local-only classification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Iterable, Optional, Tuple

from .dates import date_from_text
from .taxonomy import Taxonomy


# Tokens reserved for the content-free provider probe. Never document text.
RESERVED_OUTBOUND_TOKENS: FrozenSet[str] = frozenset({"ping"})

# Medical / injury words that force local-only regardless of taxonomy
# category. These must stay OUT of the outbound vocabulary.
MEDICAL_INJURY_KEYWORDS: Tuple[str, ...] = (
    "diagnosis",
    "injury",
    "injuries",
    "injured",
    "claim",
    "claims",
    "claimant",
    "physician",
    "hospital",
    "prescription",
    "accident",
    "patient",
    "patients",
    "medical",
    "clinic",
    "pharmacy",
    "doctor",
    "surgeon",
    "treatment",
    "pediatric",
    "immunization",
    "well-child",
    "wellchild",
    "icd",
    "icd-10",
    "icd10",
    "disability",
    "therapist",
    "therapy",
    "nurse",
    "nursing",
    "eob",
    "diagnosis",
    "prognosis",
)

# Digit-free prompt scaffold. Every alphanumeric token here is allowed
# outbound. Do not add digits, dates, or names to this string.
PROMPT_SCAFFOLD = """
You are classifying scanned household business documents into a strict taxonomy.

ALLOWED taxonomy nodes owner area doc_type. You must pick EXACTLY one.
Use empty string for area when the node has no area segment.

Return ONLY a JSON object with these keys nothing else
owner string must be one of the owners above
area string empty if the owner has no area segment
doc_type string must match the owner area pair
doc_date string empty if unknown
confidence float reflecting how sure you are about owner area doc_type
short_name short descriptive name lowercase words underscores no file extension no PII

If you cannot confidently place the document in any node set confidence low
rather than guessing the caller will route low confidence documents for human review

Observed keywords
Coarse features pages dates amounts yes no none single few many unknown
"""

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_WORD_BOUNDARY_TEMPLATE = r"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"

_AMOUNT_RE = re.compile(
    r"""(?ix)
    (?:
        \$[\d,]+(?:\.\d{2})?
        |
        \b(?:amount|total|balance|due|paid|owed|fee)\b[^\n]{0,24}\d
        |
        \b\d{1,3}(?:,\d{3})+\.\d{2}\b
    )
    """
)


def _vocab_paths() -> Tuple[Path, ...]:
    here = Path(__file__).resolve().parent.parent
    return (
        here / "config" / "outbound_vocabulary.txt",
        Path("config/outbound_vocabulary.txt"),
    )


def _load_vocab_file(path: Path) -> Tuple[str, ...]:
    words: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words.append(line.lower())
    return tuple(words)


@lru_cache(maxsize=1)
def load_outbound_vocabulary() -> FrozenSet[str]:
    """Return the committed generic vocabulary (lowercase, unique)."""
    seen: set[str] = set()
    for path in _vocab_paths():
        try:
            if path.exists():
                for word in _load_vocab_file(path):
                    if word:
                        seen.add(word)
                break
        except OSError:
            continue
    if not seen:
        raise RuntimeError("outbound vocabulary file is missing or empty")
    return frozenset(seen)


def scaffold_tokens() -> FrozenSet[str]:
    return frozenset(t.lower() for t in _TOKEN_RE.findall(PROMPT_SCAFFOLD))


def taxonomy_tokens(taxonomy: Optional[Taxonomy] = None) -> FrozenSet[str]:
    """Alphanumeric tokens from taxonomy category names (hyphens split)."""
    tax = taxonomy if taxonomy is not None else _default_taxonomy()
    tokens: set[str] = set()
    for node in tax.iter_nodes():
        for part in (node.owner, node.area, node.doc_type):
            if not part:
                continue
            for tok in _TOKEN_RE.findall(part):
                tokens.add(tok.lower())
    for extra in tax.doc_type_aliases.keys():
        for tok in _TOKEN_RE.findall(str(extra)):
            tokens.add(tok.lower())
    for extra in tax.doc_type_aliases.values():
        for tok in _TOKEN_RE.findall(str(extra)):
            tokens.add(tok.lower())
    return frozenset(tokens)


@lru_cache(maxsize=1)
def _default_taxonomy() -> Taxonomy:
    here = Path(__file__).resolve().parent.parent
    return Taxonomy.load(here / "taxonomy.yaml")


def allowed_outbound_tokens(taxonomy: Optional[Taxonomy] = None) -> FrozenSet[str]:
    """Union of vocabulary, taxonomy names, scaffold, and reserved tokens."""
    return frozenset(
        set(load_outbound_vocabulary())
        | set(taxonomy_tokens(taxonomy))
        | set(scaffold_tokens())
        | set(RESERVED_OUTBOUND_TOKENS)
    )


def iter_outbound_tokens(text: str) -> Tuple[str, ...]:
    """Lowercased alphanumeric tokens in ``text`` (hyphens/punct split)."""
    if not text:
        return ()
    return tuple(t.lower() for t in _TOKEN_RE.findall(text))


def _vocab_pattern(word: str) -> str:
    """Build a document matcher for one vocab word.

    Form tokens such as ``w2`` also match the hyphenated ``w-2`` shape.
    """
    if word.isalnum() and any(c.isdigit() for c in word) and any(c.isalpha() for c in word):
        return "-?".join(re.escape(ch) for ch in word)
    return re.escape(word)


def extract_vocab_hits(text: str, vocab: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
    """Return committed vocab words that appear in ``text`` (lowercase, sorted)."""
    if not text:
        return ()
    words = frozenset(vocab) if vocab is not None else load_outbound_vocabulary()
    lowered = text.lower()
    found: set[str] = set()
    for word in words:
        pat = _WORD_BOUNDARY_TEMPLATE.format(pat=_vocab_pattern(word))
        if re.search(pat, lowered):
            found.add(word)
    return tuple(sorted(found))


def page_bucket(text: str, page_count: Optional[int] = None) -> str:
    """Coarse page count: ``single`` / ``few`` / ``many`` / ``unknown``.

    Never emits a raw digit. Unknown unless a count is supplied or the
    OCR text uses form-feed page breaks.
    """
    count = page_count
    if count is None and text and "\f" in text:
        count = text.count("\f") + 1
    if count is None:
        return "unknown"
    if count <= 1:
        return "single"
    if count <= 4:
        return "few"
    return "many"


def has_dates(text: str) -> bool:
    return date_from_text(text) is not None


def has_amounts(text: str) -> bool:
    return bool(text) and _AMOUNT_RE.search(text) is not None


def has_medical_or_injury(text: str) -> bool:
    """True when document text contains a medical / injury keyword."""
    if not text:
        return False
    lowered = text.lower()
    for keyword in MEDICAL_INJURY_KEYWORDS:
        pat = _WORD_BOUNDARY_TEMPLATE.format(pat=re.escape(keyword))
        if re.search(pat, lowered):
            return True
    return False


@dataclass(frozen=True)
class OutboundSignals:
    """The only document-derived payload that may leave the host."""

    keywords: Tuple[str, ...]
    pages: str
    has_dates: bool
    has_amounts: bool

    def as_prompt_block(self) -> str:
        kw = " ".join(self.keywords) if self.keywords else "none"
        dates = "yes" if self.has_dates else "no"
        amounts = "yes" if self.has_amounts else "no"
        return (
            f"Observed keywords: {kw}\n"
            f"Coarse features: pages {self.pages} dates {dates} amounts {amounts}"
        )


def extract_outbound_signals(
    text: str,
    page_count: Optional[int] = None,
) -> OutboundSignals:
    """Derive allowlisted signals. Never copies OCR, names, or numbers."""
    return OutboundSignals(
        keywords=extract_vocab_hits(text),
        pages=page_bucket(text, page_count=page_count),
        has_dates=has_dates(text),
        has_amounts=has_amounts(text),
    )


def disallowed_tokens(
    text: str,
    taxonomy: Optional[Taxonomy] = None,
) -> Tuple[str, ...]:
    """Tokens in ``text`` that are not in the outbound allowlist."""
    allowed = allowed_outbound_tokens(taxonomy)
    return tuple(tok for tok in iter_outbound_tokens(text) if tok not in allowed)
