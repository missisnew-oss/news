"""Stage 1 — COLLECT: fetch raw items from every enabled source.

Supported source types: rss, atom, json_api, html (link harvest), sitemap,
telegram (public channel preview at t.me/s/<name>, see telegram_source.py).
Network access is skipped entirely in DRY_RUN; the pipeline then reads
``config/sample_items.json`` so a dry run is reproducible and offline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .config import CONFIG_DIR, Settings, enabled_sources, load_sources
from .telegram_source import parse_preview
from .textutil import canonical_url, strip_html

log = logging.getLogger("pipeline.collect")

SAMPLE_ITEMS_FILE = CONFIG_DIR / "sample_items.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get(url: str, *, timeout: int, user_agent: str) -> tuple[int, bytes, str]:
    import requests

    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        allow_redirects=True,
    )
    return response.status_code, response.content, response.headers.get("Content-Type", "")


def _parse_feed(payload: bytes, source: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    import feedparser

    parsed = feedparser.parse(payload)
    items: list[dict[str, Any]] = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link") or ""
        if not link:
            continue
        published = None
        for key in ("published_parsed", "updated_parsed"):
            struct = entry.get(key)
            if struct:
                published = datetime(*struct[:6], tzinfo=timezone.utc).isoformat()
                break
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        items.append({
            "source_id": source["id"],
            "title": strip_html(entry.get("title", "")),
            "summary": summary[:1200],
            "url": link,
            "published_at": published,
            "image_url": _feed_image(entry),
            "raw_text": summary[:4000],
        })
    return items


def _feed_image(entry: Any) -> str | None:
    for media in (entry.get("media_content") or []):
        if media.get("url"):
            return media["url"]
    for link in (entry.get("links") or []):
        if str(link.get("type", "")).startswith("image/") and link.get("href"):
            return link["href"]
    return None


def _dig(payload: Any, path: str | None) -> Any:
    """Read a dotted path out of a nested dict/list structure."""
    if not path:
        return None
    current = payload
    for part in str(path).split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _parse_json_api(payload: bytes, source: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    extract = source.get("extract") or {}
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("Источник %s: тело не является валидным JSON", source["id"])
        return []
    rows = _dig(data, extract.get("items_path")) if extract.get("items_path") else data
    if not isinstance(rows, list):
        log.warning("Источник %s: items_path не указывает на список", source["id"])
        return []
    items: list[dict[str, Any]] = []
    for row in rows[:limit]:
        url = _dig(row, extract.get("url_path"))
        title = _dig(row, extract.get("title_path"))
        if not url or not title:
            continue
        items.append({
            "source_id": source["id"],
            "title": strip_html(str(title)),
            "summary": strip_html(str(_dig(row, extract.get("summary_path")) or ""))[:1200],
            "url": str(url),
            "published_at": _dig(row, extract.get("date_path")),
            "image_url": None,
            "raw_text": "",
        })
    return items


def _parse_html(payload: bytes, source: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Very conservative HTML harvest: anchors whose text looks like a headline.

    HTML sources are a last resort — they break often, which is why
    ``pipeline.verify_sources`` reports them separately.
    """
    import re

    text = payload.decode("utf-8", errors="replace")
    anchors = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, flags=re.S | re.I)
    base = source.get("homepage") or source["url"]
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for href, label in anchors:
        title = strip_html(label)
        if len(title) < 35 or len(title) > 200:
            continue
        url = href if href.startswith("http") else _join(base, href)
        key = canonical_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({
            "source_id": source["id"],
            "title": title,
            "summary": "",
            "url": url,
            "published_at": None,
            "image_url": None,
            "raw_text": "",
        })
        if len(items) >= limit:
            break
    return items


def _join(base: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, href)


PARSERS = {
    "rss": _parse_feed,
    "atom": _parse_feed,
    "json_api": _parse_json_api,
    "html": _parse_html,
    "sitemap": _parse_html,
    "telegram": parse_preview,
}


def load_sample_items() -> list[dict[str, Any]]:
    """Offline fixture used by DRY_RUN so the pipeline is reproducible."""
    if not SAMPLE_ITEMS_FILE.exists():
        return []
    data = json.loads(SAMPLE_ITEMS_FILE.read_text(encoding="utf-8"))
    return data.get("items", []) if isinstance(data, dict) else list(data)


def collect(settings: Settings, sources_doc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return raw (not yet normalised) items from all enabled sources."""
    doc = sources_doc or load_sources()
    defaults = doc.get("defaults") or {}
    timeout = int(defaults.get("timeout_sec", 20))
    user_agent = defaults.get("user_agent", "DubaiNewsBot/1.0")
    limit = int(defaults.get("max_items_per_source", 20))

    if settings.dry_run:
        items = load_sample_items()
        log.info("DRY_RUN: сеть не используется, загружено %d демо-элементов", len(items))
        return items

    raw: list[dict[str, Any]] = []
    for source in enabled_sources(doc):
        parser = PARSERS.get(source["type"])
        if parser is None:
            log.warning("Источник %s: тип %s не поддерживается", source["id"], source["type"])
            continue
        try:
            status, payload, content_type = _http_get(
                source["url"], timeout=timeout, user_agent=user_agent
            )
        except Exception as exc:  # network errors must not kill the whole run
            log.warning("Источник %s недоступен: %s", source["id"], exc)
            continue
        if status != 200 or not payload:
            log.warning("Источник %s вернул HTTP %s", source["id"], status)
            continue
        try:
            items = parser(payload, source, limit)
        except Exception as exc:
            log.warning("Источник %s: ошибка разбора (%s): %s", source["id"], content_type, exc)
            continue
        log.info("Источник %-28s → %d элементов", source["id"], len(items))
        raw.extend(items)
    return raw
