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
from .config import TG_MESSAGE_LIMIT, Settings
from .generate import compose_text
from .telegram import TelegramClient, approval_keyboard
from .textutil import sanitize_telegram_html, truncate_html

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


def dubai_time(iso: str | None) -> str:
    """``2026-09-23T15:30:00+00:00`` → ``23.09 в 19:30`` (Asia/Dubai)."""
    if not iso:
        return "ближайший свободный слот"
    from zoneinfo import ZoneInfo

    try:
        moment = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo("Asia/Dubai")).strftime("%d.%m в %H:%M")


STATUS_COMMANDS = ("статус", "очередь", "/status", "/queue", "что в очереди", "статус очереди")

STATUS_LABELS = {
    "approved": "✅ одобрено, выйдет",
    "queued": "⏳ ждёт вашего решения",
    "postponed": "🕒 отложено, выйдет",
    "rewrite": "✍️ переписывается",
}


def status_report(queue: dict[str, Any], *, now: datetime | None = None) -> str:
    """What is in the queue, in the owner's words and Dubai time.

    Sent when the owner writes «статус» to the bot: she could not tell which
    taps had been applied and believed approved posts were not going out.
    """
    now = now or datetime.now(timezone.utc)
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in STATUS_LABELS}
    published = []
    for post in queue.get("posts") or []:
        status = post.get("status")
        if status in groups:
            groups[status].append(post)
        elif status == "published":
            published.append(post)
    lines = [f"<b>Очередь на {dubai_time(now.isoformat())} по Дубаю</b>"]
    for status in ("approved", "postponed", "queued", "rewrite"):
        for post in sorted(groups[status], key=lambda p: p.get("slot_at") or ""):
            title = truncate_html(post.get("title") or post.get("post_id") or "", 70)
            label = STATUS_LABELS[status]
            when = f" {dubai_time(post.get('slot_at'))}" if status in {"approved", "postponed"} else ""
            lines.append(f"{label}{when}: «{title}»")
    if len(lines) == 1:
        lines.append("Пусто: ни одного поста на решении и ни одного одобренного.")
    recent = sorted(published, key=lambda p: str((p.get("publish") or {}).get("published_at") or p.get("published_at") or ""))[-3:]
    if recent:
        lines.append("")
        lines.append("Последние опубликованные: " + "; ".join(
            f"«{truncate_html(p.get('title') or '', 50)}»" for p in recent))
    lines.append("")
    lines.append("Одобренные посты выходят в свой слот: 09:30 или 19:30 по Дубаю. "
                 "Пост, отправленный на переписывание, после нового превью нужно одобрить заново.")
    return "\n".join(lines)


def preview_header(post: dict[str, Any]) -> str:
    gate = post.get("gate") or {}
    warnings = gate.get("warnings") or []
    sources = post.get("sources") or []
    lines = [
        f"<b>Черновик: {post.get('rubric')}</b>",
        f"Выйдет {dubai_time(post.get('slot_at'))} по Дубаю, если одобрить",
        f"Длина: {post.get('length_chars', 0)} симв.",
        f"Источников: {len(sources)}",
    ]
    if int(post.get("rewrite_count") or 0) > 0:
        lines.append(f"✍️ Переписанный вариант №{int(post['rewrite_count'])}. "
                     "Прежние нажатия не считаются: чтобы он вышел, нажмите «Опубликовать» здесь.")
    if warnings:
        lines.append("⚠️ " + "; ".join(warnings[:2]))
    return "\n".join(lines)


def preview_body(post: dict[str, Any]) -> str:
    return "\n".join([truncate_html(compose_text(post), 3500), "—" * 12, EDIT_HINT])


def preview_text(post: dict[str, Any]) -> str:
    return "\n".join([preview_header(post), "—" * 12, preview_body(post)])


# Telegram caps a photo caption at 1024 characters. A preview above that used
# to be cut mid-sentence and the owner read it as «пост не дописан»: now the
# photo carries the header only and the full text follows as a message.
PREVIEW_CAPTION_LIMIT = 1024


# Shown under every preview. The buttons are read by a scheduled poll, not a
# live bot, so the owner needs a way to hand over her own text as well.
EDIT_HINT = ("✏️ Поправить: ответьте на это сообщение своим текстом — он заменит пост. "
             "«дополни: …» — вплету дополнение в этот пост. "
             "«переписать: короче, без цифр» — перепишу с учётом пожелания.")

