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

import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse

from . import local_rules
from .privacy import SensitiveTerms, assert_safe_for_outbound
from .taxonomy import DEFAULT_DOC_TYPE_ALIASES, Taxonomy, alias_doc_type
from .vocabulary import OutboundSignals, extract_outbound_signals

log = logging.getLogger(__name__)


# Owner rule: never default to the newest, most expensive frontier tier.
# Anthropic: the original Haiku 3 is the cheapest hosted tier and never
# auto-upgrades. Gemini 2.0 Flash and 2.0 Flash-Lite shut down 2026-06-01.
# The oldest cheap Flash still listed as live is gemini-2.5-flash-lite
# (Google: "fastest and most budget-friendly" 2.5 multimodal model).
# Override with GEMINI_MODEL (or SCAN_GEMINI_MODEL / SCAN_MODEL / --model).
DEFAULT_ANTHROPIC_MODEL = "claude-3-haiku-20240307"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_OLLAMA_TIMEOUT_S = 60

# Literal hosts plus RFC1918 / loopback. Remote hostnames are refused so a
# mis-set OLLAMA_URL cannot leak redacted OCR off the LAN.
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_LOCALHOST_NAMES = frozenset({"localhost", "localhost.localdomain"})

# Retry transient Gemini capacity / rate-limit failures. "Up to 3 times"
# means 3 retries after the first failure (4 attempts total). Override
# with SCAN_GEMINI_RETRIES or disable with --no-retry.
GEMINI_RETRY_STATUSES = frozenset({429, 503})
GEMINI_MAX_RETRIES = 3
GEMINI_BACKOFF_S = 1.0


class ProviderError(RuntimeError):
    """Raised by remote providers when they fail (missing key, HTTP error …)."""


class CloudCallCapError(ProviderError):
    """Raised when ``--max-cloud-calls`` / ``SCAN_MAX_CLOUD_CALLS`` is exhausted."""


class OllamaURLError(ProviderError):
    """Raised when ``OLLAMA_URL`` is not localhost or an RFC1918/LAN address."""


@dataclass
class CloudCallBudget:
    """Count every HTTP request, including retries.

    ``max_calls is None`` means unlimited. ``used`` still increments so
    operators can see how many requests a run issued.
    """

    max_calls: Optional[int] = None
    used: int = 0
    _logged_cap: bool = False

    def exhausted(self) -> bool:
        hit = self.max_calls is not None and self.used >= self.max_calls
        if hit:
            self._log_cap()
        return hit

    def consume(self) -> bool:
        """Reserve one HTTP request. False if the cap is already reached."""
        if self.max_calls is not None and self.used >= self.max_calls:
            self._log_cap()
            return False
        self.used += 1
        return True

    def _log_cap(self) -> None:
        if self._logged_cap:
            return
        self._logged_cap = True
        log.warning(
            "cloud call cap reached: %s HTTP request(s) (cap %s); "
            "remaining outbound files go to review",
            self.used,
            self.max_calls,
        )


def cloud_cap_result(provider: str) -> Dict[str, object]:
    """Classification payload that the organizer routes to ``_Review/``."""
    return {
        "owner": "",
        "area": "",
        "doc_type": "",
        "doc_date": "",
        "confidence": 0.0,
        "short_name": "",
        "provider": provider,
        "cloud_cap": True,
    }


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


def _build_prompt(taxonomy: Taxonomy, signals: OutboundSignals) -> str:
    """Build an allowlist-only prompt. Never interpolates OCR, filenames, or paths."""
    allowed = taxonomy.describe_for_prompt()
    return f"""You are classifying scanned household business documents into a strict taxonomy.

ALLOWED taxonomy nodes owner area doc_type. You must pick EXACTLY one.
Use empty string for area when the node has no area segment.

{allowed}

Return ONLY a JSON object with these keys nothing else
  owner string must be one of the owners above
  area string empty if the owner has no area segment
  doc_type string must match the owner area pair
  doc_date string empty if unknown
  confidence float reflecting how sure you are about owner area doc_type
  short_name short descriptive name lowercase words underscores no file extension no PII

If you cannot confidently place the document in any node set confidence low
rather than guessing the caller will route low confidence documents for human review.

{signals.as_prompt_block()}
"""


