"""Local Ollama provider: full redacted OCR, LAN-only URL, privacy routing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.classifier import (
    DEFAULT_OLLAMA_URL,
    CloudCallBudget,
    ClassifierSettings,
    OllamaURLError,
    classify,
    resolve_ollama_url,
    validate_ollama_url,
)
from scanorganizer.config import is_local_provider
from scanorganizer.organizer import process_batch
from scanorganizer.privacy import SensitiveTerms, gate
from tests.test_gemini_retry import _http_error
from tests.test_outbound_privacy import _write


CLEAN_BILL = "City Electric Co monthly electric bill. kWh used 541. Amount due $40.00."


def _ok_chat(text: str | None = None):
    payload = text or json.dumps({
        "owner": "Household",
        "area": "Home",
        "doc_type": "Utilities",
        "confidence": 0.91,
        "short_name": "electric_bill",
        "doc_date": "",
    })
    body = json.dumps({"message": {"role": "assistant", "content": payload}}).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def _ok_generate(text: str | None = None):
    payload = text or json.dumps({
        "owner": "Household",
        "area": "Home",
        "doc_type": "Utilities",
        "confidence": 0.91,
        "short_name": "electric_bill",
        "doc_date": "",
    })
    body = json.dumps({"response": payload}).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
        "http://10.0.0.5:11434",
        "http://192.168.1.10",
        "http://172.16.0.1:11434",
        "http://172.31.255.1",
        "https://192.168.0.2:11434",
    ],
)
def test_validate_ollama_url_allows_localhost_and_rfc1918(url):
    cleaned = validate_ollama_url(url)
    assert cleaned.startswith("http")
    assert "://" in cleaned


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:11434",
        "https://api.openai.com/v1",
        "http://ollama.example.com",
        "http://1.1.1.1",
        "http://172.32.0.1",
        "http://169.254.169.254",
        "ftp://localhost:11434",
        "http://evil.com",
        "http://localhost.evil.com",
        "",
        "http://",
        "http://user:pass@127.0.0.1:11434",
    ],
)
def test_validate_ollama_url_refuses_remote_hosts(url):
    with pytest.raises(OllamaURLError):
        validate_ollama_url(url)


def test_default_ollama_url_is_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    assert resolve_ollama_url() == "http://localhost:11434"
    assert DEFAULT_OLLAMA_URL == "http://localhost:11434"


def test_ollama_is_local_provider():
    assert is_local_provider("ollama")
    assert is_local_provider("local-keyword")
    assert not is_local_provider("gemini")
    assert not is_local_provider("anthropic")


def test_remote_ollama_url_never_opens_a_socket(monkeypatch, taxonomy):
    monkeypatch.setenv("OLLAMA_URL", "https://api.openai.com")
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("urlopen must not run for a remote OLLAMA_URL")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", boom)
    settings = ClassifierSettings(provider="ollama")
    result = classify(CLEAN_BILL, "bill.pdf", taxonomy, settings)
    assert calls["n"] == 0
    assert result["provider"] == "local-keyword"
    assert result.get("fallback_from") == "ollama"
    assert result.get("fallback_reason") == "OllamaURLError"


def test_ollama_sends_full_redacted_ocr_and_skips_allowlist(
    monkeypatch, taxonomy,
):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2")
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        body = json.loads(req.data.decode("utf-8"))
        assert body["format"] == "json"
        assert body["model"] == "llama3.2"
        assert body["stream"] is False
        return _ok_chat()

    def forbid_assert(*_a, **_k):
        raise AssertionError("assert_safe_for_outbound must not run for ollama")

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(classifier_mod, "assert_safe_for_outbound", forbid_assert)

    terms = SensitiveTerms.load(path=None, include_builtins=False)
    text = CLEAN_BILL + " Contact Marta Quill at the office."
    report = gate(text, terms)
    settings = ClassifierSettings(provider="ollama")
    result = classify(
        text,
        "bill.pdf",
        taxonomy,
        settings,
        terms=terms,
        local_ocr_text=report.scrubbed_text,
    )
    assert result["provider"] == "ollama"
    assert result.get("is_local") is True
    assert captured, "expected an Ollama HTTP call"
    assert str(captured[0].full_url).endswith("/api/chat")
    sent = json.loads(captured[0].data.decode("utf-8"))
    prompt = sent["messages"][0]["content"]
    assert "kWh used 541" in prompt
    assert "City Electric Co" in prompt
    assert report.scrubbed_text in prompt


def test_ollama_falls_back_to_generate_on_chat_404(monkeypatch, taxonomy):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.1.10:11434")
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        if str(req.full_url).endswith("/api/chat"):
            raise _http_error(404, "not found")
        body = json.loads(req.data.decode("utf-8"))
        assert body["format"] == "json"
        assert "prompt" in body
        return _ok_generate()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    settings = ClassifierSettings(provider="ollama")
    result = classify(CLEAN_BILL, "bill.pdf", taxonomy, settings)
    assert result["provider"] == "ollama"
    assert any(u.endswith("/api/chat") for u in seen)
    assert any(u.endswith("/api/generate") for u in seen)


def test_ollama_ignores_cloud_call_budget(monkeypatch, taxonomy):
    monkeypatch.setenv("OLLAMA_URL", "http://10.1.2.3:11434")
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _ok_chat()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    budget = CloudCallBudget(max_calls=0)
    settings = ClassifierSettings(provider="ollama", cloud_budget=budget)
    result = classify(CLEAN_BILL, "bill.pdf", taxonomy, settings)
    assert result["provider"] == "ollama"
    assert calls["n"] == 1
    assert budget.used == 0


def test_ollama_private_term_still_stays_local(monkeypatch, taxonomy):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")

    def boom(*_a, **_k):
        raise AssertionError("ollama must not be called for a private-term filename")

    monkeypatch.setattr(classifier_mod, "_call_ollama", boom)
    terms = SensitiveTerms.load(
        path=None, extra_terms=["Zebulon Quill"], include_builtins=False,
    )
    settings = ClassifierSettings(provider="ollama")
    result = classify(
        CLEAN_BILL,
        "zebulon_quill_bill.pdf",
        taxonomy,
        settings,
        terms=terms,
        file_path="/scans/Zebulon/bill.pdf",
    )
    assert result["provider"] == "local-keyword"
    assert result.get("privacy_blocked") is True


def test_ollama_local_only_category_still_short_circuits(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")

    def boom(*_a, **_k):
        raise AssertionError("ollama must not be called for Finance-Bank")

    monkeypatch.setattr(classifier_mod, "_call_ollama", boom)
    _write(
        tmp_scan_tree["src"] / "bank.pdf",
        "checking account monthly statement deposit withdrawal",
    )

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return "checking account monthly statement deposit withdrawal"

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    cfg = runconfig_factory(provider="ollama")
    result = process_batch(cfg)
    assert result.outcomes
    assert result.outcomes[0].provider == "local-keyword"
    assert result.outcomes[0].classification.get("privacy_blocked") is True
