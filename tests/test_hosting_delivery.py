"""One Telegram message for a long post: hosted card as a preview above the text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import approve, hosting, publish
from pipeline.config import Settings
from pipeline.telegram import TelegramClient


def _card(tmp_path: Path) -> Path:
    from PIL import Image

    path = tmp_path / "card.png"
    Image.new("RGB", (1200, 630), (247, 244, 238)).save(path)
    return path


def test_host_card_commits_and_returns_a_raw_url(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(hosting, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "missisnew-oss/news")
    committed = {}

    def fake_commit(rel, message):
        committed["rel"], committed["msg"] = rel, message
        return "abc123"

    url = hosting.host_card(_card(tmp_path), "dubai_life-1", Settings(dry_run=False),
                            commit=fake_commit, reachable=lambda u: True)
    assert url.startswith("https://raw.githubusercontent.com/missisnew-oss/news/abc123/cards/dubai_life-1-")
    assert committed["rel"].startswith("cards/dubai_life-1-") and "[skip ci]" not in committed["msg"]
    assert (tmp_path / "cards").is_dir()


def test_host_card_is_none_in_dry_run_or_when_git_or_url_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(hosting, "ROOT", tmp_path)
    card = _card(tmp_path)
    assert hosting.host_card(card, "p", Settings(dry_run=True), commit=lambda r, m: "x", reachable=lambda u: True) is None
    assert hosting.host_card(card, "p", Settings(dry_run=False), commit=lambda r, m: None, reachable=lambda u: True) is None
    assert hosting.host_card(card, "p", Settings(dry_run=False), commit=lambda r, m: "x", reachable=lambda u: False) is None
    assert hosting.host_card(None, "p", Settings(dry_run=False)) is None


def test_send_message_puts_the_preview_above_the_text():
    client = TelegramClient("t", dry_run=True)
    client.send_message("@c", "text", preview_url="https://example.com/c.png")
    payload = client.calls[-1]["payload"]
    options = json.loads(payload["link_preview_options"])
    assert options == {"url": "https://example.com/c.png", "prefer_large_media": True, "show_above_text": True}
    client.send_message("@c", "text")
    assert json.loads(client.calls[-1]["payload"]["link_preview_options"]) == {"is_disabled": True}


class _Channel(TelegramClient):
    def __init__(self):
        super().__init__("t", dry_run=True)
        self.sent = []

    def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", text, kw.get("preview_url")))
        return {"ok": True, "result": {"message_id": 100 + len(self.sent)}}

    def send_photo(self, chat_id, photo_path=None, *, caption="", reply_markup=None, file_id=None):
        self.sent.append(("photo", caption, None))
        return {"ok": True, "result": {"message_id": 100 + len(self.sent)}}

    def delete_message(self, chat_id, message_id):
        self.sent.append(("delete", message_id, None))
        return {"ok": True, "result": True}


def _long_post(tmp_path):
    return {"post_id": "dubai_life-1", "rubric": "dubai_life", "status": "approved",
            "title": "Dubizzle", "body": "Абзац про рынок и аренду. " * 60, "cta": "", "hashtags": [],
            "slot_at": "2020-01-01T00:00:00+00:00", "image_path": str(_card(tmp_path)),
            "image_meta": {"provider": "own_card", "headline": "Dubizzle", "accent": ""}}


def test_long_post_goes_out_as_one_message_when_the_card_is_hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "host_card", lambda path, pid, settings: "https://raw.githubusercontent.com/o/r/s/cards/x.png")
    settings = Settings(dry_run=False, telegram_bot_token="t", telegram_channel_id="@c")
    client = _Channel()
    published = {"version": 1, "posts": [], "keys": []}
    result = publish.publish_one(settings, _long_post(tmp_path), client, published, persist=False)
    assert result["published"] and result["record"]["mode"] == "text_with_preview"
    assert len(client.sent) == 1 and client.sent[0][0] == "text"
    assert client.sent[0][1].startswith('<a href="https://raw.githubusercontent.com/o/r/s/cards/x.png">')
    assert client.sent[0][2] == "https://raw.githubusercontent.com/o/r/s/cards/x.png"


def test_long_post_falls_back_to_photo_plus_text_without_hosting(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "host_card", lambda path, pid, settings: None)
    settings = Settings(dry_run=False, telegram_bot_token="t", telegram_channel_id="@c")
    client = _Channel()
    result = publish.publish_one(settings, _long_post(tmp_path), client, published={"version": 1, "posts": [], "keys": []},
                                 persist=False)
    assert result["record"]["mode"] == "photo_plus_text"
    assert [s[0] for s in client.sent] == ["photo", "text"]


def test_republish_deletes_the_old_messages_and_sends_again(tmp_path, monkeypatch):
    from pipeline import postqueue, state

    post = {**_long_post(tmp_path), "status": "published"}
    queue = {"version": 1, "posts": [post]}
    published = {"version": 1, "keys": ["k1"], "posts": [{"post_id": "dubai_life-1", "publish_key": "k1",
                                                          "message_ids": [3, 4]}]}
    monkeypatch.setattr(postqueue, "load_queue", lambda: queue)
    monkeypatch.setattr(postqueue, "save_queue", lambda q: None)
    monkeypatch.setattr(state, "load", lambda name: published)
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "host_card", lambda path, pid, settings: "https://raw.githubusercontent.com/o/r/s/cards/x.png")
    settings = Settings(dry_run=False, telegram_bot_token="t", telegram_channel_id="@c")
    client = _Channel()
    result = publish.republish(settings, "dubai_life-1", client=client, persist=False)
    assert [s[:2] for s in client.sent[:2]] == [("delete", 3), ("delete", 4)]
    assert client.sent[2][0] == "text" and result["published"]
    assert post["status"] == "published" and "k1" not in published["keys"]
    assert [r["post_id"] for r in published["posts"]] == ["dubai_life-1"] and published["posts"][0]["mode"] == "text_with_preview"


def test_long_preview_is_one_message_when_hosted(settings, tmp_path, monkeypatch):
    from pipeline import illustrate

    monkeypatch.setattr(illustrate, "GENERATED_DIR", tmp_path, raising=False)
    monkeypatch.setattr(hosting, "host_card", lambda path, pid, settings: "https://raw.githubusercontent.com/o/r/s/cards/x.png")
    post = {"post_id": "p1", "rubric": "dubai_life", "title": "Заголовок", "body": "Абзац. " * 200,
            "status": "queued", "slot_at": None, "approval": {}, "source_items": [],
            "image_meta": {"provider": "own_card", "headline": "Заголовок", "accent": ""}}
    client = _Channel()
    assert approve.send_previews(settings, client=client, queue={"posts": [post]}, persist=False) == 1
    assert len(client.sent) == 1 and client.sent[0][0] == "text" and client.sent[0][2]
    assert post["approval"]["preview_message_id"] == 101
