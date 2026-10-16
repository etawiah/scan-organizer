"""Privacy filter: local sensitive-terms matching + outbound text redaction.

The filter is intentionally conservative. If any term from the user's private
terms file matches the OCR'd text (case-insensitive), we classify locally and
send NOTHING outbound. For documents that *are* sent to an external API we
still redact SSNs, long account numbers, and dates of birth so the vendor only
ever sees scrubbed text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

BUILTIN_SENSITIVE_TERMS: Tuple[str, ...] = (
    "school",
    "pediatric",
    "report card",
    "immunization",
    "DOB",
    "guardian",
    "parent-teacher",
    "IEP",
    "Section 504",
    "minor child",
    "custody",
)


def _load_terms_file(path: Path) -> List[str]:
    terms: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms


@dataclass
class SensitiveTerms:
    """Case-insensitive matcher over an in-memory set of sensitive terms."""

    terms: List[str] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        path: Optional[Path | str] = None,
        extra_terms: Optional[Iterable[str]] = None,
        include_builtins: bool = True,
    ) -> "SensitiveTerms":
        collected: List[str] = []
        if include_builtins:
            collected.extend(BUILTIN_SENSITIVE_TERMS)
        if path:
            p = Path(path)
            if p.exists():
                collected.extend(_load_terms_file(p))
        if extra_terms:
            collected.extend(t for t in extra_terms if t)
        seen: Set[str] = set()
        unique: List[str] = []
        for term in collected:
            key = term.lower()
            if key not in seen:
                seen.add(key)
                unique.append(term)
        return cls(terms=unique)

    def _compiled(self) -> List[re.Pattern[str]]:
        patterns: List[re.Pattern[str]] = []
        for term in self.terms:
            escaped = re.escape(term)
            if re.match(r"\w", term) and re.search(r"\w$", term):
                pattern = rf"\b{escaped}\b"
            else:
                pattern = escaped
            patterns.append(re.compile(pattern, re.IGNORECASE))
        return patterns

    def matches(self, text: str) -> List[str]:
        if not text:
            return []
        hits: List[str] = []
        for term, pattern in zip(self.terms, self._compiled()):
            if pattern.search(text):
                hits.append(term)
        return hits

    def contains_sensitive(self, text: str) -> bool:
        return bool(self.matches(text))


# -- Redaction patterns -------------------------------------------------------

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_ACCT_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{10,}(?!\d)")

_ACCT_SPACED_RE = re.compile(
    r"(?<!\d)(?:\d[\s-]?){11,24}\d(?!\d)"
)

_DOB_LABELED_RE = re.compile(
    r"(?i)\b(?:DOB|D\.O\.B\.?|date\s+of\s+birth|born(?:\s+on)?)[^\n]{0,20}?"
    r"(?P<date>"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"|(?:\d{4}-\d{2}-\d{2})"
    r"|(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4})"
    r")"
)


def redact(text: str) -> str:
    """Scrub SSNs, long account numbers, and labeled DOBs from *text*."""
    if not text:
        return text

    out = _SSN_RE.sub("[REDACTED_SSN]", text)

    def _dob_sub(match: re.Match[str]) -> str:
        head = match.group(0)[: match.start("date") - match.start()]
        return f"{head}[REDACTED_DOB]"

    out = _DOB_LABELED_RE.sub(_dob_sub, out)
    out = _ACCT_SPACED_RE.sub("[REDACTED_ACCT]", out)
    out = _ACCT_LONG_DIGITS_RE.sub("[REDACTED_ACCT]", out)
    return out


@dataclass
class PrivacyDecision:
    outbound_ok: bool
    matched_terms: Sequence[str]
    scrubbed_text: str


def evaluate(text: str, terms: SensitiveTerms) -> PrivacyDecision:
    hits = terms.matches(text) if text else []
    if hits:
        return PrivacyDecision(outbound_ok=False, matched_terms=hits, scrubbed_text="")
    return PrivacyDecision(outbound_ok=True, matched_terms=(), scrubbed_text=redact(text))
