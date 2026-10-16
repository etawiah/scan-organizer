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

import os
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


def _given_name_tokens(term: str) -> List[str]:
    """Lone given-name token from a user private term.

    ``"Zebulon Quill"`` → ``["Zebulon"]``. ``"Quill, Zebulon"`` →
    ``["Zebulon"]``. A single-token term is already matched as itself.
    Built-in topical phrases must NOT go through this helper.
    """
    cleaned = term.strip()
    if not cleaned or any(c in cleaned for c in "/\\"):
        return []
    if "," in cleaned:
        bits = [p.strip() for p in cleaned.split(",") if p.strip()]
        if len(bits) >= 2:
            first = bits[1].split()[0] if bits[1].split() else ""
            return [first] if first else []
        return []
    parts = cleaned.split()
    if len(parts) >= 2:
        return [parts[0]]
    return []


def _terms_from_env() -> List[str]:
    """Load user private terms from ``PRIVATE_TERMS`` / ``SCAN_PRIVATE_TERMS``.

    Comma or newline separated. Never log the values. The repo is public,
    so this is the other supported way to supply names without committing
    them (alongside the gitignored ``private_terms.txt``).
    """
    raw = os.environ.get("PRIVATE_TERMS") or os.environ.get("SCAN_PRIVATE_TERMS") or ""
    if not raw.strip():
        return []
    items: List[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        token = chunk.strip()
        if token and not token.startswith("#"):
            items.append(token)
    return items


def _compile_variant(variant: str, *, filename_safe: bool) -> re.Pattern[str]:
    escaped = re.escape(variant)
    if filename_safe:
        # Underscore and hyphen are filename separators, so ``ZEBULON_scan.pdf``
        # and ``zebulon-notes`` must match a lone given name.
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    if re.match(r"\w", variant):
        head = r"\b"
    else:
        head = ""
    if re.search(r"\w$", variant):
        tail = r"\b"
    else:
        tail = ""
    return re.compile(f"{head}{escaped}{tail}", re.IGNORECASE)


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

    Internally terms expand to include ``Last, First`` variants and, for
    user-supplied names, the lone given-name token. Matching is
    case-insensitive (including ALL CAPS) and treats ``_`` / ``-`` as
    boundaries so filenames and paths match. The matcher only reports
    counts — never the matched values — so results are safe to log.

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
        include_env: bool = True,
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
        if include_env:
            env_terms = _terms_from_env()
            collected.extend(env_terms)
            user_collected.extend(env_terms)
        return cls(terms=_unique_terms(collected), file_terms=_unique_terms(user_collected))

    def _expanded(self) -> List[str]:
        out: List[str] = []
        for term in self.terms:
            out.extend(_expand_name_variants(term))
        return out

    def _compiled(self) -> List[re.Pattern[str]]:
        file_keys = {t.lower() for t in self.file_terms}
        patterns: List[re.Pattern[str]] = []
        seen: Set[str] = set()
        for term in self.terms:
            is_user = term.lower() in file_keys
            variants = list(_expand_name_variants(term))
            if is_user:
                variants.extend(_given_name_tokens(term))
            for variant in variants:
                key = (variant.lower(), is_user)
                if key in seen:
                    continue
                seen.add(key)
                patterns.append(_compile_variant(variant, filename_safe=is_user))
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
        (including ALL CAPS) with filename-safe boundaries, ``Last, First``
        variants, and lone given-name tokens from multi-word terms.
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


# -- Card / account fragments, US addresses, person-name heuristic ------------
#
# These catch the shapes that slipped a dry-run outbound gate: a third
# party's full name plus a 4-digit card fragment, and person names plus a
# US street address. Placeholders are typed: [CARD], [ADDRESS], [NAME].

_CARD_FRAG_RE = re.compile(
    r"""(?ix)
    (?:
        (?:(?:the\s+)?(?:card|account|acct)\s+)?ending(?:\s+in)?\s+\d{4}
        |
        last\s*4\s*:?\s*\d{4}
        |
        (?:acct|account)(?:\s+(?:number|no\.?))?\s*\#\s*\d{4}
        |
        (?:acct|account)\s+\#?\d{4}
        |
        (?:x{2,}|\*{2,}|\.{3,}|[xX]-)\d{4}
    )
    \b
    """
)

_STREET_SUFFIX_RE = (
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|"
    r"Dr|Drive|Ct|Court|Way|Pl|Place|Ter|Terrace|Cir|Circle|"
    r"Hwy|Highway|(?:US\s+)?(?:Route|Rte))\b\.?"
)

_SUITE_RE = r"(?:Suite|Ste|Apt|Apartment|Unit|\#|PMB)\.?\s*[A-Za-z0-9-]+"

_US_STATE_ABBR = (
    r"AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|"
    r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|PR|RI|SC|SD|TN|"
    r"TX|UT|VA|VT|WA|WI|WV|WY"
)

# House number + street name + suffix, optional suite, optional city/state/ZIP.
_ADDRESS_RE = re.compile(
    rf"""
    \b
    \d{{1,6}}
    [\s_\-]+
    (?:[A-Z][A-Za-z0-9.']+[\s_\-]+){{1,5}}
    {_STREET_SUFFIX_RE}
    (?:[\s,]+{_SUITE_RE})?
    (?:
        [\s,]+
        [A-Z][A-Za-z .'-]+
        [\s,]+
        (?:{_US_STATE_ABBR})
        (?:[\s,]+(?:\d{{5}}(?:-\d{{4}})?))?
    )?
    """,
    re.VERBOSE,
)

# Standalone 5-digit ZIP or ZIP+4 sitting next to a state abbreviation.
_STATE_ZIP_RE = re.compile(
    rf"\b(?:{_US_STATE_ABBR})\s+\d{{5}}(?:-\d{{4}})?\b"
)

_HONORIFIC_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr)\.?"
    r"[\s_\-]+"
    r"[A-Z][a-z]+(?:[\s_\-]+[A-Z][a-z]+){0,2}\b"
)

