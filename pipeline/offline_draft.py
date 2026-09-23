"""Deterministic offline post builder used by DRY_RUN.

It reads the rendered prompt (which already contains the input items block and
the rubric id) and assembles a schema-valid response, so a dry run exercises
the same parsing, fact-check and rendering code paths as production without
calling any API.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .textutil import truncate

_RUBRIC_RE = re.compile(r"RUBRIC_ID:\s*([a-z_]+)", re.I)
_ITEMS_RE = re.compile(r"<INPUT_ITEMS>(.*?)</INPUT_ITEMS>", re.S)
# A figure of at least two digits: something the fact-check gate would ask a source for.
_NUMBER_IN_TEXT_RE = re.compile(r"\d{2,}(?:[.,]\d+)?")


def _parse_items(prompt: str) -> list[dict[str, Any]]:
    match = _ITEMS_RE.search(prompt)
    if not match:
        return []
    try:
        data = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def build_offline_draft(prompt: str) -> dict[str, Any]:
    rubric_match = _RUBRIC_RE.search(prompt)
    rubric = rubric_match.group(1) if rubric_match else "market_pulse"
    items = _parse_items(prompt)

    primary = items[0] if items else {
        "title": "Демонстрационный материал",
        "url": "https://example.com/demo",
        "source_id": "demo_source",
        "summary": "Демонстрационные входные данные для сухого прогона.",
    }
    bullets = []
    for item in items[:3]:
        bullets.append(f"• {item.get('title', '')[:120]} — {item.get('source_id', '')}")

    # Mirror the confidence rules of prompts/system_tone.md: an official or
    # developer source confirms a fact on its own, anything else is a
    # single-source claim and the text must say so.
    confidence = (
        "confirmed" if primary.get("category") in {"official_data", "developers"} else "single_source"
    )
    source_link = f"<a href=\"{primary.get('url', '')}\">{primary.get('source_id', 'источник')}</a>"
    source_line = (
        f"Источник: {source_link}" if confidence == "confirmed"
        else f"По данным {source_link}; официального подтверждения пока нет."
    )

    body_lines = [
        f"<b>{primary.get('title', '')[:90]}</b>",
        "",
        "Коротко, что это значит для покупателя из СНГ и для резидента ОАЭ:",
        *bullets,
        "",
        "Это сухой прогон: текст собран офлайн-заглушкой, без обращения к модели.",
        "",
        source_line,
    ]
    body = "\n".join(line for line in body_lines if line is not None)

    facts = []
    number = _NUMBER_IN_TEXT_RE.search(f"{primary.get('title', '')} {primary.get('summary', '')}")
    if number:
        facts.append({
            "claim": truncate(str(primary.get("title", "")), 80, "…"),
            "value": number.group(0),
            "source_url": primary.get("url", ""),
            "confidence": confidence,
            "sources": [primary.get("url", "")],
        })

    return {
        "rubric": rubric,
        "title": truncate(str(primary.get("title", "Демо-пост")), 90, "…"),
        "body": body,
        "hashtags": ["#дубай", "#недвижимость"],
        "cta": "Напишите в личные сообщения, если хотите разобрать свой случай.",
        "image": {
            "mode": "card",
            "headline": truncate(str(primary.get("title", "Демо-пост")), 60, "…"),
            "accent": "",
            "stock_query": "dubai skyline real estate",
        },
        "sources": [
            {
                "source_id": item.get("source_id", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
            }
            for item in (items[:3] or [primary])
        ],
        "facts": facts,
        "self_check": {
            "all_numbers_have_source": True,
            "no_guaranteed_returns": True,
            "language_is_russian": True,
            "no_verbatim_copy": True,
            "fits_telegram_limit": len(body) <= 1024,
        },
        "length_chars": len(body),
        "needs_separate_text": len(body) > 1024,
    }