def _build_local_prompt(taxonomy: Taxonomy, scrubbed_ocr: str) -> str:
    """Full-text prompt for a local model. Uses already-redacted OCR."""
    allowed = taxonomy.describe_for_prompt()
    body = scrubbed_ocr if scrubbed_ocr is not None else ""
    return f"""You are classifying scanned household business documents into a strict taxonomy.

ALLOWED taxonomy nodes owner area doc_type. You must pick EXACTLY one.
Use empty string for area when the node has no area segment.

{allowed}

Return ONLY a JSON object with these keys nothing else
  owner string must be one of the owners above
  area string empty if the owner has no area segment
  doc_type string must match the owner area pair
  doc_date string empty if unknown
  confidence float reflecting how sure you are about owner area doc_type
  short_name short descriptive name lowercase words underscores no file extension no PII

If you cannot confidently place the document in any node set confidence low
rather than guessing the caller will route low confidence documents for human review.

The document text below has already had private terms redacted. Classify it.

---
{body}
---
"""


def _is_allowed_ollama_host(host: str) -> bool:
    """True only for localhost, IPv4/IPv6 loopback, or RFC1918 / ULA."""
    name = (host or "").strip().lower().rstrip(".")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name:
        return False
    if name in _LOCALHOST_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 4:
        return any(ip in network for network in _RFC1918_NETWORKS)
    # IPv6 unique-local (fc00::/7) is the LAN equivalent of RFC1918.
    return ip.is_private and not ip.is_link_local and not ip.is_multicast


def validate_ollama_url(url: str) -> str:
    """Return a cleaned Ollama base URL, or raise :class:`OllamaURLError`.

    Only ``http``/``https`` URLs whose host is ``localhost``, loopback, or
    an RFC1918/LAN address are accepted. Hostnames that are not
    ``localhost`` are refused without DNS lookup so a remote name cannot
    be used to leak document text.
    """
    raw = (url or "").strip()
    if not raw:
        raise OllamaURLError("OLLAMA_URL is empty")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise OllamaURLError(
            f"OLLAMA_URL scheme must be http or https, got {parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host:
        raise OllamaURLError("OLLAMA_URL is missing a host")
    if parsed.username or parsed.password:
        raise OllamaURLError("OLLAMA_URL must not include userinfo")
    if not _is_allowed_ollama_host(host):
        raise OllamaURLError(
            f"OLLAMA_URL host {host!r} is not localhost or an RFC1918/LAN address"
        )
    # Rebuild without a trailing path so callers can append /api/chat.
    netloc = parsed.netloc
    cleaned = f"{parsed.scheme}://{netloc}"
    return cleaned.rstrip("/")


def resolve_ollama_url(explicit: Optional[str] = None) -> str:
    raw = explicit if explicit is not None else os.environ.get("OLLAMA_URL")
    if raw is None or str(raw).strip() == "":
        raw = DEFAULT_OLLAMA_URL
    return validate_ollama_url(str(raw))


@dataclass
class ClassifierSettings:
    provider: str
    model: Optional[str] = None
    timeout_s: int = 30
    max_tokens: int = 400
    gemini_retries: Optional[int] = None
    no_retry: bool = False
    cloud_budget: Optional[CloudCallBudget] = None
    ollama_url: Optional[str] = None
    ollama_timeout_s: Optional[int] = None


def _resolve_anthropic_model(settings: ClassifierSettings) -> str:
    return (
        settings.model
        or os.environ.get("SCAN_ANTHROPIC_MODEL")
        or os.environ.get("SCAN_MODEL")
        or DEFAULT_ANTHROPIC_MODEL
    )


def _resolve_gemini_retries(settings: ClassifierSettings) -> int:
    """Number of *retries* after the first attempt. ``0`` means one shot."""
    if settings.no_retry:
        return 0
    if settings.gemini_retries is not None:
        return max(0, int(settings.gemini_retries))
    raw = os.environ.get("SCAN_GEMINI_RETRIES")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0, int(str(raw).strip()))
        except ValueError:
            return GEMINI_MAX_RETRIES
    return GEMINI_MAX_RETRIES


