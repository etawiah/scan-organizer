"""Mirrored / flipped OCR retry keeps the best orientation."""
from __future__ import annotations

import sys

import pytest

from scanorganizer.ocr import (
    extract_text,
    ocr_image_best,
    ocr_quality_low,
    score_ocr,
    word_hit_rate,
)


class _FakeTess:
    Output = type("O", (), {"DICT": "dict"})()

    @staticmethod
    def _is_upright(im) -> bool:
        rgb = im.convert("RGB")
        return rgb.getpixel((5, 5)) == (255, 0, 0)

    @classmethod
    def image_to_string(cls, im, **_k) -> str:
        if cls._is_upright(im):
            return "Invoice Amount Due Electric Bill Statement"
        return "x q z"

    @classmethod
    def image_to_data(cls, im, output_type=None, **_k):
        if cls._is_upright(im):
            return {"conf": ["90", "88", "92", "85"]}
        return {"conf": ["4", "6", "3", "5"]}


def test_word_hit_rate_and_quality_helpers():
    assert word_hit_rate("Invoice Amount Due Electric") > 0.8
    assert word_hit_rate("x q z") < 0.25
    assert ocr_quality_low("x q z", confidence=5.0)
    assert not ocr_quality_low("Invoice Amount Due Electric Bill Statement", confidence=90.0)
    assert score_ocr("Invoice Amount Due Electric Bill Statement", 90) > score_ocr("x q z", 5)


def test_mirrored_synthetic_image_keeps_flipped_text(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    # Red marker at top-left marks the readable orientation. Save the
    # horizontally flipped image so the first OCR pass looks mirrored.
    upright = Image.new("RGB", (80, 40), "white")
    upright.putpixel((5, 5), (255, 0, 0))
    mirrored = upright.transpose(Image.FLIP_LEFT_RIGHT)
    path = tmp_path / "mirrored.png"
    mirrored.save(path)

    monkeypatch.setitem(sys.modules, "pytesseract", _FakeTess)

    text = extract_text(path)
    assert "Invoice" in text
    assert "Electric" in text
    assert "x q z" not in text


def test_ocr_image_best_retries_flip_when_first_pass_is_weak():
    pytest.importorskip("PIL")
    from PIL import Image

    upright = Image.new("RGB", (80, 40), "white")
    upright.putpixel((5, 5), (255, 0, 0))
    mirrored = upright.transpose(Image.FLIP_LEFT_RIGHT)

    text = ocr_image_best(mirrored, pytesseract=_FakeTess)
    assert "Invoice Amount Due" in text


def test_high_quality_original_is_not_rotated():
    pytest.importorskip("PIL")
    from PIL import Image

    calls = []

    class _Once:
        Output = type("O", (), {"DICT": "dict"})()

        @classmethod
        def image_to_string(cls, im, **_k):
            calls.append("string")
            return "Invoice Amount Due Electric Bill Statement"

        @classmethod
        def image_to_data(cls, im, output_type=None, **_k):
            calls.append("data")
            return {"conf": ["90", "91", "88"]}

    img = Image.new("RGB", (40, 20), "white")
    text = ocr_image_best(img, pytesseract=_Once)
    assert "Invoice" in text
    # original + confidence only; no flip / 180 pass
    assert calls.count("string") == 1
