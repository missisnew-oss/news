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

from . import illustrate, postqueue, state
from .config import Settings
from .generate import compose_text
from .telegram import TelegramClient, approval_keyboard
from .textutil import truncate_html

log = logging.getLogger("pipeline.approve")

ACTIONS = {"ok": "approved", "no": "rejected", "later": "postponed", "redo": "rewrite"}


def record_reactions(update: dict[str, Any], *, persist: bool = True) -> bool:
    """Store a message_reaction_count update into state/analytics.json.

    Bot API has no method to ask for a post's reactions: they arrive only as
    updates, carry absolute totals, and are gone forever if the 24-hour
    getUpdates window passes unread (see docs/ANALYTICS.md). So we overwrite,
    never accumulate, and we poll often.
    """
    payload = update.get("message_reaction_count")
    if not payload:
        return False
    message_id = payload.get("message_id")
    if not message_id:
        return False

    by_emoji: dict[str, int] = {}
    total = 0
    for entry in payload.get("reactions") or []:
        count = int(entry.get("total_count") or 0)
        total += count
        emoji = ((entry.get("type") or {}).get("emoji")
                 or (entry.get("type") or {}).get("custom_emoji_id")
                 or "other")
        by_emoji[emoji] = by_emoji.get(emoji, 0) + count

    analytics = state.load("analytics.json")
    touched = False
    for post in analytics.get("posts") or []:
        if post.get("message_id") == message_id:
            metrics = post.setdefault("metrics", {})
            metrics["reactions"] = total
            metrics["reactions_by_emoji"] = by_emoji
            metrics["reactions_updated_at"] = datetime.now(timezone.utc).isoformat()
            metrics["manual"] = metrics.get("views") is None
            touched = True
    if touched and persist:
        state.save("analytics.json", analytics)
    if not touched:
        log.debug("Реакции для message_id=%s: пост не найден в analytics.json", message_id)
    return touched


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
    lines.append(truncate_html(compose_text(post), 2500))
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
        file_id = post.get("telegram_file_id")
        image_path = None if file_id else illustrate.ensure_image(settings, post)
        try:
            if file_id or image_path:
                response = client.send_photo(
                    settings.telegram_owner_id,
                    image_path,
                    file_id=file_id,
                    caption=truncate_html(preview_text(post), 1024),
                    reply_markup=keyboard,
                )
                uploaded = TelegramClient.photo_file_id(response)
                if uploaded:
                    post["telegram_file_id"] = uploaded
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
    owner = str(settings.telegram_owner_id or "")
    if not owner:
        # Fail closed. Previously an unset owner id skipped the identity check
        # entirely, so anyone who found the bot could publish to the channel.
        log.error(
            "TELEGRAM_OWNER_ID не задан — кнопки апрува не обрабатываются. "
            "Задайте секрет TELEGRAM_OWNER_ID (docs/SETUP.md, шаг 6)."
        )
        return []

    client = client or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    queue = queue if queue is not None else postqueue.load_queue()
    offset_state = state.load("telegram_offset.json")
    offset = int(offset_state.get("offset") or 0)

    updates = client.get_updates(offset, timeout=poll_timeout)
    decisions: list[dict[str, Any]] = []

    try:
        for update in updates:
            try:
                decision = _apply_update(settings, client, queue, update, owner,
                                         persist=persist)
            except Exception as exc:
                # One malformed update must not cost us the whole batch's
                # offset, which would replay every update on the next run.
                log.warning("Обновление %s не обработано: %s", update.get("update_id"), exc)
                decision = None
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            if decision:
                decisions.append(decision)
    finally:
        offset_state["offset"] = offset
        offset_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if persist:
            state.save("telegram_offset.json", offset_state)
            postqueue.save_queue(queue)

    log.info("Обработано обновлений: %d, решений: %d", len(updates), len(decisions))
    return decisions


# A decision on a post in one of these states is final: the post is already in
# the channel or already cancelled. Telegram keeps old inline keyboards alive
# in the chat history, and an update can be redelivered after a crashed run,
# so the same button can legitimately arrive twice.
TERMINAL_STATUSES = {"published", "rejected"}


def _apply_update(settings: Settings, client: TelegramClient, queue: dict[str, Any],
                  update: dict[str, Any], owner: str, *, persist: bool) -> dict[str, Any] | None:
    log.info("Обновление %s: %s", update.get("update_id"), describe_update(update))
    if update.get("message_reaction_count"):
        record_reactions(update, persist=persist)
        return None
    message = update.get("message")
    if message:
        _answer_id_request(client, message, owner)
        return None
    callback = update.get("callback_query")
    if not callback:
        return None

    from_id = str(((callback.get("from") or {}).get("id")) or "")
    if from_id != owner:
        log.warning("Проигнорирована кнопка от постороннего пользователя %s", from_id)
        try:
            client.answer_callback(callback.get("id", ""), "Недостаточно прав")
        except Exception:
            pass
        return None

    action, _, post_id = str(callback.get("data") or "").partition(":")
    if action not in ACTIONS or not post_id:
        return None

    post = next((p for p in (queue.get("posts") or []) if p.get("post_id") == post_id), None)
    if post is None:
        _ack(client, settings, callback, "Пост не найден")
        return None
    if post.get("status") in TERMINAL_STATUSES:
        log.info("Кнопка %r по посту %s в статусе %s — решение уже принято",
                 action, post_id, post["status"])
        _ack(client, settings, callback,
             "Пост уже опубликован" if post["status"] == "published" else "Пост уже отклонён")
        return None

    status = ACTIONS[action]
    if status == "postponed":
        postqueue.postpone(queue, post_id)
        applied = "postponed"
    elif status == "rewrite":
        # "rewrite" is picked up by pipeline.run.stage_generate, which
        # regenerates the post from the items stored on it.
        postqueue.set_status(
            queue, post_id, "rewrite",
            approval={"sent_at": None, "action": "redo",
                      "requested_at": datetime.now(timezone.utc).isoformat()},
        )
        applied = "rewrite"
    else:
        postqueue.set_status(
            queue, post_id, status,
            approval={
                "sent_at": (post.get("approval") or {}).get("sent_at"),
                "decided_at": datetime.now(timezone.utc).isoformat(),
                "by": from_id,
                "action": action,
            },
        )
        applied = status

    _ack(client, settings, callback, f"Принято: {applied}")
    return {"post_id": post_id, "action": action, "status": applied}


