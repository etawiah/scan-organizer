"""Classifier providers.

A provider accepts (text, filename) and returns a dict shaped like:

    {
      "owner": "Business",
      "area": "ComplexTech",
      "doc_type": "Invoices-Out",
      "doc_date": "2026-01-15",   # may be ""
      "confidence": 0.92,
      "short_name": "acme_invoice_1234",
    }

Values that fail to parse / don't validate against the taxonomy are the
caller's problem. The caller demotes them to _Review/.

Every outbound HTTP call in this module runs
:func:`scanorganizer.privacy.assert_safe_for_outbound` on the prompt right
before it goes on the wire, as a defense in depth against anyone who
bypasses the ``privacy.gate`` entry point in future code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

from . import local_rules
from .privacy import SensitiveTerms, assert_safe_for_outbound, sanitize_outbound_label
from .taxonomy import DEFAULT_DOC_TYPE_ALIASES, Taxonomy, alias_doc_type

log = logging.getLogger(__name__)


# Owner rule: never default to the newest, most expensive frontier tier.
# Anthropic: the original Haiku 3 is the cheapest hosted tier and never
# auto-upgrades. Gemini: 2.0 Flash is the oldest supported Flash tier at
# the time of writing, and it is explicitly older than 2.5 Flash.
DEFAULT_ANTHROPIC_MODEL = "claude-3-haiku-20240307"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


class ProviderError(RuntimeError):
    """Raised by remote providers when they fail (missing key, HTTP error …)."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(payload: str) -> Dict[str, object]:
    stripped = payload.strip()
    fence = _JSON_FENCE_RE.search(stripped)
    if fence:
        stripped = fence.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def _build_prompt(
    taxonomy: Taxonomy,
    text: str,
    filename: str = "",
    path: str = "",
) -> str:
    allowed = taxonomy.describe_for_prompt()
    extras: list[str] = []
    if filename:
        extras.append(f"Original filename: {filename}")
    if path and path != filename:
        extras.append(f"Original path: {path}")
    extra_block = ("\n".join(extras) + "\n\n") if extras else ""
    return f"""You are classifying scanned household/business documents into a strict taxonomy.

ALLOWED taxonomy nodes (owner / area / doc_type). You must pick EXACTLY one.
Use "" (empty string) for area when the node has no area segment.

{allowed}

Return ONLY a JSON object with these keys, nothing else:
  "owner":        string, must be one of the owners above
  "area":         string, "" if the owner has no area segment
  "doc_type":     string, must match the owner/area pair
  "doc_date":     string "YYYY-MM-DD" (document date), "" if unknown
  "confidence":   float 0..1 reflecting how sure you are about owner/area/doc_type
  "short_name":   short descriptive name (lowercase, 2-4 words, underscores,
                  no file extension, no PII)

If you cannot confidently place the document in any node, set confidence < 0.5
rather than guessing — the caller will route low-confidence documents for human review.

{extra_block}Document text (sensitive values already redacted):
---
{text}
---
"""


@dataclass
class ClassifierSettings:
    provider: str
    model: Optional[str] = None
    timeout_s: int = 30
    max_tokens: int = 400


def _resolve_anthropic_model(settings: ClassifierSettings) -> str:
    return (
        settings.model
        or os.environ.get("SCAN_ANTHROPIC_MODEL")
        or os.environ.get("SCAN_MODEL")
        or DEFAULT_ANTHROPIC_MODEL
    )


def _resolve_gemini_model(settings: ClassifierSettings) -> str:
    return (
        settings.model
        or os.environ.get("SCAN_GEMINI_MODEL")
        or os.environ.get("SCAN_MODEL")
        or DEFAULT_GEMINI_MODEL
    )


