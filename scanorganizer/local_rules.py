"""Local keyword classifier.

Used as:
  * the fallback when an external provider errors out
  * the mandatory classifier when the privacy filter blocks outbound calls
  * the explicit ``--provider local-keyword`` mode

Keyword rules are scored and the highest-scoring taxonomy node wins. The
returned confidence is deliberately conservative (<= 0.7) so that routine
reviews still land in ``_Review/`` unless a rule clearly matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .taxonomy import Taxonomy


@dataclass
class Rule:
    owner: str
    area: str
    doc_type: str
    patterns: Tuple[str, ...]
    short_name_hint: str
    confidence: float = 0.6


_RULES: Tuple[Rule, ...] = (
    # ---- Family / Kids ----
    Rule("Family", "Kids", "School", (
        "report card", "parent-teacher", "homeroom", "pta",
        "school district", "iep", "section 504", "field trip",
    ), "school_doc", 0.65),
    Rule("Family", "Kids", "Medical", (
        "pediatric", "immunization", "well-child", "vaccine",
        "children's hospital", "booster",
    ), "pediatric_record", 0.65),
    Rule("Family", "Kids", "Activities", (
        "little league", "scouts", "soccer club", "dance recital",
        "camp registration", "youth league",
    ), "kids_activity", 0.6),
    Rule("Family", "Kids", "ID", (
        "birth certificate", "minor passport", "custody",
    ), "kids_id", 0.65),

    # ---- Family / Spouse / Mother ----
    Rule("Family", "Spouse", "Medical", (
        "spouse", "husband", "wife", "partner",
    ), "spouse_medical", 0.55),
    Rule("Family", "Mother", "Medical", (
        "mother", "mom ", "elder care", "assisted living",
    ), "mother_medical", 0.55),

    # ---- Personal ----
    Rule("Personal", "", "Taxes", (
        "irs", "1040", "w-2", "w2", "1099", "schedule c",
        "tax return", "tax year",
    ), "personal_tax", 0.7),
    Rule("Personal", "", "Finance-Bank", (
        "checking account", "savings account", "bank statement",
        "deposit", "withdrawal", "monthly statement",
    ), "personal_bank", 0.65),
    Rule("Personal", "", "Medical", (
        "patient", "provider", "diagnosis", "prescription",
        "explanation of benefits", "eob",
    ), "personal_medical", 0.6),
    Rule("Personal", "", "Insurance", (
        "policy number", "insured", "premium", "coverage period",
        "deductible",
    ), "personal_insurance", 0.6),
    Rule("Personal", "", "ID-Legal", (
        "driver license", "driver's license", "passport",
        "social security", "marriage certificate", "notary",
    ), "personal_id", 0.65),
    Rule("Personal", "", "Correspondence", (
        "dear ", "sincerely", "regards", "to whom it may concern",
    ), "letter", 0.5),
    Rule("Personal", "", "Receipts", (
        "receipt", "amount paid", "total paid", "thank you for your purchase",
    ), "receipt", 0.55),

    # ---- Household ----
    Rule("Household", "Home", "Utilities", (
        "electric bill", "gas bill", "water bill", "sewer",
        "power company", "kwh",
    ), "home_utility", 0.65),
    Rule("Household", "Home", "Mortgage-Rent", (
        "mortgage", "escrow", "rent due", "lease agreement",
    ), "home_mortgage", 0.6),
    Rule("Household", "Home", "Repairs", (
        "repair", "contractor estimate", "plumber", "hvac",
    ), "home_repair", 0.55),
    Rule("Household", "Home", "Warranties", (
        "warranty", "warrantee", "limited warranty",
    ), "warranty", 0.55),
    Rule("Household", "Vehicles", "Registration", (
        "vehicle registration", "dmv", "vin ",
    ), "vehicle_reg", 0.65),
    Rule("Household", "Vehicles", "Insurance", (
        "auto insurance", "car insurance", "collision", "liability coverage",
    ), "auto_insurance", 0.6),
    Rule("Household", "Vehicles", "Service", (
        "oil change", "tire rotation", "service invoice", "mileage",
    ), "vehicle_service", 0.55),
    Rule("Household", "Vehicles", "Loan", (
        "auto loan", "car loan", "vehicle loan",
    ), "vehicle_loan", 0.6),

    # ---- Business / ComplexTech ----
    Rule("Business", "ComplexTech", "Invoices-Out", (
        "complex tech", "complextech",
    ), "complextech_invoice", 0.55),
    Rule("Business", "ComplexTech", "Bills-In", (
        "vendor bill", "accounts payable",
    ), "complextech_bill", 0.5),
    Rule("Business", "ComplexTech", "Contracts", (
        "master service agreement", "msa ", "statement of work", "sow ",
    ), "complextech_contract", 0.55),
    Rule("Business", "ComplexTech", "Tax", (
        "ein", "employer identification", "form 941",
    ), "complextech_tax", 0.55),
    Rule("Business", "ComplexTech", "Payroll", (
        "payroll", "pay stub", "paycheck",
    ), "payroll", 0.6),
    Rule("Business", "ComplexTech", "Bank", (
        "business checking", "bank statement",
    ), "biz_bank", 0.5),
    Rule("Business", "ComplexTech", "Licenses-Insurance", (
        "business license", "general liability",
    ), "biz_license", 0.55),

    # ---- Business / ElJetSit ----
    Rule("Business", "ElJetSit", "Invoices", (
        "eljetsit", "el jet sit",
    ), "eljetsit_invoice", 0.55),
    Rule("Business", "ElJetSit", "Bills", (
        "vendor bill",
    ), "eljetsit_bill", 0.45),
    Rule("Business", "ElJetSit", "Corporate", (
        "articles of organization", "operating agreement", "corporate filing",
    ), "eljetsit_corp", 0.55),

    # ---- Business / 432SouthStVT ----
    Rule("Business", "432SouthStVT", "Lease-Tenants", (
        "432 south", "south street", "tenant lease",
    ), "south_lease", 0.55),
    Rule("Business", "432SouthStVT", "Mortgage-Bank", (
        "property mortgage",
    ), "south_mortgage", 0.5),
    Rule("Business", "432SouthStVT", "PropertyTax", (
        "property tax", "assessor",
    ), "property_tax", 0.6),
    Rule("Business", "432SouthStVT", "Insurance", (
        "property insurance", "landlord insurance",
    ), "property_insurance", 0.55),
    Rule("Business", "432SouthStVT", "Repairs-Receipts", (
        "contractor", "repair invoice",
    ), "property_repair", 0.5),
    Rule("Business", "432SouthStVT", "Utilities", (
        "water department", "electric utility",
    ), "property_utility", 0.5),

    # ---- Campaign / BRKidsFirst ----
    Rule("Campaign", "BRKidsFirst", "Donations-Receipts", (
        "donation", "contribution receipt", "501(c)",
    ), "campaign_donation", 0.6),
    Rule("Campaign", "BRKidsFirst", "Expenses", (
        "campaign expense", "reimbursement",
    ), "campaign_expense", 0.55),
    Rule("Campaign", "BRKidsFirst", "Filings-Compliance", (
        "campaign finance", "fec", "disclosure report",
    ), "campaign_filing", 0.6),
    Rule("Campaign", "BRKidsFirst", "Correspondence", (
        "br kids first", "brkidsfirst",
    ), "campaign_letter", 0.55),
)


def _slug(name: str) -> str:
    name = re.sub(r"[^\w]+", "_", name.lower()).strip("_")
    return name or "document"


@dataclass
class LocalClassification:
    owner: str
    area: str
    doc_type: str
    short_name: str
    confidence: float
    matched_terms: Tuple[str, ...]


def classify(text: str, taxonomy: Taxonomy, original_filename: str = "") -> Dict[str, object]:
    """Return a dict matching the AI JSON shape.

    If no rule matches we return a low-confidence ``Personal/Correspondence``
    assignment so the caller still routes to ``_Review/`` via the threshold.
    """
    haystack = f"{text}\n{original_filename}".lower()

    scores: List[Tuple[Rule, int, List[str]]] = []
    for rule in _RULES:
        if not taxonomy.is_valid_node(rule.owner, rule.area, rule.doc_type):
            continue
        hits: List[str] = []
        for pat in rule.patterns:
            if pat.lower() in haystack:
                hits.append(pat)
        if hits:
            scores.append((rule, len(hits), hits))

    if not scores:
        # Fallback: well below threshold so it lands in _Review/.
        hint = _slug(_stem(original_filename)) or "document"
        return {
            "owner": "Personal",
            "area": "",
            "doc_type": "Correspondence",
            "doc_date": "",
            "confidence": 0.2,
            "short_name": hint,
            "matched_terms": [],
        }

    scores.sort(key=lambda s: (s[1], s[0].confidence), reverse=True)
    best_rule, hit_count, hits = scores[0]
    bonus = min(0.1, 0.02 * (hit_count - 1))
    conf = min(0.9, best_rule.confidence + bonus)

    return {
        "owner": best_rule.owner,
        "area": best_rule.area,
        "doc_type": best_rule.doc_type,
        "doc_date": "",
        "confidence": conf,
        "short_name": best_rule.short_name_hint,
        "matched_terms": hits,
    }


def _stem(name: str) -> str:
    """Tiny helper to avoid importing pathlib just for a stem split."""
    base = name.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base
