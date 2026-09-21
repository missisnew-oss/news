"""Private Telegram channels as a source: read them through a user account.

Neither the Bot API nor the t.me/s web preview can see a private channel, so
the only way in is MTProto with a real user session (Telethon). The session
string is a full login to that account and is handled like a password:
it lives in ``TELEGRAM_SESSION`` (GitHub Secret), never in the repository,
and is redacted from logs by ``logging_setup``.

Channels are addressed by numeric ``channel_id`` (not by invite link — an
invite link lets anyone join, so it must not be committed). The reading
account has to be a member already; ``scripts/telegram_login.py`` prints the
ids of every channel it is in.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Settings

log = logging.getLogger("pipeline.telegram_private")

MIN_TEXT = 20


def is_configured(settings: Settings) -> bool:
    return bool(settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session)


def normalize_channel_id(value: int | str) -> int:
    """Accept both Bot-API style ``-100123`` and bare ``123``; return the bare id."""
    text = str(value).strip()
    if text.startswith("-100"):
        text = text[4:]
    return int(text.lstrip("-"))


def permalink(channel_id: int, message_id: int) -> str:
    return f"https://t.me/c/{normalize_channel_id(channel_id)}/{message_id}"


def _headline(text: str, limit: int = 120) -> str:
    first = text.split("\n", 1)[0].strip()
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0] + "…"
    return first


def message_to_item(message: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Telethon Message onto the collector's raw-item schema."""
    text = (getattr(message, "message", None) or "").strip()
    if len(text) < MIN_TEXT:
        return None
    date = getattr(message, "date", None)
    return {
        "source_id": source["id"],
        "title": _headline(text),
        "summary": text[:1200],
        "url": permalink(source["channel_id"], message.id),
        "published_at": date.isoformat() if date else None,
        # Photos need an authenticated download; the card template covers it.
        "image_url": None,
        "raw_text": text[:4000],
        "views": getattr(message, "views", None),
    }


def _open_client(settings: Settings):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(settings.telegram_session),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
    )


async def _fetch_async(sources: list[dict[str, Any]], settings: Settings, limit: int) -> list[dict[str, Any]]:
    from telethon.tl.types import PeerChannel

    client = _open_client(settings)
    items: list[dict[str, Any]] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "TELEGRAM_SESSION не авторизована: перевыпустите её через scripts/telegram_login.py"
            )
        for source in sources:
            channel_id = normalize_channel_id(source["channel_id"])
            try:
                entity = await client.get_entity(PeerChannel(channel_id))
                messages = await client.get_messages(entity, limit=limit)
            except Exception as exc:  # one broken channel must not kill the run
                log.warning("Приватный канал %s недоступен: %s", source["id"], exc)
                continue
            found = [i for i in (message_to_item(m, source) for m in messages) if i]
            log.info("Источник %-28s → %d элементов", source["id"], len(found))
            items.extend(found)
    finally:
        await client.disconnect()
    return items


def fetch_private(sources: list[dict[str, Any]], settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    """Read the latest posts of every private channel in ``sources``.

    Returns [] (with a single warning) when the user session is not configured,
    so the rest of the pipeline keeps working without it.
    """
    sources = [s for s in sources if s.get("type") == "telegram_private"]
    if not sources:
        return []
    if not is_configured(settings):
        log.warning(
            "Приватные каналы (%s) пропущены: не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH / "
            "TELEGRAM_SESSION — см. docs/SETUP.md, шаг «Приватные каналы»",
            ", ".join(s["id"] for s in sources),
        )
        return []
    try:
        return asyncio.run(_fetch_async(sources, settings, limit))
    except Exception as exc:
        log.warning("Чтение приватных каналов не удалось: %s", exc)
        return []