def _call_anthropic(
    prompt: str,
    settings: ClassifierSettings,
    terms: Optional[SensitiveTerms] = None,
) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ProviderError("ANTHROPIC_API_KEY not set")
    model = _resolve_anthropic_model(settings)
    body = {
        "model": model,
        "max_tokens": settings.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    assert_safe_for_outbound(prompt, terms)
    try:
        with urllib.request.urlopen(req, timeout=settings.timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"Anthropic HTTP {exc.code}") from exc
    except Exception as exc:
        raise ProviderError(f"Anthropic call failed: {exc}") from exc
    try:
        return result["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected Anthropic payload shape") from exc


def _call_gemini(
    prompt: str,
    settings: ClassifierSettings,
    terms: Optional[SensitiveTerms] = None,
) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ProviderError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
    model = _resolve_gemini_model(settings)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": settings.max_tokens,
            "responseMimeType": "application/json",
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    assert_safe_for_outbound(prompt, terms)
    try:
        with urllib.request.urlopen(req, timeout=settings.timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"Gemini HTTP {exc.code}") from exc
    except Exception as exc:
        raise ProviderError(f"Gemini call failed: {exc}") from exc
    try:
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected Gemini payload shape") from exc


def _labels_contain_private_term(
    filename: str,
    file_path: str,
    terms: Optional[SensitiveTerms],
) -> bool:
    if terms is None:
        return False
    for value in (filename, file_path):
        if value and terms.contains_sensitive(str(value)):
            return True
    return False


def classify(
    text: str,
    filename: str,
    taxonomy: Taxonomy,
    settings: ClassifierSettings,
    terms: Optional[SensitiveTerms] = None,
    file_path: str = "",
) -> Dict[str, object]:
    """Dispatch to the configured provider.

    ``text`` is expected to already have passed through
    :func:`scanorganizer.privacy.gate`. A defensive residual-PII check
    inside each remote call ensures a forgetful caller still cannot leak
    unscrubbed IDs. Filenames and paths are redacted (or dropped) before
    they are interpolated into the prompt; a private-term hit on either
    keeps the document local-only.

    Falls back to the local keyword classifier if a remote provider fails.
    """
    if settings.provider == "local-keyword":
        result = local_rules.classify(text, taxonomy, filename)
        result["provider"] = "local-keyword"
        return result

    if _labels_contain_private_term(filename, file_path, terms):
        result = local_rules.classify(text, taxonomy, filename)
        result["provider"] = "local-keyword"
        result["privacy"] = "local-only: sensitive"
        result["privacy_blocked"] = True
        return result

    safe_name = sanitize_outbound_label(filename, terms)
    safe_path = sanitize_outbound_label(file_path, terms) if file_path else ""
    if safe_path == safe_name:
        safe_path = ""
    prompt = _build_prompt(taxonomy, text, filename=safe_name, path=safe_path)
    try:
        if settings.provider == "anthropic":
            raw = _call_anthropic(prompt, settings, terms=terms)
        elif settings.provider == "gemini":
            raw = _call_gemini(prompt, settings, terms=terms)
        else:
            raise ProviderError(f"Unknown provider: {settings.provider}")
        parsed = _extract_json(raw)
        parsed = _coerce(parsed)
        parsed["provider"] = settings.provider
        return parsed
    except Exception as exc:
        log.warning(
            "Classifier provider %s failed, falling back to local-keyword: %s",
            settings.provider,
            type(exc).__name__,
        )
        fallback = local_rules.classify(text, taxonomy, filename)
        fallback["provider"] = "local-keyword"
        fallback["fallback_from"] = settings.provider
        fallback["fallback_reason"] = type(exc).__name__
        return fallback


def _coerce(raw: Dict[str, object]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    out["owner"] = str(raw.get("owner", "") or "").strip()
    area = raw.get("area", "")
    out["area"] = "" if area is None else str(area).strip()
    out["doc_type"] = alias_doc_type(
        str(raw.get("doc_type", "") or "").strip(),
        DEFAULT_DOC_TYPE_ALIASES,
    )
    out["doc_date"] = str(raw.get("doc_date", "") or "").strip()
    try:
        out["confidence"] = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    out["short_name"] = str(raw.get("short_name", "") or "").strip()
    return out