def _urlopen(settings: ClassifierSettings, req: urllib.request.Request, timeout: int):
    """Issue one HTTP request, counting it against the cloud-call budget."""
    budget = settings.cloud_budget
    if budget is not None and not budget.consume():
        raise CloudCallCapError(
            f"cloud call cap reached ({budget.used}/{budget.max_calls})"
        )
    return urllib.request.urlopen(req, timeout=timeout)


def _resolve_gemini_model(settings: ClassifierSettings) -> str:
    return (
        settings.model
        or os.environ.get("GEMINI_MODEL")
        or os.environ.get("SCAN_GEMINI_MODEL")
        or os.environ.get("SCAN_MODEL")
        or DEFAULT_GEMINI_MODEL
    )


def _resolve_ollama_model(settings: ClassifierSettings) -> str:
    return (
        settings.model
        or os.environ.get("OLLAMA_MODEL")
        or os.environ.get("SCAN_MODEL")
        or DEFAULT_OLLAMA_MODEL
    )


def _resolve_ollama_timeout(settings: ClassifierSettings) -> int:
    if settings.ollama_timeout_s is not None:
        return max(1, int(settings.ollama_timeout_s))
    raw = os.environ.get("OLLAMA_TIMEOUT")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, int(str(raw).strip()))
        except ValueError:
            return DEFAULT_OLLAMA_TIMEOUT_S
    return DEFAULT_OLLAMA_TIMEOUT_S


