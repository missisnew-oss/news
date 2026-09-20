"""Stage 2 — NORMALIZE + DEDUPE.

Raw source payloads become NormalizedItem objects with a stable ``item_id``
and a ``dedupe_hash``. Anything whose hash is already in
``state/seen_items.json`` is dropped, so the same story never reaches GENERATE
twice, no matter how many feeds carry it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from . import state
from .models import NormalizedItem
from .textutil import canonical_url, dedupe_hash, normalize_title, sha1, strip_html

log = logging.getLogger("pipeline.normalize")

# Sequences that have no business travelling from an RSS feed into an LLM
# prompt. Stripping them here is the first prompt-injection barrier
# (see docs/SECURITY.md).
INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "disregard previous",
    "system prompt", "you are now", "new instructions",
    "игнорируй предыдущие", "забудь инструкции", "системный промпт",
)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            from dateutil import parser as date_parser

            parsed = date_parser.parse(str(value))
        except Exception:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def scrub_untrusted(text: str) -> str:
    """Neutralise instruction-like content coming from third-party feeds."""
    clean = strip_html(text or "")
    lowered = clean.lower()
    for marker in INJECTION_MARKERS:
        idx = lowered.find(marker)
        while idx != -1:
            clean = clean[:idx] + "[удалено]" + clean[idx + len(marker):]
            lowered = clean.lower()
            idx = lowered.find(marker)
    return clean


def normalize_one(raw: dict[str, Any], source_index: dict[str, dict[str, Any]]) -> NormalizedItem | None:
    url = (raw.get("url") or "").strip()
    title = scrub_untrusted(raw.get("title") or "")
    if not url or len(title) < 12:
        return None
    source = source_index.get(raw.get("source_id", ""), {})
    canonical = canonical_url(url)
    published = _parse_dt(raw.get("published_at"))
    return NormalizedItem(
        item_id=sha1(canonical or url),
        source_id=raw.get("source_id", "unknown"),
        category=source.get("category", raw.get("category", "realty_news")),
        title=title[:300],
        summary=scrub_untrusted(raw.get("summary") or "")[:1200],
        url=url,
        canonical_url=canonical,
        published_at=published.isoformat() if published else None,
        collected_at=datetime.now(timezone.utc).isoformat(),
        lang=source.get("lang", raw.get("lang", "en")),
        image_url=raw.get("image_url"),
        tags=list(source.get("topics") or []),
        raw_text=scrub_untrusted(raw.get("raw_text") or "")[:4000],
        dedupe_hash=dedupe_hash(title, url),
    )


def dedupe(items: Iterable[NormalizedItem], seen: dict[str, Any]) -> tuple[list[NormalizedItem], dict[str, Any]]:
    """Filter out items already seen, and items duplicated inside this batch.

    Two items collapse when their canonical URL + normalised title hash match,
    or when their normalised titles are byte-identical (same story, two feeds).
    """
    known_hashes = set((seen.get("items") or {}).keys())
    known_titles = {
        (meta or {}).get("title_key")
        for meta in (seen.get("items") or {}).values()
    }
    known_titles.discard(None)

    fresh: list[NormalizedItem] = []
    batch_hashes: set[str] = set()
    batch_titles: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        title_key = normalize_title(item.title)
        if item.dedupe_hash in known_hashes or item.dedupe_hash in batch_hashes:
            continue
        if title_key and (title_key in known_titles or title_key in batch_titles):
            continue
        batch_hashes.add(item.dedupe_hash)
        if title_key:
            batch_titles.add(title_key)
        fresh.append(item)

    items_map = seen.setdefault("items", {})
    for item in fresh:
        items_map[item.dedupe_hash] = {
            "first_seen_at": now,
            "source_id": item.source_id,
            "title_key": normalize_title(item.title),
            "url": item.canonical_url,
        }
    return fresh, seen


def normalize_and_dedupe(
    raw_items: list[dict[str, Any]],
    sources_doc: dict[str, Any],
    *,
    persist: bool = True,
) -> list[NormalizedItem]:
    source_index = {s["id"]: s for s in (sources_doc.get("sources") or [])}
    normalized = [
        item for item in (normalize_one(raw, source_index) for raw in raw_items) if item
    ]
    seen = state.load("seen_items.json")
    fresh, seen = dedupe(normalized, seen)
    if persist:
        state.save("seen_items.json", state.prune_seen(seen))
    log.info("Нормализовано %d, после дедупликации осталось %d", len(normalized), len(fresh))
    return fresh