_LAST_FIRST_RE = re.compile(
    r"\b[A-Z][a-z]+,\s+[A-Z][a-z]+(?:[\s_\-]+[A-Z][a-z]+)?\b"
)

_TITLE_TOKEN_RE = re.compile(r"[A-Z][a-z]+")

# Tokens that almost never start or form a person name on a household scan.
# Category / org / document words stay visible so the classifier still has
# signal after the name heuristic runs.
_NAME_ALLOWLIST = frozenset({
    # months / days
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs",
    "fri", "sat", "sun",
    # US states and common geographic words
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "hampshire", "jersey", "mexico", "york", "carolina", "dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode", "island",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "wisconsin", "wyoming", "columbia", "district",
    "new", "north", "south", "east", "west", "united", "states",
    "america", "american",
    # taxonomy / org / document words
    "personal", "family", "household", "business", "campaign", "spouse",
    "kids", "mother", "home", "vehicles",
    "bank", "insurance", "statement", "statements", "department",
    "internal", "revenue", "service", "services",
    "invoice", "invoices", "receipt", "receipts", "account", "accounts",
    "payable", "receivable", "agreement", "contract", "contracts",
    "license", "licenses", "policy", "premium", "coverage",
    "medical", "record", "records", "patient", "provider", "diagnosis",
    "prescription", "explanation", "benefits", "driver", "passport",
    "social", "security", "marriage", "certificate", "notary",
    "electric", "company", "companies", "utility", "utilities", "bill",
    "bills", "monthly", "quarterly", "annual", "vehicle", "registration",
    "renewal", "tax", "taxes", "return", "form", "year", "filing",
    "status", "total", "income", "refund", "due", "amount", "paid",
    "balance", "payment", "payments", "deposit", "withdrawal",
    "checking", "savings", "mortgage", "rent", "lease", "tenant",
    "tenants", "landlord", "property", "warranty", "warranties",
    "repair", "repairs", "estimate", "contractor", "plumber",
    "finance", "disclosure", "report", "reports", "contribution",
    "donation", "donations", "expense", "expenses", "corporate",
    "articles", "organization", "operating", "master", "scope",
    "platform", "integration", "deliverables", "complex", "tech",
    "first", "correspondence", "letter", "dear", "sincerely",
    "regards", "whom", "concern", "city", "state", "county", "federal",
    "treasury", "government", "office", "bureau", "agency",
    "administration", "commission", "board", "school", "elementary",
    "middle", "high", "college", "university", "hospital", "clinic",
    "health", "care", "pharmacy", "laboratory", "imaging", "dental",
    "vision", "wellness", "child", "children", "pediatric", "parent",
    "teacher", "homeroom", "transcript", "immunization", "card",
    "cards", "number", "numbers", "date", "dates", "period", "billing",
    "address", "street", "avenue", "road", "boulevard", "lane",
    "drive", "court", "route", "highway", "place", "terrace", "circle",
    "suite", "apartment", "unit", "legal", "id", "identification",
    "out", "terms", "conditions", "apply", "privacy", "please",
    "verify", "member", "applicant", "reference", "shipment",
    "arrived", "today", "thank", "you", "original", "filename",
    "document", "documents", "text", "sensitive", "values", "already",
    "redacted", "allowed", "taxonomy", "nodes", "owner", "area",
    "empty", "string", "segment", "return", "only", "json", "object",
    "these", "keys", "nothing", "else", "must", "pick", "exactly",
    "one", "scan", "scanned", "classifying", "household", "strict",
    "confidence", "unknown", "descriptive", "lowercase", "words",
    "underscores", "extension", "cannot", "confidently", "place",
    "rather", "guessing", "caller", "will", "route", "low", "human",
    "review", "page", "pages", "attached", "schedule", "updated",
    "used", "charged", "debited", "overdue", "issued", "dated",
    "consulting", "contractor", "vendor", "payroll", "wire",
    "routing", "on", "file", "for", "the", "and", "this", "that",
    "from", "with", "your", "our", "any", "all", "each", "every",
    "llc", "inc", "ltd", "corp", "corporation", "co",
    "acme", "sample", "placeholder", "fictional",
    "utilities", "registration", "activities", "compliance",
    "filings", "donations", "invoices",
    "in", "of", "or", "to", "by", "as", "at", "an", "a",
    "compliance", "legal", "loan", "payroll", "corporate",
    "activities", "warranties", "property",
})