def _ollama_post(url: str, body: Dict[str, object], timeout: int) -> Dict[str, object]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Local: do not count against the cloud-call budget and do not run
    # assert_safe_for_outbound (the prompt is the full redacted OCR).
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_ollama(prompt: str, settings: ClassifierSettings) -> str:
    """Call a LAN Ollama daemon. Tries ``/api/chat`` then ``/api/generate``."""
    base = resolve_ollama_url(settings.ollama_url)
    model = _resolve_ollama_model(settings)
    timeout = _resolve_ollama_timeout(settings)
    chat_body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    try:
        result = _ollama_post(f"{base}/api/chat", chat_body, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise ProviderError(f"Ollama HTTP {exc.code}") from exc
        log.info("Ollama /api/chat returned 404; trying /api/generate")
        result = None
    except Exception as exc:
        raise ProviderError(f"Ollama call failed: {exc}") from exc
    else:
        try:
            return result["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected Ollama /api/chat payload shape") from exc

    generate_body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
    try:
        result = _ollama_post(f"{base}/api/generate", generate_body, timeout)
        return result["response"]
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"Ollama HTTP {exc.code}") from exc
    except (KeyError, TypeError) as exc:
        raise ProviderError("Unexpected Ollama /api/generate payload shape") from exc
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"Ollama call failed: {exc}") from exc


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    """Return the provider error body. Never include the request prompt."""
    try:
        raw = exc.read()
    except Exception:
        return ""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _call_anthropic(
    prompt: str,
    settings: ClassifierSettings,
    terms: Optional[SensitiveTerms] = None,
    taxonomy: Optional[Taxonomy] = None,
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
    assert_safe_for_outbound(prompt, terms, taxonomy=taxonomy)
    try:
        with _urlopen(settings, req, settings.timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except CloudCallCapError:
        raise
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
    taxonomy: Optional[Taxonomy] = None,
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
    assert_safe_for_outbound(prompt, terms, taxonomy=taxonomy)
    last_status: Optional[int] = None
    last_body = ""
    result: Optional[Dict[str, object]] = None
    retries = _resolve_gemini_retries(settings)
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        try:
            with _urlopen(settings, req, settings.timeout_s) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            break
        except CloudCallCapError:
            raise
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_body = _read_http_error_body(exc)
            log.error(
                "Gemini HTTP %s (attempt %s/%s) body=%s",
                exc.code,
                attempt + 1,
                retries + 1,
                last_body,
            )
            if exc.code in GEMINI_RETRY_STATUSES and attempt < retries:
                time.sleep(GEMINI_BACKOFF_S * (2 ** attempt))
                continue
            raise ProviderError(f"Gemini HTTP {exc.code}: {last_body}") from exc
        except Exception as exc:
            raise ProviderError(f"Gemini call failed: {exc}") from exc
    else:
        raise ProviderError(f"Gemini HTTP {last_status}: {last_body}")
    try:
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected Gemini payload shape") from exc


def provider_probe(settings: ClassifierSettings) -> Dict[str, object]:
    """Send a content-free ``ping`` and report HTTP status. No document text."""
    ping = "ping"
    assert_safe_for_outbound(ping)
    model = ""
    try:
        if settings.provider == "local-keyword":
            return {
                "ok": True,
                "provider": "local-keyword",
                "model": "",
                "status": 0,
                "detail": "local-keyword makes no network calls",
            }
        if settings.provider == "ollama":
            model = _resolve_ollama_model(settings)
            _call_ollama(ping, settings)
            return {"ok": True, "provider": "ollama", "model": model, "status": 200}
        if settings.provider == "gemini":
            model = _resolve_gemini_model(settings)
            _call_gemini(ping, settings)
            return {"ok": True, "provider": "gemini", "model": model, "status": 200}
        if settings.provider == "anthropic":
            model = _resolve_anthropic_model(settings)
            _call_anthropic(ping, settings)
            return {"ok": True, "provider": "anthropic", "model": model, "status": 200}
        raise ProviderError(f"Unknown provider: {settings.provider}")
    except ProviderError as exc:
        status = None
        message = str(exc)
        match = re.search(r"HTTP (\d{3})", message)
        if match:
            status = int(match.group(1))
        log.error(
            "Provider probe failed: provider=%s model=%s status=%s error=%s",
            settings.provider,
            model,
            status,
            message,
        )
        return {
            "ok": False,
            "provider": settings.provider,
            "model": model,
            "status": status,
            "detail": message,
        }


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
    local_ocr_text: Optional[str] = None,
) -> Dict[str, object]:
    """Dispatch to the configured provider.

    ``text`` is expected to already have passed through
    :func:`scanorganizer.privacy.gate`. Cloud providers get an allowlist
    prompt — OCR text, filenames, and paths are never interpolated. The
    local ``ollama`` provider is marked ``is_local`` and receives the
    full redacted-by-private-terms OCR (``local_ocr_text`` or ``text``);
    the outbound allowlist and cloud-call budget do not apply. A
    private-term hit on the filename or path still keeps the document
    local-only. A defensive token-allowlist check inside each remote
    call ensures a forgetful caller still cannot leak leftovers.

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

    if settings.provider == "ollama":
        prompt = _build_local_prompt(
            taxonomy,
            local_ocr_text if local_ocr_text is not None else text,
        )
        try:
            raw = _call_ollama(prompt, settings)
            parsed = _coerce(_extract_json(raw))
            parsed["provider"] = "ollama"
            parsed["is_local"] = True
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

    if settings.cloud_budget is not None and settings.cloud_budget.exhausted():
        return cloud_cap_result(settings.provider)

    prompt = _build_prompt(taxonomy, extract_outbound_signals(text))
    try:
        if settings.provider == "anthropic":
            raw = _call_anthropic(prompt, settings, terms=terms, taxonomy=taxonomy)
        elif settings.provider == "gemini":
            raw = _call_gemini(prompt, settings, terms=terms, taxonomy=taxonomy)
        else:
            raise ProviderError(f"Unknown provider: {settings.provider}")
        parsed = _extract_json(raw)
        parsed = _coerce(parsed)
        parsed["provider"] = settings.provider
        return parsed
    except CloudCallCapError:
        return cloud_cap_result(settings.provider)
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
