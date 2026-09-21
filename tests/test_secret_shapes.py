"""Secrets pasted with surrounding text are trimmed to the token itself."""

from __future__ import annotations

import logging

import pytest

from pipeline.config import load_settings


def _key(prefix: str, n: int = 40) -> str:
    return prefix + "".join(chr(ord("a") + i % 26) for i in range(n))


def test_anthropic_key_is_extracted_from_a_pasted_curl_example(monkeypatch, caplog):
    key = _key("sk-ant-api03-")
    monkeypatch.setenv("ANTHROPIC_API_KEY", (
        "curl https://api.anthropic.com/v1/messages \\\n"
        f'  --header "x-api-key: {key}" \\\n'
        '  --data \'{"model": "claude-opus-5"}\''
    ))
    with caplog.at_level(logging.WARNING):
        assert load_settings().anthropic_api_key == key
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_bot_token_is_extracted_from_botfather_message(monkeypatch):
    token = "1234567890:" + "A" * 35
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"Use this token to access the HTTP API:\n{token}\nKeep it safe")
    assert load_settings().telegram_bot_token == token


def test_owner_id_is_extracted_from_userinfobot_reply(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "Id: 123456789\nFirst: Maria")
    assert load_settings().telegram_owner_id == "123456789"


@pytest.mark.parametrize("name,value", [
    ("TELEGRAM_BOT_TOKEN", "1234567890:" + "B" * 35),
    ("TELEGRAM_OWNER_ID", "987654321"),
    ("ANTHROPIC_API_KEY", _key("sk-ant-")),
    ("TELEGRAM_CHANNEL_ID", "@realnew_mary"),
])
def test_clean_values_pass_through_unchanged(monkeypatch, name, value):
    monkeypatch.setenv(name, f"  '{value}' \n")
    settings = load_settings()
    field = {"TELEGRAM_BOT_TOKEN": "telegram_bot_token", "TELEGRAM_OWNER_ID": "telegram_owner_id",
             "ANTHROPIC_API_KEY": "anthropic_api_key", "TELEGRAM_CHANNEL_ID": "telegram_channel_id"}[name]
    assert getattr(settings, field) == value


def test_unrecognisable_value_is_kept_as_is(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key-at-all")
    assert load_settings().anthropic_api_key == "not-a-key-at-all"