_ORG_SUFFIXES = frozenset({
    "school", "elementary", "middle", "hospital", "company", "companies",
    "service", "services", "department", "agreement", "statement",
    "statements", "llc", "inc", "ltd", "corp", "corporation", "co",
    "bank", "insurance", "district", "university", "college", "clinic",
    "agency", "bureau", "office", "commission", "board", "association",
    "foundation", "trust", "group", "partners", "holdings",
    "technologies", "solutions", "systems", "bill", "invoice",
    "invoices", "receipt", "receipts", "form", "return", "filing",
    "report", "reports", "records", "record", "policy", "license",
    "licenses", "registration", "renewal", "utilities", "utility",
    "electric", "campaign", "committee", "treasury", "revenue",
    "administration", "hospital",
})

_STREET_SUFFIX_WORDS = frozenset({
    "st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard",
    "ln", "lane", "dr", "drive", "ct", "court", "way", "pl", "place",
    "ter", "terrace", "cir", "circle", "hwy", "highway", "route", "rte",
})

_MONTH_DAY_WORDS = frozenset({
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday",
})


def _is_name_tokens(tokens: Sequence[str]) -> bool:
    """True when a 2–3 Title-Case run looks like a person name."""
    if not tokens or not (2 <= len(tokens) <= 3):
        return False
    lowered = [t.lower() for t in tokens]
    if any(t in _STREET_SUFFIX_WORDS for t in lowered):
        return False
    if lowered[-1] in _ORG_SUFFIXES:
        return False
    if all(t in _NAME_ALLOWLIST for t in lowered):
        return False
    if lowered[0] in _MONTH_DAY_WORDS:
        return False
    return True


def _adjacent_title_runs(text: str) -> List[List[Tuple[int, int, str]]]:
    """Group adjacent Title-Case tokens (space / _ / - / 'and')."""
    tokens = [(m.start(), m.end(), m.group(0)) for m in _TITLE_TOKEN_RE.finditer(text)]
    if not tokens:
        return []
    runs: List[List[Tuple[int, int, str]]] = []
    current = [tokens[0]]
    for prev, tok in zip(tokens, tokens[1:]):
        between = text[prev[1]:tok[0]]
        # Horizontal separators only — a newline (taxonomy listings,
        # ``- Item`` bullets) must start a new run.
        if re.fullmatch(r"[ \t_\-]+", between) or re.fullmatch(r"[ \t]+and[ \t]+", between):
            current.append(tok)
        else:
            runs.append(current)
            current = [tok]
    runs.append(current)
    return runs


