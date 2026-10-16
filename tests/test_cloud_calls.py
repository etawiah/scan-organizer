"""Cloud HTTP budget: --max-cloud-calls counts retries; leftovers go to review."""
from __future__ import annotations

from pathlib import Path

import pytest

from scanorganizer import classifier as classifier_mod
from scanorganizer import ocr as ocr_mod
from scanorganizer.classifier import (
    CloudCallBudget,
    CloudCallCapError,
    ClassifierSettings,
    ProviderError,
    _call_gemini,
    _resolve_gemini_retries,
)
from scanorganizer.organizer import process_batch
from tests.test_gemini_retry import _http_error, _ok_response
from tests.test_outbound_privacy import _write


CLEAN_BILL = "City Electric Co monthly electric bill. kWh used 541. Amount due $40.00."


@pytest.fixture()
def gemini_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda _s: None)


def _seed_bills(tmp_scan_tree, monkeypatch, names):
    for name in names:
        _write(tmp_scan_tree["src"] / name, CLEAN_BILL)

    def fake_extract(path: Path, max_pages: int = 5, dpi: int = 300) -> str:
        return CLEAN_BILL

    monkeypatch.setattr(ocr_mod, "extract_text", fake_extract)


def test_resolve_gemini_retries_no_retry_and_env(monkeypatch):
    monkeypatch.delenv("SCAN_GEMINI_RETRIES", raising=False)
    assert _resolve_gemini_retries(ClassifierSettings(provider="gemini")) == 3
    assert _resolve_gemini_retries(
        ClassifierSettings(provider="gemini", no_retry=True)
    ) == 0
    monkeypatch.setenv("SCAN_GEMINI_RETRIES", "0")
    assert _resolve_gemini_retries(ClassifierSettings(provider="gemini")) == 0
    monkeypatch.setenv("SCAN_GEMINI_RETRIES", "2")
    assert _resolve_gemini_retries(ClassifierSettings(provider="gemini")) == 2
    assert _resolve_gemini_retries(
        ClassifierSettings(provider="gemini", no_retry=True)
    ) == 0


def test_no_retry_makes_a_single_http_attempt(gemini_env, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(503, '{"error":{"message":"overloaded"}}')

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    settings = ClassifierSettings(provider="gemini", no_retry=True)
    with pytest.raises(ProviderError, match="503"):
        _call_gemini("ping", settings)
    assert calls["n"] == 1


def test_max_cloud_calls_counts_retries_then_stops(gemini_env, monkeypatch):
    """Cap=2 with default 3 retries: only two HTTP attempts, then cap."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(503, '{"error":{"message":"overloaded"}}')

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)
    budget = CloudCallBudget(max_calls=2)
    settings = ClassifierSettings(provider="gemini", cloud_budget=budget)
    with pytest.raises(CloudCallCapError):
        _call_gemini("ping", settings)
    assert calls["n"] == 2
    assert budget.used == 2
    assert budget.exhausted()


def test_cap_routes_remaining_outbound_files_to_review(
    tmp_scan_tree, runconfig_factory, monkeypatch, caplog,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda _s: None)
    _seed_bills(tmp_scan_tree, monkeypatch, ("bill_a.pdf", "bill_b.pdf"))

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        # First file: 503, 503, 200 — three HTTP requests if uncapped.
        if calls["n"] < 3:
            raise _http_error(503, '{"error":{"message":"overloaded"}}')
        return _ok_response()

    monkeypatch.setattr(classifier_mod.urllib.request, "urlopen", fake_urlopen)

    cfg = runconfig_factory(provider="gemini", dry_run=True)
    cfg.settings.cloud_budget = CloudCallBudget(max_calls=3)
    cfg.settings.gemini_retries = 3
    caplog.set_level("WARNING")
    result = process_batch(cfg)

    # File A consumes 3 requests (2 retries + success). File B is outbound
    # but the cap is already reached, so it must go to review with no extra
    # HTTP call.
    assert calls["n"] == 3
    assert cfg.settings.cloud_budget.used == 3
    by_name = {o.src.name: o for o in result.outcomes}
    assert "bill_a.pdf" in by_name and "bill_b.pdf" in by_name
    assert by_name["bill_b.pdf"].action == "review"
    assert by_name["bill_b.pdf"].reason == "cloud-call-cap"
    assert by_name["bill_b.pdf"].classification.get("cloud_cap") is True
    assert any("cloud call cap reached" in rec.getMessage() for rec in caplog.records)
    assert any("3" in rec.getMessage() for rec in caplog.records)
