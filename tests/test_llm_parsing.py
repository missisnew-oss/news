"""Parsing and validating the model's JSON answer."""

from __future__ import annotations

import json

import pytest

from pipeline.llm import LLMError, extract_json, parse_response, validate_payload

VALID = {
    "rubric": "market_pulse",
    "title": "Заголовок поста",
    "body": "<b>Текст</b> поста.",
    "hashtags": ["#дубай"],
    "cta": "Напишите мне.",
    "image": {"mode": "card", "headline": "Заголовок", "accent": "", "stock_query": "dubai"},
    "sources": [{"source_id": "s1", "title": "t", "url": "https://example.com/a"}],
    "facts": [],
    "self_check": {
        "all_numbers_have_source": True,
        "no_guaranteed_returns": True,
        "language_is_russian": True,
        "no_verbatim_copy": True,
        "fits_telegram_limit": True,
    },
    "length_chars": 20,
    "needs_separate_text": False,
}


def test_plain_json():
    assert extract_json(json.dumps(VALID))["rubric"] == "market_pulse"


def test_json_inside_markdown_fence():
    raw = "Вот результат:\n```json\n" + json.dumps(VALID) + "\n```\nГотово."
    assert extract_json(raw)["title"] == "Заголовок поста"


def test_json_with_chatter_around_it():
    raw = "Конечно! " + json.dumps(VALID) + " Надеюсь, подошло."
    assert extract_json(raw)["cta"] == "Напишите мне."


def test_braces_inside_strings_do_not_break_extraction():
    payload = dict(VALID, body="Текст со скобкой } и { внутри")
    raw = "префикс " + json.dumps(payload, ensure_ascii=False) + " суффикс"
    assert extract_json(raw)["body"] == "Текст со скобкой } и { внутри"


def test_empty_response_raises():
    with pytest.raises(LLMError):
        extract_json("   ")


def test_response_without_json_raises():
    with pytest.raises(LLMError):
        extract_json("Извините, не могу.")


def test_unclosed_json_raises():
    with pytest.raises(LLMError):
        extract_json('{"rubric": "market_pulse"')


def test_validate_requires_all_keys():
    broken = dict(VALID)
    broken.pop("self_check")
    with pytest.raises(LLMError, match="self_check"):
        validate_payload(broken)


def test_validate_requires_every_self_check_flag():
    broken = json.loads(json.dumps(VALID))
    broken["self_check"].pop("no_guaranteed_returns")
    with pytest.raises(LLMError, match="no_guaranteed_returns"):
        validate_payload(broken)


def test_validate_rejects_empty_body():
    with pytest.raises(LLMError):
        validate_payload(dict(VALID, body="   "))


def test_validate_fills_image_defaults():
    payload = validate_payload(dict(VALID, image="не объект"))
    assert payload["image"]["mode"] == "card"
    assert payload["image"]["stock_query"]


def test_validate_drops_sources_without_url():
    payload = validate_payload(dict(VALID, sources=[{"title": "нет ссылки"}]))
    assert payload["sources"] == []


def test_parse_response_end_to_end():
    payload = parse_response("```json\n" + json.dumps(VALID, ensure_ascii=False) + "\n```")
    assert payload["self_check"]["language_is_russian"] is True
    assert payload["length_chars"] == 20