def _name_spans(text: str) -> List[Tuple[int, int]]:
    """Return ``(start, end)`` spans that the name heuristic should scrub."""
    if not text:
        return []
    spans: List[Tuple[int, int]] = []
    for match in _HONORIFIC_RE.finditer(text):
        spans.append(match.span())
    for match in _LAST_FIRST_RE.finditer(text):
        last, first = match.group(0).split(",", 1)
        last_tok = last.strip()
        first_toks = [p for p in re.split(r"[\s_\-]+", first.strip()) if p]
        if _is_name_tokens([last_tok, *first_toks][:3]):
            spans.append(match.span())
    for run in _adjacent_title_runs(text):
        words = [tok for _s, _e, tok in run]
        # Prefer the longest window that looks like a name.
        replaced = [False] * len(run)
        for window in (3, 2):
            if len(run) < window:
                continue
            i = 0
            while i <= len(run) - window:
                if any(replaced[i:i + window]):
                    i += 1
                    continue
                if _is_name_tokens(words[i:i + window]):
                    spans.append((run[i][0], run[i + window - 1][1]))
                    for j in range(i, i + window):
                        replaced[j] = True
                    i += window
                else:
                    i += 1
    # Merge overlapping / adjacent spans so "Zebulon and Marta Quill"
    # becomes a single [NAME] even if windows overlapped.
    if not spans:
        return []
    spans.sort()
    merged: List[Tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        prev_s, prev_e = merged[-1]
        gap = text[prev_e:start]
        if start <= prev_e or re.fullmatch(r"[ \t_\-]+", gap) or re.fullmatch(r"[ \t]+and[ \t]+", gap):
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def _find_card_fragments(text: str) -> List[str]:
    if not text:
        return []
    return _CARD_FRAG_RE.findall(text)


def _find_addresses(text: str) -> List[str]:
    if not text:
        return []
    found = _ADDRESS_RE.findall(text)
    found.extend(_STATE_ZIP_RE.findall(text))
    return found


def _find_names(text: str) -> List[str]:
    return [text[s:e] for s, e in _name_spans(text)]


def _redact_addresses(text: str) -> Tuple[str, int]:
    """Replace US street / state+ZIP shapes. Run *before* grouped-account."""
    if not text:
        return text, 0
    count = 0

    def _addr_sub(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[ADDRESS]"

    out = _ADDRESS_RE.sub(_addr_sub, text)
    out = _STATE_ZIP_RE.sub(_addr_sub, out)
    return out, count


def _redact_card_fragments(text: str) -> Tuple[str, int]:
    """Replace 4-digit card/account fragments. Run *after* full PAN redaction."""
    if not text:
        return text, 0
    out, n = _CARD_FRAG_RE.subn("[CARD]", text)
    return out, n


def _redact_names(text: str) -> Tuple[str, int]:
    """Replace Title-Case person-name runs."""
    if not text:
        return text, 0
    spans = _name_spans(text)
    if not spans:
        return text, 0
    pieces: List[str] = []
    cursor = 0
    count = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        pieces.append("[NAME]")
        count += 1
        cursor = end
    pieces.append(text[cursor:])
    out = "".join(pieces)
    # Conjunction leftovers: "Zebulon and [NAME]" / "[NAME] and Marta".
    out, n = re.subn(r"\b[A-Z][a-z]+\s+and\s+\[NAME\]", "[NAME]", out)
    count += n
    out, n = re.subn(r"\[NAME\]\s+and\s+[A-Z][a-z]+\b", "[NAME]", out)
    count += n
    return out, count


def _redact_identity(text: str, *, addresses: bool = True, cards: bool = True, names: bool = True) -> Tuple[str, Dict[str, int]]:
    """Scrub card fragments, US addresses, and Title-Case person names.

    Returns ``(scrubbed_text, counts)`` with keys ``card``, ``address``,
    ``name``. Applied to OCR text, filenames, and paths.
    """
    counts: Dict[str, int] = {"card": 0, "address": 0, "name": 0}
    if not text:
        return text, counts
    out = text
    if addresses:
        out, n = _redact_addresses(out)
        counts["address"] += n
    if cards:
        out, n = _redact_card_fragments(out)
        counts["card"] += n
    if names:
        out, n = _redact_names(out)
        counts["name"] += n
    return out, counts


def _identity_leak_count(text: str) -> int:
    """Count name / address / card-fragment shapes still present in ``text``."""
    if not text:
        return 0
    return (
        len(_find_card_fragments(text))
        + len(_find_addresses(text))
        + len(_find_names(text))
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
    return zero here. Includes number-shaped IDs plus the name / address /
    card-fragment shapes that must never appear in an outbound prompt.
    """
    if not scrubbed:
        return 0
    return (
        len(_RESIDUAL_SSN_RE.findall(scrubbed))
        + len(_RESIDUAL_EIN_RE.findall(scrubbed))
        + len(_RESIDUAL_LONG_DIGITS_RE.findall(scrubbed))
        + len(_RESIDUAL_GROUPED_RE.findall(scrubbed))
        + _identity_leak_count(scrubbed)
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
    category counts come back together. Applied to OCR text, filenames,
    and paths: number-shaped IDs plus card fragments, US addresses, and
    Title-Case person-name runs.
    """
    # Addresses (including state+ZIP / ZIP+4) must run before the
    # grouped-account rule so ``NJ 00000-0000`` is not eaten as an acct.
    # Full PANs run next so ``ending in 4111 1111 1111 1111`` is a card,
    # then leftover 4-digit fragments and person names.
    scrubbed, _ = _redact_identity(text, cards=False, names=False)
    scrubbed, _ = _redact_pii(scrubbed)
    scrubbed, _ = _redact_identity(scrubbed, addresses=False)
    return scrubbed


def gate(text: str, terms: SensitiveTerms) -> RedactionReport:
    """Run the single local redaction gate used by every provider path.

    This is the only function in the project allowed to produce the text
    passed to an LLM. The decision tree is:

    1. Redact private terms (names/schools from ``private_terms.txt`` plus
       built-ins). A non-zero hit count means ``sensitive`` immediately —
       no amount of further scrubbing will unblock an outbound call.
    2. Redact SSN / EIN / account / card / DOB in a single ordered pass.
    3. Redact card/account 4-digit fragments, US street addresses, and
       Title-Case person-name runs (filenames and paths use this same pass).
    4. Re-scan the scrubbed output for residual PII shapes. Anything that
       slips through flips ``sensitive`` to true.
    5. Count topical sensitive-subject keywords. ``>= TOPICAL_HIT_THRESHOLD``
       flips ``sensitive`` to true even when all values were scrubbed.
    """
    counts: Dict[str, int] = {"private_term": 0, "ssn": 0, "ein": 0,
                              "acct": 0, "card": 0, "dob": 0,
                              "address": 0, "name": 0,
                              "residual_pii": 0, "topic": 0}
    if not text:
        return RedactionReport(scrubbed_text=text or "", category_counts=counts)

    scrubbed, private_hits = terms.redact(text)
    counts["private_term"] = private_hits

    # Addresses / ZIP+4 before grouped-account so state+ZIP is not an acct.
    # Full PANs next; leftover 4-digit fragments and names after that.
    scrubbed, identity_first = _redact_identity(scrubbed, cards=False, names=False)
    scrubbed, pii_counts = _redact_pii(scrubbed)
    for k, v in pii_counts.items():
        counts[k] = v
    scrubbed, identity_counts = _redact_identity(scrubbed, addresses=False)
    counts["card"] += identity_first.get("card", 0) + identity_counts.get("card", 0)
    counts["address"] = identity_first.get("address", 0) + identity_counts.get("address", 0)
    counts["name"] = identity_first.get("name", 0) + identity_counts.get("name", 0)

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
    scrubbed = redact(value)
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
    * Card/account 4-digit fragments, US street addresses, and Title-Case
      person-name runs.
    * User-supplied private terms from ``config/private_terms.txt``,
      ``PRIVATE_TERMS`` / ``SCAN_PRIVATE_TERMS``, or ``terms.file_terms``
      when a matcher is passed. Case-insensitive including ALL CAPS;
      lone given names match on their own; ``_`` / ``-`` are boundaries.
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
