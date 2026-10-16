"""Privacy gate: single local redaction pass that EVERY provider call path
must traverse before any OCR text is handed to an external LLM.

Guarantees enforced here:

* **Children's PII never leaves the host.** Any match of a term listed in
  ``config/private_terms.txt`` (whole-word, case-insensitive, both
  ``"First Last"`` and ``"Last, First"`` order) marks the document as
  ``local-only: sensitive`` and short-circuits all provider calls.
* **No SSN, EIN, routing / account / card number or similar ID ever reaches
  a frontier model.** These are redacted in the one pass that produces the
  outbound text, and a defensive check in the classifier re-scans the final
  payload before any HTTP call.
* **Nothing sensitive is ever logged or printed.** The gate returns category
  counts only — never the terms themselves or the matched substrings.

The gate is intentionally conservative: when in doubt it keeps the document
local. A doc with 2+ hits on sensitive topical keywords (SSN / tax / medical
/ school-record), or any residual PII pattern after redaction, is routed to
the local classifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


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

# Topical signals used *after* redaction to detect sensitive documents by
# subject matter — never the raw PII itself. A document with >= 2 hits from
# this set stays local even if every value was successfully scrubbed.
TOPICAL_SIGNALS: Tuple[Tuple[str, str], ...] = (
    # (category, keyword)
    ("school", "report card"),
    ("school", "parent-teacher"),
    ("school", "homeroom"),
    ("school", "school district"),
    ("school", "iep"),
    ("school", "section 504"),
    ("school", "student id"),
    ("school", "transcript"),
    ("school", "elementary"),
    ("school", "middle school"),
    ("school", "high school"),
    ("medical", "pediatric"),
    ("medical", "immunization"),
    ("medical", "well-child"),
    ("medical", "patient"),
    ("medical", "diagnosis"),
    ("medical", "prescription"),
    ("medical", "medical record"),
    ("medical", "icd-10"),
    ("medical", "booster"),
    ("medical", "explanation of benefits"),
    ("ssn", "ssn"),
    ("ssn", "social security"),
    ("ssn", "soc sec"),
    ("tax", "irs"),
    ("tax", "1040"),
    ("tax", "w-2"),
    ("tax", "1099"),
    ("tax", "form 941"),
    ("tax", "tax year"),
    ("tax", "ein"),
    ("tax", "employer identification"),
    ("minor", "minor child"),
    ("minor", "guardian"),
    ("minor", "custody"),
    ("minor", "section 504"),
)

TOPICAL_HIT_THRESHOLD = 2


# -- Term matching ------------------------------------------------------------


def _load_terms_file(path: Path) -> List[str]:
    terms: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms


def _expand_name_variants(term: str) -> List[str]:
    """``"John Smith"`` → ``["John Smith", "Smith, John"]``.

    Three+ word names ("John Q Smith") also produce a ``"Smith, John Q"``
    rendition so people-indexed records are caught in either order.
    """
    cleaned = term.strip()
    if not cleaned:
        return []
    variants = [cleaned]
    parts = cleaned.split()
    if len(parts) == 2 and not any(c in cleaned for c in ",./\\"):
        variants.append(f"{parts[1]}, {parts[0]}")
    elif len(parts) >= 3 and "," not in cleaned:
        variants.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
    return variants


def _unique_terms(collected: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    unique: List[str] = []
    for term in collected:
        if not term:
            continue
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


@dataclass
class SensitiveTerms:
    """Case-insensitive whole-word matcher over user-supplied private terms.

    Internally terms expand to include ``Last, First`` variants and the
    matcher only reports counts — never the matched values — so results are
    safe to log.

    ``terms`` is the full matching list (builtins + user file + extras).
    ``file_terms`` is only what the user supplied (``private_terms.txt`` and
    ``extra_terms``) — used by the outbound prompt guard so generic builtin
    keywords like ``school`` cannot false-positive against the taxonomy listing.
    """

    terms: List[str] = field(default_factory=list)
    file_terms: List[str] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        path: Optional[Path | str] = None,
        extra_terms: Optional[Iterable[str]] = None,
        include_builtins: bool = True,
    ) -> "SensitiveTerms":
        collected: List[str] = []
        user_collected: List[str] = []
        if include_builtins:
            collected.extend(BUILTIN_SENSITIVE_TERMS)
        if path:
            p = Path(path)
            if p.exists():
                loaded = _load_terms_file(p)
                collected.extend(loaded)
                user_collected.extend(loaded)
        if extra_terms:
            extras = [t for t in extra_terms if t]
            collected.extend(extras)
            user_collected.extend(extras)
        return cls(terms=_unique_terms(collected), file_terms=_unique_terms(user_collected))

    def _expanded(self) -> List[str]:
        out: List[str] = []
        for term in self.terms:
            out.extend(_expand_name_variants(term))
        return out

    def _compiled(self) -> List[re.Pattern[str]]:
        patterns: List[re.Pattern[str]] = []
        for variant in self._expanded():
            escaped = re.escape(variant)
            # Honor whole-word boundaries only when the term itself starts
            # and ends with a word character. Terms that end in punctuation
            # (``"Section 504"``) still get boundary-matched on the leading
            # word char.
            if re.match(r"\w", variant):
                head = r"\b"
            else:
                head = ""
            if re.search(r"\w$", variant):
                tail = r"\b"
            else:
                tail = ""
            patterns.append(re.compile(f"{head}{escaped}{tail}", re.IGNORECASE))
        return patterns

    def count_hits(self, text: str) -> int:
        """Return the total number of private-term hits in ``text``.

        Only a count is ever returned — never the matched substring — so
        this value is safe to record in logs and manifests.
        """
        if not text:
            return 0
        total = 0
        for pattern in self._compiled():
            total += len(pattern.findall(text))
        return total

    def contains_sensitive(self, text: str) -> bool:
        """True iff ``text`` contains any private-term hit."""
        return self.count_hits(text) > 0

    def contains_user_term(self, text: str) -> bool:
        """True iff ``text`` contains a *user-supplied* private term.

        Built-in topical keywords are excluded. Matching is case-insensitive
        with whole-word boundaries (including ``Last, First`` name variants).
        """
        if not text or not self.file_terms:
            return False
        return SensitiveTerms(terms=list(self.file_terms), file_terms=list(self.file_terms)).contains_sensitive(text)

    # Legacy name used by older tests: returns matched terms. Kept for
    # backward compatibility only. New code must prefer ``count_hits``
    # since private-term *values* must never be logged.
    def matches(self, text: str) -> List[str]:
        if not text:
            return []
        hits: List[str] = []
        for term, pattern in zip(self.terms, self._compiled()):
            if pattern.search(text):
                hits.append(term)
        return hits

    def redact(self, text: str) -> Tuple[str, int]:
        """Return ``(scrubbed_text, hit_count)``.

        The scrubbed text replaces every variant of every term with the
        ``[REDACTED_TERM]`` placeholder so no private name reaches the
        LLM provider through the outbound prompt.
        """
        if not text:
            return text, 0
        scrubbed = text
        total = 0
        for pattern in self._compiled():
            scrubbed, count = pattern.subn("[REDACTED_TERM]", scrubbed)
            total += count
        return scrubbed, total


# -- Redaction patterns -------------------------------------------------------

_SSN_DASHED_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SSN_SPACED_RE = re.compile(r"\b\d{3}\s\d{2}\s\d{4}\b")

# Unformatted 9-digit SSN near an SSN keyword. Capturing group 1 is the
# keyword run, group ``digits`` is the number.
_SSN_NEAR_KEYWORD_RE = re.compile(
    r"(?i)(?P<label>ssn|social\s*security(?:\s*(?:number|no\.?|#))?|soc\.?\s*sec\.?|"
    r"tax\s*id(?:entification)?(?:\s*number)?|tin|itin)"
    r"(?P<sep>[^\d\n]{0,30})"
    r"(?P<digits>\d{9})\b"
)

# EIN: ##-#######
_EIN_RE = re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])")

# Raw digit runs of 8+ (no separators).
_ACCT_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")

# Grouped digit sequence like "4111 1111 1111 1111" or "123-456-7890".
# Each group must be 3+ digits so that ISO/short date shapes
# (``2026-01-15``, ``1/2/15``) are not consumed by the account rule.
_ACCT_GROUPED_RE = re.compile(
    r"(?<![\w/])(?:\d{3,}(?:[\s-]\d{3,}){1,})(?![\w/])"
)

_DOB_LABELED_RE = re.compile(
    r"(?i)\b(?:DOB|D\.O\.B\.?|date\s+of\s+birth|birthdate|birthday|born(?:\s+on)?)[^\n]{0,20}?"
    r"(?P<date>"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"|(?:\d{4}-\d{2}-\d{2})"
    r"|(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4})"
    r")"
)

# After redaction we re-scan for any digit shape that *could* still be an
# ID. Used both to detect leaks and to drive the ``residual-pii`` sensitive
# verdict. These patterns MUST match a subset of the redaction patterns
# above so a successfully scrubbed document never trips them.
_RESIDUAL_SSN_RE = re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b")
_RESIDUAL_EIN_RE = re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])")
_RESIDUAL_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")
_RESIDUAL_GROUPED_RE = re.compile(
    r"(?<![\w/])(?:\d{3,}(?:[\s-]\d{3,}){1,})(?![\w/])"
)


# -- Luhn helper --------------------------------------------------------------


def _digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _luhn_valid(digits: str) -> bool:
    """Return True when ``digits`` passes the Luhn checksum.

    ``digits`` must be a pure-digit string. Lengths outside the 13..19
    range (the full set of PANs we care about) always return ``False``.
    """
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# -- Core redaction -----------------------------------------------------------


@dataclass
class RedactionReport:
    """Everything a caller needs to know about one redaction pass.

    ``scrubbed_text`` is the outbound-safe text. Every field here is a
    scalar or a count — nothing in this report may ever be a matched
    substring or private term.
    """

    scrubbed_text: str
    category_counts: Dict[str, int] = field(default_factory=dict)
    sensitive: bool = False
    sensitive_reason: str = ""  # "" | "private-term-match" | "residual-pii" | "topic-density"

    def summary_for_manifest(self) -> Dict[str, object]:
        return {
            "redaction_counts": dict(self.category_counts),
            "sensitive": self.sensitive,
            "sensitive_reason": self.sensitive_reason,
        }


def _redact_pii(text: str) -> Tuple[str, Dict[str, int]]:
    """Apply SSN / EIN / account / card / DOB redaction to ``text``.

    Returns ``(scrubbed_text, counts)``. ``counts`` keys: ``ssn``, ``ein``,
    ``acct``, ``card``, ``dob``.
    """
    counts: Dict[str, int] = {"ssn": 0, "ein": 0, "acct": 0, "card": 0, "dob": 0}
    if not text:
        return text, counts

    out = text

    out, n = _SSN_DASHED_RE.subn("[REDACTED_SSN]", out)
    counts["ssn"] += n

    out, n = _SSN_SPACED_RE.subn("[REDACTED_SSN]", out)
    counts["ssn"] += n

    def _ssn_near(match: re.Match[str]) -> str:
        counts["ssn"] += 1
        return f"{match.group('label')}{match.group('sep')}[REDACTED_SSN]"

    out = _SSN_NEAR_KEYWORD_RE.sub(_ssn_near, out)

    out, n = _EIN_RE.subn("[REDACTED_EIN]", out)
    counts["ein"] += n

    def _dob_sub(match: re.Match[str]) -> str:
        counts["dob"] += 1
        head = match.group(0)[: match.start("date") - match.start()]
        return f"{head}[REDACTED_DOB]"

    out = _DOB_LABELED_RE.sub(_dob_sub, out)

    def _grouped_sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = _digits_only(raw)
        if len(digits) < 8:
            return raw
        if _luhn_valid(digits):
            counts["card"] += 1
            return "[REDACTED_CARD]"
        counts["acct"] += 1
        return "[REDACTED_ACCT]"

    out = _ACCT_GROUPED_RE.sub(_grouped_sub, out)

    def _long_sub(match: re.Match[str]) -> str:
        digits = match.group(0)
        if _luhn_valid(digits):
            counts["card"] += 1
            return "[REDACTED_CARD]"
        counts["acct"] += 1
        return "[REDACTED_ACCT]"

    out = _ACCT_LONG_DIGITS_RE.sub(_long_sub, out)
    return out, counts


def _residual_pii_count(scrubbed: str) -> int:
    """Count any PII-shaped artifact that survived redaction.

    This is the belt-and-suspenders check used to decide whether a document
    is safe to send to an external LLM. A healthy redaction pass should
    return zero here.
    """
    if not scrubbed:
        return 0
    return (
        len(_RESIDUAL_SSN_RE.findall(scrubbed))
        + len(_RESIDUAL_EIN_RE.findall(scrubbed))
        + len(_RESIDUAL_LONG_DIGITS_RE.findall(scrubbed))
        + len(_RESIDUAL_GROUPED_RE.findall(scrubbed))
    )


def _topic_hits(scrubbed: str) -> int:
    """Count hits against the topical sensitive-signal keyword list.

    Case-insensitive substring search is sufficient here — these words are
    deliberately generic topical cues, not identifiers.
    """
    if not scrubbed:
        return 0
    lowered = scrubbed.lower()
    hits = 0
    for _cat, keyword in TOPICAL_SIGNALS:
        if keyword in lowered:
            hits += 1
    return hits


def redact(text: str) -> str:
    """Backwards-compatible helper: apply PII redaction and return the text.

    New callers should prefer :func:`gate` so the privacy verdict and
    category counts come back together.
    """
    scrubbed, _ = _redact_pii(text)
    return scrubbed


def gate(text: str, terms: SensitiveTerms) -> RedactionReport:
    """Run the single local redaction gate used by every provider path.

    This is the only function in the project allowed to produce the text
    passed to an LLM. The decision tree is:

    1. Redact private terms (names/schools from ``private_terms.txt`` plus
       built-ins). A non-zero hit count means ``sensitive`` immediately —
       no amount of further scrubbing will unblock an outbound call.
    2. Redact SSN / EIN / account / card / DOB in a single ordered pass.
    3. Re-scan the scrubbed output for residual PII shapes. Anything that
       slips through flips ``sensitive`` to true.
    4. Count topical sensitive-subject keywords. ``>= TOPICAL_HIT_THRESHOLD``
       flips ``sensitive`` to true even when all values were scrubbed.
    """
    counts: Dict[str, int] = {"private_term": 0, "ssn": 0, "ein": 0,
                              "acct": 0, "card": 0, "dob": 0,
                              "residual_pii": 0, "topic": 0}
    if not text:
        return RedactionReport(scrubbed_text=text or "", category_counts=counts)

    scrubbed, private_hits = terms.redact(text)
    counts["private_term"] = private_hits

    scrubbed, pii_counts = _redact_pii(scrubbed)
    for k, v in pii_counts.items():
        counts[k] = v

    residual = _residual_pii_count(scrubbed)
    counts["residual_pii"] = residual
    topic_hits = _topic_hits(scrubbed)
    counts["topic"] = topic_hits

    sensitive = False
    reason = ""
    if private_hits > 0:
        sensitive = True
        reason = "private-term-match"
    elif residual > 0:
        sensitive = True
        reason = "residual-pii"
    elif topic_hits >= TOPICAL_HIT_THRESHOLD:
        sensitive = True
        reason = "topic-density"

    return RedactionReport(
        scrubbed_text=scrubbed,
        category_counts=counts,
        sensitive=sensitive,
        sensitive_reason=reason,
    )


def sanitize_outbound_label(value: str, terms: Optional[SensitiveTerms] = None) -> str:
    """Redact a filename or path the same way document text is redacted.

    Private terms and number-shaped PII are scrubbed. Returns the scrubbed
    value, or ``""`` (drop from the prompt) when a private term matched or
    residual number PII remains after redaction.
    """
    if not value:
        return ""
    if terms is not None:
        report = gate(value, terms)
        if report.category_counts.get("private_term", 0) > 0:
            return ""
        if _residual_pii_count(report.scrubbed_text) > 0:
            return ""
        return report.scrubbed_text
    scrubbed, _ = _redact_pii(value)
    if _residual_pii_count(scrubbed) > 0:
        return ""
    return scrubbed


def _default_private_term_paths() -> Tuple[Path, ...]:
    here = Path(__file__).resolve().parent.parent
    return (
        here / "config" / "private_terms.txt",
        here / "private_terms.txt",
        Path("config/private_terms.txt"),
        Path("private_terms.txt"),
    )


def _load_user_terms_for_outbound() -> SensitiveTerms:
    for p in _default_private_term_paths():
        try:
            if p.exists():
                return SensitiveTerms.load(path=p, include_builtins=False)
        except OSError:
            continue
    return SensitiveTerms.load(path=None, include_builtins=False)


def assert_safe_for_outbound(text: str, terms: Optional[SensitiveTerms] = None) -> None:
    """Raise ``ValueError`` if ``text`` still contains a PII shape or private term.

    Used as a defensive gate inside each provider call so that any future
    code path that forgets to run :func:`gate` still fails loudly rather
    than silently exfiltrating data.

    Checks:

    * Residual number PII (SSN / EIN / account / card shapes).
    * User-supplied private terms from ``config/private_terms.txt`` (or
      ``terms.file_terms`` when a matcher is passed), case-insensitive with
      word boundaries.
    """
    if _residual_pii_count(text) > 0:
        raise ValueError(
            "refusing to send outbound text: residual PII pattern detected"
        )
    checker = terms if terms is not None else _load_user_terms_for_outbound()
    if checker.contains_user_term(text):
        raise ValueError(
            "refusing to send outbound text: private term detected"
        )


# -- Legacy adapter kept for callers that still use ``evaluate`` --------------


@dataclass
class PrivacyDecision:
    outbound_ok: bool
    matched_terms: Sequence[str]
    scrubbed_text: str
    report: Optional[RedactionReport] = None


def evaluate(text: str, terms: SensitiveTerms) -> PrivacyDecision:
    """Compat shim around :func:`gate`.

    ``matched_terms`` here is a count-only sentinel list so downstream
    callers can detect that a match happened without ever receiving the
    actual private strings.
    """
    report = gate(text, terms)
    outbound_ok = not report.sensitive
    sentinel: Sequence[str] = (
        ["<redacted>"] * report.category_counts.get("private_term", 0)
    )
    return PrivacyDecision(
        outbound_ok=outbound_ok,
        matched_terms=sentinel,
        scrubbed_text=report.scrubbed_text if outbound_ok else "",
        report=report,
    )