def _log_bot_identity(client: TelegramClient, settings: Settings) -> None:
    """Say which bot this token belongs to and catch the classic mix-up.

    Owners often have several bots (BotFather, @userinfobot, their own) and
    write /id to the wrong one, or put the bot's own id into
    TELEGRAM_OWNER_ID. Both are invisible without this line in the log.
    """
    try:
        me = (client.call("getMe") or {}).get("result") or {}
    except Exception as exc:
        log.warning("getMe не выполнен: %s", exc)
        return
    username, bot_id = me.get("username"), str(me.get("id") or "")
    log.info("Бот: @%s (id %s) — команды /id и кнопки принимает именно он", username, bot_id)
    if bot_id and str(settings.telegram_owner_id) == bot_id:
        log.error(
            "TELEGRAM_OWNER_ID равен id самого бота (%s). Нужен ВАШ id: напишите @%s "
            "в личку /id и вставьте число из ответа в секрет.", bot_id, username
        )


def describe_update(update: dict[str, Any]) -> str:
    """One safe line per update for the Actions log: kind, chat type, sender id,
    and whether there was text — never the text itself."""
    kind = next((k for k in ("message", "callback_query", "channel_post",
                             "message_reaction_count", "my_chat_member") if k in update), "?")
    body = update.get(kind) if kind != "?" else {}
    body = body or {}
    chat = body.get("chat") or (body.get("message") or {}).get("chat") or {}
    sender = (body.get("from") or {}).get("id")
    text = body.get("text") or body.get("data") or ""
    return (f"тип={kind} чат={chat.get('type', '?')} от={sender} "
            f"текст={'есть (' + str(text)[:12] + '…)' if text else 'нет'}")


ID_COMMANDS = ("/start", "/id")


def _answer_id_request(client: TelegramClient, message: dict[str, Any], owner: str) -> None:
    """Tell a private-chat sender their numeric id.

    The owner has to put their own user id into TELEGRAM_OWNER_ID, and the
    number people copy from third-party bots is often the wrong one (the
    bot's own id, a chat id). Replying with the id from inside this bot gives
    them the exact value; it reveals nothing but the sender's own id.
    """
    chat = message.get("chat") or {}
    text = str(message.get("text") or "").strip().lower()
    if chat.get("type") != "private" or not text.startswith(ID_COMMANDS):
        return
    from_id = str(((message.get("from") or {}).get("id")) or "")
    if not from_id:
        return
    if from_id == owner:
        reply = (f"Ваш ID: <code>{from_id}</code>. Он уже прописан как владелец — "
                 "превью постов будут приходить сюда.")
    else:
        reply = (f"Ваш ID: <code>{from_id}</code>.\n\nВставьте это число в секрет "
                 "<b>TELEGRAM_OWNER_ID</b> в GitHub (Settings → Secrets and variables → "
                 "Actions), и превью постов начнут приходить в этот чат.")
    try:
        client.send_message(str(chat.get("id") or from_id), reply)
    except Exception as exc:
        log.warning("Не удалось ответить на %s: %s", text.split()[0], exc)


def _ack(client: TelegramClient, settings: Settings, callback: dict[str, Any], text: str) -> None:
    """Answer the callback and take the buttons off the preview message."""
    try:
        client.answer_callback(callback.get("id", ""), text)
        message = callback.get("message") or {}
        if message.get("message_id"):
            client.edit_reply_markup(
                settings.telegram_owner_id, message["message_id"], {"inline_keyboard": []}
            )
    except Exception as exc:
        log.warning("Не удалось подтвердить нажатие: %s", exc)


def run(settings: Settings, *, rounds: int = 1, poll_timeout: int = 25) -> list[dict[str, Any]]:
    client = TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
    if not settings.dry_run:
        # getUpdates and a webhook cannot coexist.
        try:
            client.call("deleteWebhook", {"drop_pending_updates": "false"})
        except Exception as exc:
            log.warning("deleteWebhook не выполнен: %s", exc)
        _log_bot_identity(client, settings)
    queue = postqueue.load_queue()
    send_previews(settings, client=client, queue=queue, persist=True)
    decisions: list[dict[str, Any]] = []
    for _ in range(max(1, rounds)):
        queue = postqueue.load_queue()
        decisions.extend(poll_once(settings, client=client, queue=queue, poll_timeout=poll_timeout))
    return decisions
