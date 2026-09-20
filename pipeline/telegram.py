"""Telegram Bot API client.

Two guarantees matter here:
  * in DRY_RUN the client never opens a socket — every call is recorded in
    ``self.calls`` and answered with a synthetic response (tests assert this);
  * rate limits (~20 messages/minute per chat) are handled with exponential
    backoff that honours the ``retry_after`` value Telegram returns.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("pipeline.telegram")

API_ROOT = "https://api.telegram.org"
MAX_RETRIES = 5
BASE_BACKOFF = 2.0


class TelegramError(RuntimeError):
    pass


class DryRunViolation(AssertionError):
    """Raised if anything tries to reach the network while DRY_RUN is on."""


class TelegramClient:
    def __init__(self, token: str, *, dry_run: bool = True, sleep=time.sleep) -> None:
        self.token = token
        self.dry_run = dry_run
        self._sleep = sleep
        self.calls: list[dict[str, Any]] = []
        self._fake_message_id = 1000
        if not dry_run and not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN не задан, а DRY_RUN выключен")

    # -- transport ---------------------------------------------------------
    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"

    def _dry(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"method": method, "payload": payload})
        log.info("DRY_RUN telegram.%s payload_keys=%s", method, sorted(payload))
        self._fake_message_id += 1
        if method == "getUpdates":
            return {"ok": True, "result": []}
        if method == "getChatMemberCount":
            return {"ok": True, "result": 0}
        return {"ok": True, "result": {"message_id": self._fake_message_id, "dry_run": True}}

    def call(self, method: str, payload: dict[str, Any] | None = None,
             files: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        payload = payload or {}
        if self.dry_run:
            return self._dry(method, payload)

        import requests

        last_error = ""
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.post(
                    self._url(method), data=payload, files=files, timeout=timeout
                )
            except Exception as exc:
                last_error = str(exc)
                delay = BASE_BACKOFF ** attempt
                log.warning("telegram.%s сетевая ошибка (%s), пауза %.1fs", method, exc, delay)
                self._sleep(delay)
                continue

            if response.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(
                        (response.json().get("parameters") or {}).get("retry_after", 5)
                    )
                except Exception:
                    pass
                delay = max(retry_after, BASE_BACKOFF ** attempt)
                log.warning("telegram.%s rate limit, пауза %.1fs", method, delay)
                self._sleep(delay)
                continue

            if response.status_code >= 500:
                delay = BASE_BACKOFF ** attempt
                log.warning("telegram.%s HTTP %s, пауза %.1fs", method, response.status_code, delay)
                self._sleep(delay)
                continue

            data = response.json()
            if not data.get("ok"):
                raise TelegramError(f"{method}: {data.get('description', 'unknown error')}")
            return data

        raise TelegramError(f"{method}: превышено число попыток ({last_error})")

    # -- high level --------------------------------------------------------
    def send_message(self, chat_id: str, text: str, *, reply_markup: dict | None = None,
                     disable_preview: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            import json as _json

            payload["reply_markup"] = _json.dumps(reply_markup, ensure_ascii=False)
        return self.call("sendMessage", payload)

    def send_photo(self, chat_id: str, photo_path: str | Path, *, caption: str = "",
                   reply_markup: dict | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption
        if reply_markup:
            import json as _json

            payload["reply_markup"] = _json.dumps(reply_markup, ensure_ascii=False)
        if self.dry_run:
            payload["photo"] = str(photo_path)
            return self._dry("sendPhoto", payload)
        with open(photo_path, "rb") as fh:
            return self.call("sendPhoto", payload, files={"photo": fh}, timeout=60)

    # Reaction counts arrive only as updates, and only if we ask for them
    # explicitly: there is no "get reactions for message_id" method in Bot API.
    ALLOWED_UPDATES = ["message", "callback_query", "channel_post", "message_reaction_count"]

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        import json as _json

        data = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": _json.dumps(self.ALLOWED_UPDATES),
            },
            timeout=timeout + 10,
        )
        return data.get("result") or []

    def answer_callback(self, callback_id: str, text: str = "") -> dict[str, Any]:
        return self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_reply_markup(self, chat_id: str, message_id: int, reply_markup: dict | None = None) -> dict[str, Any]:
        import json as _json

        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        payload["reply_markup"] = _json.dumps(reply_markup or {"inline_keyboard": []}, ensure_ascii=False)
        return self.call("editMessageReplyMarkup", payload)

    def get_chat_member_count(self, chat_id: str) -> int:
        data = self.call("getChatMemberCount", {"chat_id": chat_id})
        result = data.get("result")
        return int(result) if isinstance(result, int) else 0


def approval_keyboard(post_id: str) -> dict[str, Any]:
    """Inline keyboard shown to the owner in a private chat."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Опубликовать", "callback_data": f"ok:{post_id}"},
                {"text": "✍️ Переписать", "callback_data": f"redo:{post_id}"},
            ],
            [
                {"text": "🕒 Отложить", "callback_data": f"later:{post_id}"},
                {"text": "🗑 Отклонить", "callback_data": f"no:{post_id}"},
            ],
        ]
    }
