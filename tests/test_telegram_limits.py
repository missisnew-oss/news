"""Telegram hard limits: caption 1024, message 4096."""

from __future__ import annotations

from pipeline.config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT
from pipeline.textutil import (
    plan_delivery, sanitize_telegram_html, split_for_telegram, truncate, visible_length,
)


def test_short_text_is_not_split():
    assert split_for_telegram("короткий текст") == ["короткий текст"]


def test_long_text_split_respects_limit():
    text = ("Абзац номер один.\n\n" * 600)
    chunks = split_for_telegram(text, TG_MESSAGE_LIMIT)
    assert len(chunks) > 1
    assert all(len(chunk) <= TG_MESSAGE_LIMIT for chunk in chunks)


def test_split_prefers_paragraph_boundaries():
    text = "A" * 4000 + "\n\n" + "B" * 500
    chunks = split_for_telegram(text, TG_MESSAGE_LIMIT)
    assert chunks[1].startswith("B")


def test_split_terminates_on_text_without_separators():
    text = "x" * (TG_MESSAGE_LIMIT * 3 + 17)
    chunks = split_for_telegram(text, TG_MESSAGE_LIMIT)
    assert sum(len(c) for c in chunks) == len(text)
    assert all(len(c) <= TG_MESSAGE_LIMIT for c in chunks)


def test_truncate_cuts_on_word_boundary():
    out = truncate("одно два три четыре пять", 12)
    assert out.endswith("…")
    assert len(out) <= 12
    assert "четыре" not in out


def test_plan_delivery_photo_with_short_caption():
    plan = plan_delivery("к" * 500, has_photo=True)
    assert plan["mode"] == "photo"
    assert len(plan["caption"]) <= TG_CAPTION_LIMIT
    assert plan["texts"] == []


def test_plan_delivery_photo_with_long_text_splits_off():
    plan = plan_delivery("слово " * 400, has_photo=True)
    assert plan["mode"] == "photo_plus_text"
    assert len(plan["caption"]) <= TG_CAPTION_LIMIT
    assert plan["texts"]
    assert all(len(t) <= TG_MESSAGE_LIMIT for t in plan["texts"])


def test_plan_delivery_without_photo_uses_message_limit():
    plan = plan_delivery("я" * 5000, has_photo=False)
    assert plan["mode"] == "text"
    assert plan["caption"] is None
    assert all(len(t) <= TG_MESSAGE_LIMIT for t in plan["texts"])


def test_nothing_ever_exceeds_the_api_limit():
    for length in (900, 1023, 1024, 1025, 4095, 4096, 4097, 12000):
        for photo in (True, False):
            plan = plan_delivery("т" * length, has_photo=photo)
            if plan["caption"]:
                assert len(plan["caption"]) <= TG_CAPTION_LIMIT
            assert all(len(t) <= TG_MESSAGE_LIMIT for t in plan["texts"])


def test_sanitize_strips_tags_telegram_rejects():
    html = '<script>bad()</script><div><b>жирный</b> <a href="https://x.dev">ссылка</a></div>'
    clean = sanitize_telegram_html(html)
    assert "<script>" not in clean and "<div>" not in clean
    assert "<b>жирный</b>" in clean
    assert '<a href="https://x.dev">ссылка</a>' in clean


def test_visible_length_ignores_markup():
    assert visible_length("<b>абв</b>") == 3
