"""Stage 8 — PUBLISH: send approved posts to the channel at their slot.

Idempotency is the hard requirement: a workflow that is retried, or two
overlapping scheduled runs, must not post twice. Every post carries a
``publish_key`` (post_id + slot). The key is written into
``state/published.json`` BEFORE the API call and the file is flushed to disk,
so a crash between the call and the bookkeeping fails closed (a skipped post)
rather than open (a duplicate).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import postqueue, state
from .config import Settings
from .generate import compose_text
from .telegram import TelegramClient
from .textutil import plan_delivery, sha1

log = logging.getLogger("pipeline.publish")


def publish_key(post: dict[str, Any]) -> str:
    return sha1(f"{post.get('post_id')}|{post.get('slot_at') or ''}")


def already_published(published: dict[str, Any], key: str) -> bool:
    return key in set(published.get("keys") or [])


def publish_one(settings: Settings, post: dict[str, Any], client: TelegramClient,
                published: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    key = publish_key(post)
    if already_published(published, key):
        log.info("Пост %s уже опубликован (ключ %s), пропускаем", post.get("post_id"), key[:8])
        return {"post_id": post.get("post_id"), "skipped": True, "reason": "duplicate"}

    # Claim the key before touching the network: fail closed, never twice.
    published.setdefault("keys", []).append(key)
    if persist:
        state.save("published.json", published)

    text = compose_text(post)
    plan = plan_delivery(text, has_photo=bool(post.get("image_path")))
    message_ids: list[int] = []

    try:
        if plan["mode"] == "photo":
            response = client.send_photo(
                settings.telegram_channel_id, post["image_path"], caption=plan["caption"] or ""
            )
            message_ids.append((response.get("result") or {}).get("message_id"))
        elif plan["mode"] == "photo_plus_text":
            response = client.send_photo(
                settings.telegram_channel_id, post["image_path"], caption=plan["caption"] or ""
            )
            message_ids.append((response.get("result") or {}).get("message_id"))
            for chunk in plan["texts"]:
                response = client.send_message(settings.telegram_channel_id, chunk)
                message_ids.append((response.get("result") or {}).get("message_id"))
        else:
            for chunk in plan["texts"]:
                response = client.send_message(settings.telegram_channel_id, chunk)
                message_ids.append((response.get("result") or {}).get("message_id"))
    except Exception as exc:
        log.error("Публикация %s не удалась: %s", post.get("post_id"), exc)
        # Release the claim so the next run can retry.
        published["keys"] = [k for k in published.get("keys", []) if k != key]
        if persist:
            state.save("published.json", published)
        return {"post_id": post.get("post_id"), "published": False, "error": str(exc)}

    record = {
        "post_id": post.get("post_id"),
        "publish_key": key,
        "rubric": post.get("rubric"),
        "message_ids": [m for m in message_ids if m],
        "published_at": datetime.now(timezone.utc).isoformat(),
        "mode": plan["mode"],
        "length_chars": post.get("length_chars", 0),
        "has_photo": bool(post.get("image_path")),
        "has_cta": bool(post.get("cta")),
        "source_ids": [s.get("source_id") for s in (post.get("sources") or [])],
        "image_meta": post.get("image_meta") or {},
        "dry_run": settings.dry_run,
    }
    published.setdefault("posts", []).append(record)
    if persist:
        state.save("published.json", published)
    log.info("Опубликован %s (%s), сообщений: %d", record["post_id"], plan["mode"], len(message_ids))
    return {"post_id": record["post_id"], "published": True, "record": record}


def run(settings: Settings, *, now: datetime | None = None, persist: bool = True) -> list[dict[str, Any]]:
    settings.require_telegram() if not settings.dry_run else None
    client = TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    queue = postqueue.load_queue()
    published = state.load("published.json")

    results: list[dict[str, Any]] = []
    for post in postqueue.due_for_publication(queue, now=now):
        result = publish_one(settings, post, client, published, persist=persist)
        if result.get("published"):
            post["status"] = "published"
            post["publish_key"] = result["record"]["publish_key"]
        elif result.get("skipped"):
            post["status"] = "published"
        results.append(result)

    if persist:
        postqueue.save_queue(queue)
        _record_analytics(published, results)
    return results


def _record_analytics(published: dict[str, Any], results: list[dict[str, Any]]) -> None:
    analytics = state.load("analytics.json")
    known = {p.get("post_id") for p in (analytics.get("posts") or [])}
    for result in results:
        record = result.get("record")
        if not record or record["post_id"] in known:
            continue
        analytics.setdefault("posts", []).append({
            "post_id": record["post_id"],
            "message_id": (record["message_ids"] or [None])[0],
            "published_at": record["published_at"],
            "rubric": record["rubric"],
            "source_ids": record["source_ids"],
            "length_chars": record["length_chars"],
            "has_cta": record["has_cta"],
            "has_photo": record["has_photo"],
            "metrics": {"views": None, "reactions": None, "forwards": None,
                        "collected_at": None, "manual": True},
        })
    state.save("analytics.json", analytics)
