"""Verify every source in config/sources.yml with a real HTTP request.

Run it with ``make verify-sources``. It rewrites the ``verification`` block of
each source in place, flips ``enabled`` to false for sources that fail, and
writes a machine-readable health snapshot to ``state/sources_health.json``.

This is the tool that turns an unverified registry into a verified one: the
brief forbids shipping URLs that were never actually requested.
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
from .config import SOURCES_FILE, load_sources
from .logging_setup import setup_logging

log = logging.getLogger("pipeline.verify_sources")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    parser.add_argument("--write", action="store_true", default=True,
                        help="переписать verification в sources.yml (по умолчанию да)")
    parser.add_argument("--dry", action="store_true", help="только показать результат, ничего не писать")
    parser.add_argument("--only", help="проверить один источник по id")
    args = parser.parse_args(argv)

    setup_logging("INFO")
    doc = load_sources()
    defaults = doc.get("defaults") or {}
    timeout = int(defaults.get("timeout_sec", 20))
    user_agent = defaults.get("user_agent", "DubaiNewsBot/1.0")

    health: dict[str, Any] = {"version": 1, "checked_at": _now(), "sources": {}}
    ok = failed = 0
    for source in doc.get("sources") or []:
        if args.only and source["id"] != args.only:
            continue
        verdict = check_source(source, timeout=timeout, user_agent=user_agent)
        source["verification"] = verdict
        if verdict["status"] != "ok":
            source["enabled"] = False
            failed += 1
            log.warning("%-30s FAILED %s %s", source["id"], verdict["http_code"], verdict["note"])
        else:
            ok += 1
            log.info("%-30s OK %s items=%s", source["id"], verdict["http_code"], verdict["items_found"])
        health["sources"][source["id"]] = verdict

    print(f"\nПроверено: {ok + failed}; рабочих: {ok}; нерабочих: {failed}")
    if args.dry:
        return 0 if failed == 0 else 1

    import yaml

    SOURCES_FILE.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    state.save("sources_health.json", health)
    print(f"Обновлены {SOURCES_FILE} и state/sources_health.json")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
