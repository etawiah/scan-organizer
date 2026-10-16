"""Local Ollama provider: full redacted OCR, LAN-only URL, privacy routing."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.classifier import (
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_TIMEOUT_S,
    DEFAULT_OLLAMA_URL,
    MIN_OLLAMA_TIMEOUT_S,
    OLLAMA_OCR_CHAR_CAP,
    CloudCallBudget,
    ClassifierSettings,
    OllamaURLError,
    _resolve_ollama_model,
    _resolve_ollama_timeout,
    cap_ollama_ocr_text,
    _is_allowed_ollama_host,
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
        "http://192.168.3.10:11434",
        "http://172.16.0.1:11434",
        "http://172.31.255.1",
        "https://192.168.0.2:11434",
        "http://[::1]",
        "http://[fd00::5]",
        "http://[fd00::5]:11434",
    ],
)
def test_validate_ollama_url_allows_localhost_and_rfc1918(url):
    cleaned = validate_ollama_url(url)
    assert cleaned.startswith("http")
    assert "://" in cleaned


def test_validate_ollama_url_accepts_bracketed_ula_and_loopback():
    assert validate_ollama_url("http://[::1]").endswith("://[::1]")
    assert validate_ollama_url("http://[fd00::5]").endswith("://[fd00::5]")


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
        "http://[::]",
        "http://0.0.0.0:11434",
        "http://[2001:db8::1]",
        "http://[64:ff9b:1::808:808]",
        "http://[::ffff:8.8.8.8]",
        "http://192.168.1.1.evil.com",
        "http://user@192.168.1.5",
    ],
)
def test_validate_ollama_url_refuses_remote_hosts(url):
    with pytest.raises(OllamaURLError):
        validate_ollama_url(url)


@pytest.mark.parametrize(
    "host",
    [
        "::",
        "2001:db8::1",
        "64:ff9b:1::808:808",
        "::ffff:8.8.8.8",
        "[::ffff:8.8.8.8]",
        "0.0.0.0",
        "192.168.1.1.evil.com",
    ],
)
def test_ollama_host_allowlist_refuses_review_cases(host):
    assert _is_allowed_ollama_host(host) is False


@pytest.mark.parametrize("host", ["::1", "[::1]", "fd00::5", "[fd00::5]"])
def test_ollama_host_allowlist_accepts_loopback_and_ula(host):
    assert _is_allowed_ollama_host(host) is True


def test_ollama_url_refuses_userinfo_without_password():
    with pytest.raises(OllamaURLError, match="userinfo"):
        validate_ollama_url("http://user@192.168.1.5")


def test_default_ollama_url_is_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    assert resolve_ollama_url() == "http://localhost:11434"
    assert DEFAULT_OLLAMA_URL == "http://localhost:11434"


def test_default_ollama_model_is_qwen(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("SCAN_MODEL", raising=False)
    assert DEFAULT_OLLAMA_MODEL == "qwen2.5:7b-instruct"
    assert _resolve_ollama_model(ClassifierSettings(provider="ollama")) == (
        "qwen2.5:7b-instruct"
    )


def test_ollama_timeout_default_180_floor_120(monkeypatch):
    monkeypatch.delenv("OLLAMA_TIMEOUT", raising=False)
    assert DEFAULT_OLLAMA_TIMEOUT_S == 180
    assert MIN_OLLAMA_TIMEOUT_S == 120
    assert _resolve_ollama_timeout(ClassifierSettings(provider="ollama")) == 180
    monkeypatch.setenv("OLLAMA_TIMEOUT", "60")
    assert _resolve_ollama_timeout(ClassifierSettings(provider="ollama")) == 120
    monkeypatch.setenv("OLLAMA_TIMEOUT", "240")
    assert _resolve_ollama_timeout(ClassifierSettings(provider="ollama")) == 240
    assert _resolve_ollama_timeout(
        ClassifierSettings(provider="ollama", ollama_timeout_s=30)
    ) == 120


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
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("SCAN_MODEL", raising=False)
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        body = json.loads(req.data.decode("utf-8"))
        assert body["format"] == "json"
        assert body["model"] == "qwen2.5:7b-instruct"
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


def test_ollama_ocr_capped_at_about_2500_tokens(monkeypatch, taxonomy):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.3.10:11434")
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _ok_chat()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    long_ocr = (
        "INVOICE HEADER START " + ("lorem " * 4000) + " UNIQUE_TAIL_MARKER_SHOULD_BE_DROPPED"
    )
    assert len(long_ocr) > OLLAMA_OCR_CHAR_CAP
    settings = ClassifierSettings(provider="ollama")
    result = classify(long_ocr, "long.pdf", taxonomy, settings, local_ocr_text=long_ocr)
    assert result["provider"] == "ollama"
    prompt = captured[0]["messages"][0]["content"]
    capped = cap_ollama_ocr_text(long_ocr)
    assert len(capped) == OLLAMA_OCR_CHAR_CAP
    assert capped in prompt
    assert long_ocr not in prompt
    assert "INVOICE HEADER START" in prompt
    assert "UNIQUE_TAIL_MARKER_SHOULD_BE_DROPPED" not in prompt


def test_ollama_requests_are_strictly_sequential(monkeypatch, taxonomy):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.3.20:11434")
    state = {"active": 0, "max_active": 0}
    barrier = threading.Lock()

    def fake_urlopen(req, timeout=None):
        with barrier:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)
        with barrier:
            state["active"] -= 1
        return _ok_chat()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    settings = ClassifierSettings(provider="ollama")

    def _run():
        classify(CLEAN_BILL, "bill.pdf", taxonomy, settings)

    threads = [
        threading.Thread(target=_run),
        threading.Thread(target=_run),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["max_active"] == 1


def test_env_example_uses_lan_placeholder():
    text = Path(__file__).resolve().parent.parent.joinpath(".env.example").read_text(
        encoding="utf-8",
    )
    assert "# OLLAMA_URL=http://192.168.x.x:11434" in text
    assert "# OLLAMA_MODEL=qwen2.5:7b-instruct" in text
    assert "llama3.2" not in text


def test_low_confidence_ollama_answer_goes_to_review(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.3.10:11434")
    _write(tmp_scan_tree["src"] / "bill.pdf", CLEAN_BILL)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return CLEAN_BILL

    def fake_urlopen(req, timeout=None):
        return _ok_chat(json.dumps({
            "owner": "Household",
            "area": "Home",
            "doc_type": "Utilities",
            "confidence": 0.2,
            "short_name": "should_not_file",
            "doc_date": "",
        }))

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    cfg = runconfig_factory(provider="ollama")
    result = process_batch(cfg)
    assert result.outcomes
    outcome = result.outcomes[0]
    assert outcome.action == "review"
    assert cfg.review_dir in outcome.dest.parents
    assert outcome.classification.get("confidence") == 0.2
    assert "should_not_file" not in (outcome.dest.name if outcome.dest else "")


def test_private_term_kids_hit_never_calls_ollama(
    tmp_scan_tree, runconfig_factory, monkeypatch,
):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.3.10:11434")
    kids = (
        "Placeholder Elementary Report Card — grade 4 — immunization record. "
        "Student Zebulon Quill."
    )
    _write(tmp_scan_tree["src"] / "zebulon_report_card.pdf", kids)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return kids

    def boom(*_a, **_k):
        raise AssertionError("ollama must not be called for a private-term/kids hit")

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)
    monkeypatch.setattr(classifier_mod, "_call_ollama", boom)
    monkeypatch.setattr(classifier_mod, "classify", boom)
    cfg = runconfig_factory(provider="ollama", extra_terms=["Zebulon Quill"])
    result = process_batch(cfg)
    assert result.outcomes
    outcome = result.outcomes[0]
    assert outcome.provider == "local-keyword"
    assert outcome.classification.get("privacy_blocked") is True
    assert outcome.classification.get("privacy") == "local-only: sensitive"
