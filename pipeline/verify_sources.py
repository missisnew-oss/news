"""Verify every source in config/sources.yml with a real HTTP request.

Run it with ``make verify-sources``. It writes the verdicts to
``state/sources_health.json`` — the collector only uses sources whose latest
verdict there is ``ok`` (see ``config.enabled_sources``). The curated
``config/sources.yml`` is never rewritten: a YAML dump would strip every
comment and produce a diff the state guard rightly refuses.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import state
from .config import load_sources
from .logging_setup import setup_logging

log = logging.getLogger("pipeline.verify_sources")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_private_source(source: dict[str, Any], settings: Any) -> dict[str, Any]:
    """A private channel is verified by actually reading it with the user session."""
    from .telegram_private import fetch_private, is_configured

    result: dict[str, Any] = {
        "status": "failed", "http_code": None, "content_type": "mtproto",
        "items_found": 0, "latest_item_at": None, "checked_at": _now(), "note": "",
    }
    if source.get("channel_id") is None:
        result["note"] = "не заполнен channel_id — возьмите его из вывода scripts/telegram_login.py"
        return result
    if not is_configured(settings):
        result["note"] = "не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION"
        return result
    items = fetch_private([source], settings, limit=20)
    result["items_found"] = len(items)
    dated = [i["published_at"] for i in items if i.get("published_at")]
    if dated:
        result["latest_item_at"] = max(dated)
    result["status"] = "ok" if items else "failed"
    if not items:
        result["note"] = "сессия работает, но постов не прочитано: аккаунт не в канале или id неверный"
    return result


def check_source(source: dict[str, Any], *, timeout: int, user_agent: str) -> dict[str, Any]:
    import requests

    result: dict[str, Any] = {
        "status": "failed",
        "http_code": None,
        "content_type": None,
        "items_found": 0,
        "latest_item_at": None,
        "checked_at": _now(),
        "note": "",
    }
    try:
        response = requests.get(
            source["url"],
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            allow_redirects=True,
        )
    except Exception as exc:
        result["note"] = f"сетевая ошибка: {exc}"[:200]
        return result

    result["http_code"] = response.status_code
    result["content_type"] = (response.headers.get("Content-Type") or "").split(";")[0]
    if response.status_code != 200:
        result["note"] = f"HTTP {response.status_code}"
        return result

    kind = source.get("type")
    if kind in {"rss", "atom"}:
        try:
            import feedparser

            parsed = feedparser.parse(response.content)
            result["items_found"] = len(parsed.entries)
            if not parsed.entries:
                result["note"] = "фид разобран, но элементов нет"
                return result
            newest = None
            for entry in parsed.entries:
                for key in ("published_parsed", "updated_parsed"):
                    struct = entry.get(key)
                    if struct:
                        candidate = datetime(*struct[:6], tzinfo=timezone.utc)
                        newest = candidate if not newest or candidate > newest else newest
                        break
            if newest:
                result["latest_item_at"] = newest.isoformat().replace("+00:00", "Z")
            result["status"] = "ok"
            if parsed.bozo:
                result["note"] = f"фид с замечаниями: {parsed.bozo_exception}"[:160]
        except ImportError:
            result["note"] = "feedparser не установлен"
        return result

    if kind == "json_api":
        try:
            data = json.loads(response.content.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            result["note"] = f"не JSON: {exc}"[:160]
            return result
        rows = data if isinstance(data, list) else None
        extract = source.get("extract") or {}
        if rows is None and extract.get("items_path"):
            from .collect import _dig

            rows = _dig(data, extract["items_path"])
        result["items_found"] = len(rows) if isinstance(rows, list) else 0
        result["status"] = "ok" if result["items_found"] else "failed"
        if not result["items_found"]:
            result["note"] = "JSON получен, но список элементов не найден"
        return result

    if kind == "telegram":
        from .telegram_source import has_preview, parse_preview

        if not has_preview(response.content):
            result["note"] = (
                "t.me отдал страницу без постов: канал приватный или у него выключено "
                "веб-превью — через t.me/s его не прочитать"
            )
            return result
        items = parse_preview(response.content, source, limit=50)
        result["items_found"] = len(items)
        dated = [i["published_at"] for i in items if i.get("published_at")]
        if dated:
            result["latest_item_at"] = max(dated)
        result["status"] = "ok" if items else "failed"
        if not items:
            result["note"] = "превью есть, но текстовых постов нет (только медиа/стикеры)"
        return result

    # html / ics / sitemap: a 200 with a non-trivial body is all we can assert.
    body_len = len(response.content)
    result["items_found"] = 1 if body_len > 500 else 0
    result["status"] = "ok" if body_len > 500 else "failed"
    if result["status"] == "failed":
        result["note"] = f"слишком короткий ответ ({body_len} байт)"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка источников из config/sources.yml")
    parser.add_argument("--dry", action="store_true", help="только показать результат, ничего не писать")
    parser.add_argument("--only", help="проверить один источник по id")
    args = parser.parse_args(argv)

    setup_logging("INFO")
    from .config import load_settings

    settings = load_settings()
    doc = load_sources()
    defaults = doc.get("defaults") or {}
    timeout = int(defaults.get("timeout_sec", 20))
    user_agent = defaults.get("user_agent", "DubaiNewsBot/1.0")

    # Start from the previous snapshot so `--only` refreshes one verdict
    # without forgetting the rest.
    health: dict[str, Any] = state.load("sources_health.json") if args.only else {"version": 1, "sources": {}}
    health["version"] = 1
    health["checked_at"] = _now()
    health.setdefault("sources", {})
    ok = failed = 0
    for source in doc.get("sources") or []:
        if args.only and source["id"] != args.only:
            continue
        if source["type"] == "telegram_private":
            verdict = check_private_source(source, settings)
        else:
            verdict = check_source(source, timeout=timeout, user_agent=user_agent)
        if verdict["status"] != "ok":
            failed += 1
            log.warning("%-30s FAILED %s %s", source["id"], verdict["http_code"], verdict["note"])
        else:
            ok += 1
            log.info("%-30s OK %s items=%s", source["id"], verdict["http_code"], verdict["items_found"])
        health["sources"][source["id"]] = verdict

    print(f"\nПроверено: {ok + failed}; рабочих: {ok}; нерабочих: {failed}")
    if args.dry:
        return 0 if failed == 0 else 1

    state.save("sources_health.json", health)
    print("Обновлён state/sources_health.json — включены источники со status: ok")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
