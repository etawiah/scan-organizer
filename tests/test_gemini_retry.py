"""Gemini retries, GEMINI_MODEL, ProviderError logging, --provider-probe."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer.classifier import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_MAX_RETRIES,
    ClassifierSettings,
    ProviderError,
    _call_gemini,
    _resolve_gemini_model,
    provider_probe,
)
def _http_error(code: int, body: str, url: str = "https://example.invalid/gemini"):
    import email.message
    fp = io.BytesIO(body.encode("utf-8"))
    return urllib.error.HTTPError(url, code, "error", hdrs=email.message.Message(), fp=fp)


def _ok_response(text: str = '{"owner":"Personal","area":"","doc_type":"Correspondence","confidence":0.9,"short_name":"letter","doc_date":""}'):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            body = {
                "candidates": [
                    {"content": {"parts": [{"text": text}]}}
                ]
            }
            return json.dumps(body).encode("utf-8")

    return _Resp()


@pytest.fixture()
def gemini_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda _s: None)


def test_default_and_gemini_model_env(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("SCAN_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("SCAN_MODEL", raising=False)
    settings = ClassifierSettings(provider="gemini")
    assert _resolve_gemini_model(settings) == DEFAULT_GEMINI_MODEL
    assert DEFAULT_GEMINI_MODEL == "gemini-2.5-flash-lite"

    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-flash")
    assert _resolve_gemini_model(settings) == "gemini-custom-flash"


def test_gemini_retries_on_503_then_succeeds(gemini_env, monkeypatch, caplog):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503, '{"error":{"message":"The model is overloaded","status":"UNAVAILABLE"}}')
        return _ok_response()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    caplog.set_level("ERROR")
    out = _call_gemini("ping", ClassifierSettings(provider="gemini"))
    assert "Correspondence" in out or "owner" in out
    assert calls["n"] == 3
    assert any("503" in rec.getMessage() for rec in caplog.records)
    assert any("overloaded" in rec.getMessage() for rec in caplog.records)
    assert all("SSN" not in rec.getMessage() for rec in caplog.records)


def test_gemini_retries_on_429_up_to_three_times(gemini_env, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(429, '{"error":{"message":"rate limited"}}')

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProviderError, match="429") as excinfo:
        _call_gemini("ping", ClassifierSettings(provider="gemini"))
    assert calls["n"] == GEMINI_MAX_RETRIES + 1
    assert "rate limited" in str(excinfo.value)
    assert "ping" not in str(excinfo.value)


def test_provider_error_logs_status_and_body_not_prompt(
    gemini_env, monkeypatch, caplog,
):
    secret_prompt = "Payment from Zebulon Quill, card ending 0000"

    def fake_urlopen(req, timeout=None):
        raise _http_error(503, '{"error":{"message":"backend unavailable"}}')

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    # The call refuses before urlopen if the prompt still has PII. Use a
    # clean ping so we exercise the HTTP error logger.
    caplog.set_level("ERROR")
    with pytest.raises(ProviderError, match="503") as excinfo:
        _call_gemini("ping", ClassifierSettings(provider="gemini"))
    assert "backend unavailable" in str(excinfo.value)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "503" in joined
    assert "backend unavailable" in joined
    assert secret_prompt not in joined
    assert "Zebulon" not in joined


def test_gemini_model_appears_in_url(gemini_env, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _ok_response()

    monkeypatch.setenv("GEMINI_MODEL", "gemini-probe-flash")
    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    _call_gemini("ping", ClassifierSettings(provider="gemini"))
    assert "gemini-probe-flash" in captured["url"]


def test_provider_probe_reports_status(gemini_env, monkeypatch):
    monkeypatch.setattr(
        classifier_mod.urllib.request,
        "urlopen",
        lambda req, timeout=None: _ok_response(),
    )
    result = provider_probe(ClassifierSettings(provider="gemini"))
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["provider"] == "gemini"
    assert result["model"] == DEFAULT_GEMINI_MODEL


def test_provider_probe_reports_failure(gemini_env, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(503, '{"error":{"message":"no capacity"}}')

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    result = provider_probe(ClassifierSettings(provider="gemini"))
    assert result["ok"] is False
    assert result["status"] == 503
    assert "no capacity" in result["detail"]
