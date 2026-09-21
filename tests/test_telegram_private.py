"""Private Telegram channels read through a user session (Telethon), with the client faked."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from pipeline import collect, telegram_private, verify_sources
from pipeline.config import Settings, load_sources, validate_sources_doc
from pipeline.telegram_private import (
    fetch_private, is_configured, message_to_item, normalize_channel_id, permalink,
)

SOURCE = {"id": "tg_private_demo", "type": "telegram_private", "category": "realty_news",
          "lang": "ru", "url": "https://t.me/c/0", "channel_id": 1234567890, "enabled": True}


@dataclass
class FakeMessage:
    id: int
    message: str | None
    date: datetime | None = None
    views: int | None = None


@dataclass
class FakeClient:
    messages: list[FakeMessage]
    authorized: bool = True
    calls: list = field(default_factory=list)
    disconnected: bool = False

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, peer):
        self.calls.append(peer.channel_id)
        return peer

    async def get_messages(self, entity, limit):
        return self.messages[:limit]

    async def disconnect(self):
        self.disconnected = True


def configured() -> Settings:
    return Settings(dry_run=False, telegram_api_id="12345", telegram_api_hash="h" * 32,
                    telegram_session="s" * 40)


def test_channel_id_accepts_bot_api_and_bare_forms():
    assert normalize_channel_id("-1001234567890") == 1234567890
    assert normalize_channel_id(1234567890) == 1234567890
    assert permalink(-1001234567890, 7) == "https://t.me/c/1234567890/7"


def test_message_mapping_skips_media_only_posts():
    when = datetime(2026, 9, 20, 10, 15, tzinfo=timezone.utc)
    item = message_to_item(FakeMessage(55, "Застройщик открыл продажи\n320 квартир", when, 900), SOURCE)
    assert item["title"] == "Застройщик открыл продажи"
    assert item["url"] == "https://t.me/c/1234567890/55"
    assert item["published_at"] == "2026-09-20T10:15:00+00:00"
    assert item["views"] == 900 and item["image_url"] is None
    assert message_to_item(FakeMessage(56, None), SOURCE) is None
    assert message_to_item(FakeMessage(57, "коротко"), SOURCE) is None


def test_fetch_reads_channels_and_disconnects(monkeypatch):
    client = FakeClient([FakeMessage(1, "Первый пост о рынке недвижимости"),
                         FakeMessage(2, "фото"),
                         FakeMessage(3, "Второй пост: новый лонч на набережной")])
    monkeypatch.setattr(telegram_private, "_open_client", lambda settings: client)
    items = fetch_private([SOURCE, {"id": "x", "type": "rss"}], configured(), limit=10)
    assert [i["url"] for i in items] == ["https://t.me/c/1234567890/1", "https://t.me/c/1234567890/3"]
    assert client.calls == [1234567890]
    assert client.disconnected


def test_fetch_without_session_is_a_noop_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert fetch_private([SOURCE], Settings(dry_run=False)) == []
    assert "TELEGRAM_SESSION" in caplog.text
    assert not is_configured(Settings(dry_run=False))


def test_expired_session_does_not_crash_the_run(monkeypatch, caplog):
    client = FakeClient([], authorized=False)
    monkeypatch.setattr(telegram_private, "_open_client", lambda settings: client)
    with caplog.at_level(logging.WARNING):
        assert fetch_private([SOURCE], configured()) == []
    assert "не авторизована" in caplog.text
    assert client.disconnected


def test_collect_skips_private_type_in_the_http_loop(monkeypatch, settings):
    settings.dry_run = False
    monkeypatch.setattr(collect, "fetch_private", lambda active, s, limit: [{"source_id": "tg_private_demo"}])
    calls = []
    monkeypatch.setattr(collect, "_http_get", lambda url, **kw: calls.append(url) or (200, b"", ""))
    doc = {"defaults": {}, "categories": {"realty_news": {}}, "sources": [SOURCE]}
    raw = collect.collect(settings, doc)
    assert raw == [{"source_id": "tg_private_demo"}]
    assert calls == [], "приватный канал не должен запрашиваться по HTTP"


def test_verify_reports_missing_id_and_missing_session(monkeypatch):
    verdict = verify_sources.check_private_source({**SOURCE, "channel_id": None}, configured())
    assert verdict["status"] == "failed" and "channel_id" in verdict["note"]
    verdict = verify_sources.check_private_source(SOURCE, Settings(dry_run=False))
    assert verdict["status"] == "failed" and "TELEGRAM_SESSION" in verdict["note"]
    monkeypatch.setattr(verify_sources, "_now", lambda: "2026-09-21T00:00:00Z")
    monkeypatch.setattr(telegram_private, "_open_client", lambda s: FakeClient(
        [FakeMessage(9, "Живой пост из закрытого канала", datetime(2026, 9, 21, tzinfo=timezone.utc))]))
    verdict = verify_sources.check_private_source(SOURCE, configured())
    assert verdict["status"] == "ok" and verdict["items_found"] == 1
    assert verdict["latest_item_at"] == "2026-09-21T00:00:00+00:00"


def test_registry_placeholder_is_disabled_and_has_no_invite_link():
    doc = load_sources()
    private = [s for s in doc["sources"] if s["type"] == "telegram_private"]
    assert private, "в реестре нет заготовки приватного канала"
    for s in private:
        assert not s["enabled"] and s["channel_id"] is None
        assert "t.me/+" not in s["url"]
    bad = dict(private[0], enabled=True)
    with pytest.raises(ValueError, match="channel_id"):
        validate_sources_doc({**doc, "sources": [bad]})


def test_session_and_hash_are_treated_as_secrets():
    s = configured()
    assert s.telegram_session in s.secret_values
    assert s.telegram_api_hash in s.secret_values


def test_destination_channel_falls_back_to_settings_file(monkeypatch):
    for name in ("TELEGRAM_CHANNEL_ID",):
        monkeypatch.delenv(name, raising=False)
    from pipeline.config import load_settings

    assert load_settings().telegram_channel_id == "@realnew_mary"
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@test_override")
    assert load_settings().telegram_channel_id == "@test_override"