# A reply that starts with one of these is an instruction for the robot,
# anything else the owner replies is her own final text.
REWRITE_PREFIXES = ("переписать", "перепиши", "переделай", "переделать")
# …and one of these means «add this to the post»: the material is merged in
# and the post is rewritten as one text.
MERGE_PREFIXES = ("дополни", "дополнить", "добавь", "добавить", "к посту", "+")


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
        photo_message_id = None
        try:
            full = preview_text(post)
            if (file_id or image_path) and len(full) <= PREVIEW_CAPTION_LIMIT:
                response = client.send_photo(
                    settings.telegram_owner_id, image_path, file_id=file_id,
                    caption=full, reply_markup=keyboard,
                )
                uploaded = TelegramClient.photo_file_id(response)
                if uploaded:
                    post["telegram_file_id"] = uploaded
            elif file_id or image_path:
                photo = client.send_photo(
                    settings.telegram_owner_id, image_path, file_id=file_id,
                    caption=truncate_html(preview_header(post) + "\n⬇️ Полный текст поста — следующим сообщением",
                                          PREVIEW_CAPTION_LIMIT),
                )
                uploaded = TelegramClient.photo_file_id(photo)
                if uploaded:
                    post["telegram_file_id"] = uploaded
                photo_message_id = (photo.get("result") or {}).get("message_id")
                response = client.send_message(
                    settings.telegram_owner_id, truncate_html(preview_body(post), TG_MESSAGE_LIMIT),
                    reply_markup=keyboard,
                )
            else:
                response = client.send_message(
                    settings.telegram_owner_id, truncate_html(full, TG_MESSAGE_LIMIT), reply_markup=keyboard
                )
        except Exception as exc:
            log.error("Не удалось отправить превью %s: %s", post["post_id"], exc)
            continue
        post.setdefault("approval", {})["sent_at"] = datetime.now(timezone.utc).isoformat()
        post["approval"]["preview_message_id"] = (response.get("result") or {}).get("message_id")
        post["approval"]["preview_photo_message_id"] = photo_message_id
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
        if _answer_status_request(client, queue, message, owner):
            return None
        edited = _apply_preview_reply(client, queue, message, owner)
        if edited:
            return edited
        if not _store_owner_material(client, message, owner, persist=persist):
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
    was_approved = post.get("status") == "approved"
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
        if status == "approved":
            # Out at the next free slot, not at the one the draft was parked on.
            earliest = postqueue.earliest_free_slot(queue, exclude_post_id=post_id)
            if not post.get("slot_at") or str(post.get("slot_at")) > earliest:
                post["slot_at"] = earliest

    _ack(client, settings, callback, f"Принято: {APPLIED_TEXT.get(applied, applied)}")
    note = APPLIED_TEXT.get(applied, applied)
    if applied in {"approved", "postponed"}:
        note = f"{note} {dubai_time(post.get('slot_at'))} по Дубаю"
    if applied == "rewrite" and was_approved:
        note = ("пост был одобрен, но это нажатие «Переписать» отменяет публикацию: " + note)
    try:
        client.send_message(settings.telegram_owner_id,
                            f"«{truncate_html(post.get('title') or post_id, 60)}» — {note}. "
                            "Напишите «статус», чтобы увидеть всю очередь.")
    except Exception as exc:
        log.warning("Не удалось сообщить о решении: %s", exc)
    return {"post_id": post_id, "action": action, "status": applied}


APPLIED_TEXT = {
    "approved": "одобрен, опубликую",
    "rejected": "отклонён",
    "postponed": "отложен, выйдет",
    "rewrite": "перепишу, новое превью придёт в ближайшие полчаса. "
               "Если хотите подсказать, как именно, — ответьте на превью «переписать: …»",
}


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


