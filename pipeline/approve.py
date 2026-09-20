"""Stage 7 — APPROVE: preview to the owner, buttons, long-poll for answers.

A webhook needs a permanently reachable host, which a GitHub Actions runner is
not, so approval uses ``getUpdates`` long-polling from a short-lived workflow.
Two consequences are handled here:

  * ``getUpdates`` and a webhook are mutually exclusive — the workflow deletes
    any webhook before polling;
  * the update offset must survive between runs, so it is persisted in
    ``state/telegram_offset.json``.

Only ``TELEGRAM_OWNER_ID`` may press the buttons; anything else is ignored.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import postqueue, state
from .config import Settings
from .generate import compose_text
from .telegram import TelegramClient, approval_keyboard
from .textutil import plan_delivery, truncate

log = logging.getLogger("pipeline.approve")

ACTIONS = {"ok": "approved", "no": "rejected", "later": "postponed", "redo": "rewrite"}


def preview_text(post: dict[str, Any]) -> str:
    gate = post.get("gate") or {}
    warnings = gate.get("warnings") or []
    sources = post.get("sources") or []
    lines = [
        f"<b>Черновик: {post.get('rubric')}</b>",
        f"Слот: {post.get('slot_at') or 'не назначен'} (UTC)",
        f"Длина: {post.get('length_chars', 0)} симв.",
        f"Источников: {len(sources)}",
    ]
    if warnings:
        lines.append("⚠️ " + "; ".join(warnings[:2]))
    lines.append("—" * 12)
    lines.append(truncate(compose_text(post), 2500))
    return "\n".join(lines)


def send_previews(settings: Settings, client: TelegramClient | None = None,
                  queue: dict[str, Any] | None = None, *, persist: bool = True) -> int:
    """Send every not-yet-previewed queued post to the owner. Returns count."""
    if not settings.telegram_owner_id:
        log.warning("TELEGRAM_OWNER_ID не задан — превью отправить некому")
        return 0
    client = client or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    queue = queue if queue is not None else postqueue.load_queue()

    sent = 0
    for post in postqueue.pending_approval(queue):
        keyboard = approval_keyboard(post["post_id"])
        image_path = post.get("image_path")
        try:
            if image_path:
                response = client.send_photo(
                    settings.telegram_owner_id,
                    image_path,
                    caption=truncate(preview_text(post), 1024),
                    reply_markup=keyboard,
                )
            else:
                response = client.send_message(
                    settings.telegram_owner_id, preview_text(post), reply_markup=keyboard
                )
        except Exception as exc:
            log.error("Не удалось отправить превью %s: %s", post["post_id"], exc)
            continue
        post.setdefault("approval", {})["sent_at"] = datetime.now(timezone.utc).isoformat()
        post["approval"]["preview_message_id"] = (response.get("result") or {}).get("message_id")
        sent += 1
    if persist:
        postqueue.save_queue(queue)
    log.info("Отправлено превью: %d", sent)
    return sent


def poll_once(settings: Settings, client: TelegramClient | None = None,
              queue: dict[str, Any] | None = None, *, persist: bool = True,
              poll_timeout: int = 25) -> list[dict[str, Any]]:
    """One long-poll round. Returns the decisions applied."""
    client = client or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    queue = queue if queue is not None else postqueue.load_queue()
    offset_state = state.load("telegram_offset.json")
    offset = int(offset_state.get("offset") or 0)

    updates = client.get_updates(offset, timeout=poll_timeout)
    decisions: list[dict[str, Any]] = []
    owner = str(settings.telegram_owner_id or "")

    for update in updates:
        offset = max(offset, int(update.get("update_id", 0)) + 1)
        callback = update.get("callback_query")
        if not callback:
            continue
        from_id = str(((callback.get("from") or {}).get("id")) or "")
        data = str(callback.get("data") or "")
        if owner and from_id != owner:
            log.warning("Проигнорирована кнопка от постороннего пользователя %s", from_id)
            try:
                client.answer_callback(callback.get("id", ""), "Недостаточно прав")
            except Exception:
                pass
            continue
        action, _, post_id = data.partition(":")
        if action not in ACTIONS or not post_id:
            continue

        status = ACTIONS[action]
        if status == "postponed":
            postqueue.postpone(queue, post_id)
            applied = "postponed"
        elif status == "rewrite":
            postqueue.set_status(queue, post_id, "draft", approval={"sent_at": None, "action": "redo"})
            applied = "rewrite"
        else:
            postqueue.set_status(
                queue,
                post_id,
                status,
                approval={
                    "sent_at": (
                        (next((p for p in queue.get("posts", []) if p.get("post_id") == post_id), {})
                         .get("approval") or {}).get("sent_at")
                    ),
                    "decided_at": datetime.now(timezone.utc).isoformat(),
                    "by": from_id,
                    "action": action,
                },
            )
            applied = status

        decisions.append({"post_id": post_id, "action": action, "status": applied})
        try:
            client.answer_callback(callback.get("id", ""), f"Принято: {applied}")
            message = callback.get("message") or {}
            if message.get("message_id"):
                client.edit_reply_markup(
                    settings.telegram_owner_id, message["message_id"], {"inline_keyboard": []}
                )
        except Exception as exc:
            log.warning("Не удалось подтвердить нажатие: %s", exc)

    offset_state["offset"] = offset
    offset_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    if persist:
        state.save("telegram_offset.json", offset_state)
        postqueue.save_queue(queue)
    log.info("Обработано обновлений: %d, решений: %d", len(updates), len(decisions))
    return decisions


def run(settings: Settings, *, rounds: int = 1, poll_timeout: int = 25) -> list[dict[str, Any]]:
    client = TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    if not settings.dry_run:
        # getUpdates and a webhook cannot coexist.
        try:
            client.call("deleteWebhook", {"drop_pending_updates": "false"})
        except Exception as exc:
            log.warning("deleteWebhook не выполнен: %s", exc)
    queue = postqueue.load_queue()
    send_previews(settings, client=client, queue=queue, persist=True)
    decisions: list[dict[str, Any]] = []
    for _ in range(max(1, rounds)):
        queue = postqueue.load_queue()
        decisions.extend(poll_once(settings, client=client, queue=queue, poll_timeout=poll_timeout))
    return decisions
