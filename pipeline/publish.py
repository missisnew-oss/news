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

from . import hosting, illustrate
from . import postqueue, state
from .config import Settings
from .generate import compose_text
from .telegram import TelegramClient
from .config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT
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
    file_id = post.get("telegram_file_id")
    long_text = len(text) > TG_CAPTION_LIMIT
    # A long post needs a local card to host (pipeline/hosting.py); a short one
    # may reuse the Telegram file_id from the preview upload.
    image_path = illustrate.ensure_image(settings, post) if (long_text or not file_id) else None
    preview_url = None
    if long_text and (image_path or file_id) and len(text) <= TG_MESSAGE_LIMIT - 80:
        preview_url = hosting.host_card(image_path, post.get("post_id", "post"), settings)
    plan = plan_delivery(text, has_photo=bool(file_id or image_path))
    if preview_url:
        # One message: the card as a large preview above the whole text.
        plan = {"mode": "text_with_preview", "caption": None, "texts": [hosting.invisible_link(preview_url) + text]}
    message_ids: list[int] = []

    try:
        if plan["mode"] == "text_with_preview":
            response = client.send_message(settings.telegram_channel_id, plan["texts"][0], preview_url=preview_url)
            message_ids.append((response.get("result") or {}).get("message_id"))
        elif plan["mode"] == "photo":
            response = client.send_photo(
                settings.telegram_channel_id, image_path, file_id=file_id,
                caption=plan["caption"] or "",
            )
            message_ids.append((response.get("result") or {}).get("message_id"))
        elif plan["mode"] == "photo_plus_text":
            response = client.send_photo(
                settings.telegram_channel_id, image_path, file_id=file_id,
                caption=plan["caption"] or "",
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
        sent = [m for m in message_ids if m]
        if sent:
            # Part of the post is already in the channel (typically the photo
            # went out and the follow-up text did not). Releasing the key here
            # would make the next run send the photo a second time, so the
            # claim stays and the post is marked for manual attention.
            record = {
                "post_id": post.get("post_id"),
                "publish_key": key,
                "rubric": post.get("rubric"),
                "message_ids": sent,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "mode": plan["mode"],
                "partial": True,
                "error": str(exc),
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
            log.error(
                "Пост %s ушёл частично (%d сообщений). Повтора не будет, "
                "чтобы не задвоить. Допубликуйте хвост руками.",
                post.get("post_id"), len(sent),
            )
            return {"post_id": post.get("post_id"), "published": False,
                    "partial": True, "record": record, "error": str(exc)}
        # Nothing left the process: release the claim so the next run retries.
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


def republish(settings: Settings, post_id: str, *, client: TelegramClient | None = None,
              persist: bool = True) -> dict[str, Any]:
    """Delete the post's messages from the channel and publish it again now.

    For a post that went out in a shape the owner rejects (photo plus a
    separate text): the old messages are removed, the publish key released,
    and the post sent again with the current delivery rules.
    """
    client = client or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    queue = postqueue.load_queue()
    published = state.load("published.json")
    post = next((p for p in (queue.get("posts") or []) if p.get("post_id") == post_id), None)
    if post is None:
        raise ValueError(f"пост {post_id} не найден в очереди")
    records = [r for r in (published.get("posts") or []) if r.get("post_id") == post_id]
    for record in records:
        for message_id in record.get("message_ids") or []:
            try:
                client.delete_message(settings.telegram_channel_id, int(message_id))
            except Exception as exc:
                log.warning("Сообщение %s не удалено: %s", message_id, exc)
        published["keys"] = [k for k in (published.get("keys") or []) if k != record.get("publish_key")]
    published["posts"] = [r for r in (published.get("posts") or []) if r.get("post_id") != post_id]
    post["status"] = "approved"
    post["slot_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    post.pop("telegram_file_id", None)
    if persist:
        state.save("published.json", published)
    result = publish_one(settings, post, client, published, persist=persist)
    if result.get("published"):
        post["status"] = "published"
        post["publish"] = result.get("record")
    if persist:
        postqueue.save_queue(queue)
    log.info("Пост %s перевыпущен: удалено %d старых записей", post_id, len(records))
    return result


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