def _store_owner_material(client: TelegramClient, message: dict[str, Any], owner: str,
                          *, persist: bool) -> bool:
    """Keep what the owner forwards or writes to the bot.

    The owner's own channel has no web preview, so the only way to get their
    posts as voice samples is a forward. The same inbox later feeds the
    floor-plan and payment-plan rubrics. Returns True when the message was
    stored (commands and strangers are left to the other handlers).
    """
    chat = message.get("chat") or {}
    from_id = str(((message.get("from") or {}).get("id")) or "")
    text = (message.get("text") or message.get("caption") or "").strip()
    if chat.get("type") != "private" or from_id != owner:
        return False
    forwarded = message.get("forward_origin") or {}
    is_forward = bool(forwarded or message.get("forward_from_chat") or message.get("forward_date"))
    if not is_forward and (not text or text.startswith("/")):
        return False

    origin_chat = forwarded.get("chat") or message.get("forward_from_chat") or {}
    photo = message.get("photo") or []
    document = message.get("document") or {}
    entry = {
        "message_id": message.get("message_id"),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "kind": "forward" if is_forward else "note",
        "origin": {
            "type": forwarded.get("type"),
            "chat_username": origin_chat.get("username"),
            "chat_title": origin_chat.get("title"),
            "message_id": forwarded.get("message_id"),
        },
        "text": text[:8000],
        "photo_file_id": (photo[-1] or {}).get("file_id") if photo else None,
        "document": {"file_id": document.get("file_id"), "name": document.get("file_name"),
                     "mime": document.get("mime_type")} if document else None,
    }
    inbox = state.load("inbox.json")
    items = inbox.setdefault("items", [])
    if any(i.get("message_id") == entry["message_id"] for i in items):
        return True
    items.append(entry)
    if persist:
        state.save("inbox.json", inbox)
    try:
        label = "пересланный пост" if is_forward else "заметка"
        client.send_message(str(chat.get("id") or from_id),
                            f"Сохранила ({label}). В копилке: {len(items)}.")
    except Exception as exc:
        log.warning("Не удалось подтвердить сохранение: %s", exc)
    return True


def _answer_id_request(client: TelegramClient, message: dict[str, Any], owner: str) -> None:
    """Tell a private-chat sender their numeric id.

    The owner has to put their own user id into TELEGRAM_OWNER_ID, and the
    number people copy from third-party bots is often the wrong one (the
    bot's own id, a chat id). Replying with the id from inside this bot gives
    them the exact value; it reveals nothing but the sender's own id.
    """
    chat = message.get("chat") or {}
    text = str(message.get("text") or "").strip().lower()
    if chat.get("type") != "private" or not text:
        return
    from_id = str(((message.get("from") or {}).get("id")) or "")
    if not from_id:
        return
    # A stranger's private message can only mean "who am I to this bot?" —
    # people type "id", "старт" or "привет" as readily as "/id", so any text
    # gets the answer. The owner is only answered on the explicit commands.
    if from_id == owner and not text.startswith(ID_COMMANDS):
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


def _post_for_reply(queue: dict[str, Any], message: dict[str, Any]) -> dict[str, Any] | None:
    """The queued post whose preview the owner replied to, if any."""
    replied = (message.get("reply_to_message") or {}).get("message_id")
    if not replied:
        return None
    for post in queue.get("posts") or []:
        approval = post.get("approval") or {}
        if replied in (approval.get("preview_message_id"), approval.get("preview_photo_message_id")):
            return post
    return None


