"""A retried workflow must never publish the same post twice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline import postqueue, publish, state
from pipeline.models import PostDraft
from pipeline.telegram import TelegramClient


def _post(post_id="market_pulse-abc123", slot=None):
    return {
        "post_id": post_id,
        "rubric": "market_pulse",
        "title": "Заголовок",
        "body": "Текст поста без цифр.",
        "hashtags": ["#дубай"],
        "cta": "Напишите мне.",
        "sources": [{"source_id": "demo", "title": "t", "url": "https://example.com/a"}],
        "status": "approved",
        "slot_at": slot,
        "image_path": None,
        "length_chars": 25,
    }


def test_publish_key_is_stable():
    post = _post(slot="2026-09-21T05:30:00+00:00")
    assert publish.publish_key(post) == publish.publish_key(dict(post))


def test_second_publish_is_skipped(settings):
    client = TelegramClient("x", dry_run=True)
    published = {"version": 1, "posts": [], "keys": []}
    post = _post()

    first = publish.publish_one(settings, post, client, published, persist=False)
    assert first["published"] is True
    calls_after_first = len(client.calls)

    second = publish.publish_one(settings, post, client, published, persist=False)
    assert second.get("skipped") is True
    assert len(client.calls) == calls_after_first, "второй вызов не должен обращаться к API"
    assert len(published["posts"]) == 1


def test_failed_publish_releases_the_key(settings, monkeypatch):
    client = TelegramClient("x", dry_run=True)
    published = {"version": 1, "posts": [], "keys": []}

    def boom(*args, **kwargs):
        raise RuntimeError("Telegram down")

    monkeypatch.setattr(client, "send_message", boom)
    result = publish.publish_one(settings, _post(), client, published, persist=False)
    assert result["published"] is False
    assert published["keys"] == [], "ключ должен освобождаться, чтобы ретрай был возможен"


def test_enqueue_is_idempotent_by_post_id():
    draft = PostDraft(post_id="market_pulse-zzz", rubric="market_pulse", title="t", body="b")
    postqueue.enqueue([draft], persist=False)
    queue = postqueue.enqueue([draft], persist=False)
    ids = [p["post_id"] for p in queue["posts"]]
    assert ids.count("market_pulse-zzz") == 1


def test_enqueue_does_not_move_an_approved_post():
    draft = PostDraft(post_id="p1", rubric="market_pulse", title="t", body="b")
    queue = postqueue.enqueue([draft], persist=True)
    queue["posts"][0]["status"] = "approved"
    queue["posts"][0]["slot_at"] = "2026-09-21T05:30:00+00:00"
    postqueue.save_queue(queue)

    postqueue.enqueue([PostDraft(post_id="p1", rubric="market_pulse", title="НОВОЕ", body="b")], persist=True)
    reloaded = postqueue.load_queue()
    assert reloaded["posts"][0]["status"] == "approved"
    assert reloaded["posts"][0]["title"] == "t"


def test_due_for_publication_respects_the_slot():
    now = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    queue = {"posts": [
        _post("past", (now - timedelta(hours=1)).isoformat()),
        _post("future", (now + timedelta(hours=1)).isoformat()),
    ]}
    due = [p["post_id"] for p in postqueue.due_for_publication(queue, now=now)]
    assert due == ["past"]


def test_run_twice_publishes_once(settings):
    draft = PostDraft(post_id="market_pulse-run", rubric="market_pulse", title="t",
                      body="Текст поста.", status="approved")
    queue = postqueue.enqueue([draft], persist=True)
    queue["posts"][0]["status"] = "approved"
    queue["posts"][0]["slot_at"] = None
    postqueue.save_queue(queue)

    first = publish.run(settings)
    second = publish.run(settings)
    assert sum(1 for r in first if r.get("published")) == 1
    assert sum(1 for r in second if r.get("published")) == 0
    assert len(state.load("published.json")["posts"]) == 1
