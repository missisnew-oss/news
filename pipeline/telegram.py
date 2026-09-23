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

    def _scrub(self, text: object) -> str:
        """Error text with the bot token removed.

        ``requests`` puts the full request URL — ``/bot<TOKEN>/…`` — into its
        exception messages. The log redactor catches those in the log, but
        a ``TelegramError`` built from such a message also travels into
        ``state/inbox.json`` (``extracted.error``), which is committed to the
        repository. So the token is stripped at the source.
        """
        value = str(text)
        return value.replace(self.token, "***") if self.token else value

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
                last_error = self._scrub(exc)
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

    def send_photo(self, chat_id: str, photo_path: str | Path | None = None, *, caption: str = "",
                   reply_markup: dict | None = None, file_id: str | None = None) -> dict[str, Any]:
        """Send a photo from a local file, or by Telegram ``file_id`` once it
        has been uploaded before (the preview upload makes publishing
        independent of the local file)."""
        payload: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption
        if reply_markup:
            import json as _json

            payload["reply_markup"] = _json.dumps(reply_markup, ensure_ascii=False)
        if self.dry_run:
            payload["photo"] = file_id or str(photo_path)
            return self._dry("sendPhoto", payload)
        if file_id:
            payload["photo"] = file_id
            return self.call("sendPhoto", payload, timeout=60)
        if not photo_path:
            raise TelegramError("sendPhoto: нет ни файла, ни file_id")
        with open(photo_path, "rb") as fh:
            return self.call("sendPhoto", payload, files={"photo": fh}, timeout=60)

    # Reaction counts arrive only as updates, and only if we ask for them
    # explicitly: there is no "get reactions for message_id" method in Bot API.
    ALLOWED_UPDATES = ["message", "callback_query", "channel_post", "message_reaction_count"]

    @staticmethod
    def photo_file_id(response: dict[str, Any]) -> str | None:
        """Largest-size file_id from a sendPhoto result, reusable in later runs."""
        photos = (response.get("result") or {}).get("photo") or []
        return (photos[-1] or {}).get("file_id") if photos else None

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

    # -- files the owner sends to the bot (pipeline/inbox.py) ----------------
    def get_file(self, file_id: str) -> dict[str, Any]:
        """``getFile`` result: ``{file_id, file_path, file_size}``.

        Bot API refuses files over 20 MB here with "file is too big"; the
        caller maps that to ``too_large``. The returned ``file_path`` is only
        valid for about an hour, so download right away.
        """
        if self.dry_run:
            self._dry("getFile", {"file_id": file_id})
            return {"file_id": file_id, "file_path": f"dry-run/{file_id}", "file_size": 0}
        data = self.call("getFile", {"file_id": file_id})
        return data.get("result") or {}

    # Bot API never serves more than this through getFile; the cap is enforced
    # here too so a wrong Content-Length or an endless stream cannot fill the disk.
    MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

    def download_file(self, file_path: str, dest: str | Path, timeout: int = 120,
                      *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> Path:
        """Fetch ``file_path`` from ``getFile`` into ``dest`` (streamed to disk).

        Stops — and removes the partial file — as soon as more than
        ``max_bytes`` arrive, whatever the headers said. In DRY_RUN nothing is
        fetched: an empty file is written so the rest of the inbox stage can
        run its "unreadable file" branches offline.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.dry_run:
            self.calls.append({"method": "downloadFile", "payload": {"file_path": file_path}})
            log.info("DRY_RUN telegram.downloadFile %s → %s", file_path, dest.name)
            dest.write_bytes(b"")
            return dest

        import requests

        url = f"{API_ROOT}/file/bot{self.token}/{file_path}"
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                if response.status_code != 200:
                    raise TelegramError(f"downloadFile: HTTP {response.status_code}")
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > max_bytes:
                    raise TelegramError(f"downloadFile: файл {declared} байт больше лимита {max_bytes}")
                total = 0
                with open(dest, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise TelegramError(f"downloadFile: поток больше лимита {max_bytes} байт")
                        fh.write(chunk)
        except TelegramError:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise TelegramError(f"downloadFile: {self._scrub(exc)}") from None
        return dest


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