def _apply_preview_reply(client: TelegramClient, queue: dict[str, Any], message: dict[str, Any],
                         owner: str) -> dict[str, Any] | None:
    """A reply to a preview edits that post.

    Plain text replaces the body as the owner wrote it (she is the approver,
    so the fact-check gate does not run on her words). Text starting with
    «переписать» is a wish for the model: the post goes back through
    ``run.regenerate_rewrites`` with that wish appended. Either way the
    buttons are taken off the old preview and a fresh one goes out.
    """
    chat = message.get("chat") or {}
    from_id = str(((message.get("from") or {}).get("id")) or "")
    if chat.get("type") != "private" or from_id != owner:
        return None
    post = _post_for_reply(queue, message)
    if post is None:
        return None
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return None
    post_id = post.get("post_id")
    if post.get("status") in TERMINAL_STATUSES:
        _reply(client, message, "Этот пост уже " + ("опубликован" if post["status"] == "published" else "отклонён")
               + ", править нечего.")
        return None
    old_preview = (post.get("approval") or {}).get("preview_message_id")

    lowered = text.lower()
    if lowered.startswith(MERGE_PREFIXES):
        from .inbox import as_item, merge_into_post

        material = text.split(":", 1)[1].strip() if ":" in text[:14] else text.lstrip("+ ").strip()
        if lowered.startswith(("дополни", "добав", "к посту")) and ":" not in text[:14]:
            material = text.split(None, 1)[1].strip() if " " in text else ""
        if not material:
            _reply(client, message, "После «дополни:» нужен сам текст дополнения.")
            return None
        entry = {"message_id": message.get("message_id"), "kind": "note", "text": material,
                 "received_at": datetime.now(timezone.utc).isoformat(), "origin": {}}
        merge_into_post(post, as_item(entry), note=material)
        _take_buttons_off(client, str(chat.get("id") or owner), old_preview)
        _reply(client, message, "Дополнение вошло в пост, перепишу его одним текстом. "
                                "Новое превью придёт в ближайшие полчаса.")
        log.info("Пост %s: владелица дополнила материалом (%d симв.)", post_id, len(material))
        return {"post_id": post_id, "action": "merge", "status": "rewrite"}
    if lowered.startswith(REWRITE_PREFIXES):
        wish = text.split(":", 1)[1].strip() if ":" in text[:16] else text.split(None, 1)[1].strip() if " " in text else ""
        postqueue.set_status(
            queue, post_id, "rewrite",
            approval={"sent_at": None, "action": "redo", "instruction": wish[:600],
                      "requested_at": datetime.now(timezone.utc).isoformat()},
        )
        _take_buttons_off(client, str(chat.get("id") or owner), old_preview)
        _reply(client, message, "Принято, перепишу" + (f" с учётом: «{wish[:200]}»" if wish else "")
               + ". Новое превью придёт в ближайшие полчаса.")
        log.info("Пост %s: владелица попросила переписать (%s)", post_id, wish[:80])
        return {"post_id": post_id, "action": "redo", "status": "rewrite", "instruction": wish}

    body = sanitize_telegram_html(text)
    post["body"] = body
    post["length_chars"] = len(body)
    post["cta"] = ""  # her text is the whole post
    if "#" in body:
        post["hashtags"] = []
    post["edited_by_owner_at"] = datetime.now(timezone.utc).isoformat()
    post["gate"] = {**(post.get("gate") or {}), "warnings": [], "owner_edited": True}
    post["status"] = "queued"
    post["approval"] = {"action": "edit", "edited_at": post["edited_by_owner_at"]}
    _take_buttons_off(client, str(chat.get("id") or owner), old_preview)
    _reply(client, message, "Заменила текст поста вашим. Сейчас пришлю новое превью с кнопками.")
    log.info("Пост %s: текст заменён владелицей (%d симв.)", post_id, len(body))
    return {"post_id": post_id, "action": "edit", "status": "queued"}


def _answer_status_request(client: TelegramClient, queue: dict[str, Any], message: dict[str, Any],
                           owner: str) -> bool:
    """«статус» from the owner → the queue report. True when handled."""
    chat = message.get("chat") or {}
    from_id = str(((message.get("from") or {}).get("id")) or "")
    text = (message.get("text") or "").strip().lower().rstrip("?!.")
    if chat.get("type") != "private" or from_id != owner or text not in STATUS_COMMANDS:
        return False
    _reply(client, message, status_report(queue))
    return True


def _reply(client: TelegramClient, message: dict[str, Any], text: str) -> None:
    chat = message.get("chat") or {}
    try:
        client.send_message(str(chat.get("id") or ""), text)
    except Exception as exc:
        log.warning("Не удалось ответить владелице: %s", exc)


def _take_buttons_off(client: TelegramClient, chat_id: str, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        client.edit_reply_markup(chat_id, message_id, {"inline_keyboard": []})
    except Exception as exc:
        log.warning("Кнопки с превью %s не сняты: %s", message_id, exc)


def _ack(client: TelegramClient, settings: Settings, callback: dict[str, Any], text: str) -> None:
    """Answer the callback and take the buttons off the preview message.

    The poll runs minutes after the tap, so the callback query is usually
    already expired and answerCallbackQuery fails; the buttons still have to
    come off, and the owner still needs to see that the tap was applied —
    hence the separate try blocks and the explicit message.
    """
    try:
        client.answer_callback(callback.get("id", ""), text)
    except Exception as exc:
        log.info("answerCallbackQuery не прошёл (обычно запрос уже истёк): %s", exc)
    message = callback.get("message") or {}
    if message.get("message_id"):
        _take_buttons_off(client, settings.telegram_owner_id, message["message_id"])


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
    if any(d.get("action") == "edit" for d in decisions):
        # The owner replaced a text with her own: she sees the result now,
        # not after the next poll.
        send_previews(settings, client=client, queue=postqueue.load_queue(), persist=True)
    return decisions
