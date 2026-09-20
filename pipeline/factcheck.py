"""Fact-check gate between GENERATE and QUEUE.

The brief is explicit: every number in a post (price, yield, deadline, event
date) must be traceable to the collected input. A post that cannot prove a
number is rejected, not published with a disclaimer.

Checks performed:
  1. numbers  — each number in the body occurs in the input items, or is
                declared in ``facts`` with a source_url taken from the input;
  2. sources  — every cited URL comes from the input items;
  3. legal    — no guaranteed-return wording (docs/LEGAL.md);
  4. limits   — the post fits the Telegram budget for its delivery mode;
  5. language — the body is actually Russian.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT
from .models import NormalizedItem
from .textutil import canonical_url, strip_html

log = logging.getLogger("pipeline.factcheck")

# Forbidden investment wording — see docs/LEGAL.md for the full "нельзя → можно" table.
BANNED_PATTERNS = (
    r"гарантиров\w*\s+доходн",
    r"гарантиру\w*\s+(?:доход|прибыл|рост|перепродаж)",
    r"беспроигрышн",
    r"без\s+риск",
    r"нулев\w*\s+риск",
    r"100\s*%\s*(?:доходн|надёжн|надежн|доход)",
    r"точно\s+вырастет",
    r"обязательно\s+(?:подорожает|окупится|вырастет)",
    r"надёжнее\s+(?:депозит|банк)",
    r"надежнее\s+(?:депозит|банк)",
    r"пассивн\w*\s+доход\s+без",
    r"последний\s+шанс",
    r"лучшая\s+инвестиция",
    r"налогов\s+в\s+дубае\s+нет\s+вообще",
)

# Numbers that carry no factual claim and never need a source.
_NOISE_NUMBERS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "0", "100"}

_NUMBER_RE = re.compile(r"\d[\d\s .,]*\d|\d")
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def _digits(token: str) -> str:
    return re.sub(r"\D", "", token)


def extract_numbers(text: str) -> list[str]:
    plain = strip_html(text or "")
    # Drop years inside URLs and hashtags before scanning.
    plain = re.sub(r"https?://\S+", " ", plain)
    found = []
    for raw in _NUMBER_RE.findall(plain):
        digits = _digits(raw)
        if not digits or digits in _NOISE_NUMBERS or len(digits) > 15:
            continue
        found.append(digits)
    return found


def _haystack_digits(items: list[NormalizedItem]) -> set[str]:
    haystack: set[str] = set()
    for item in items:
        blob = f"{item.title} {item.summary} {item.raw_text} {item.published_at or ''}"
        for raw in _NUMBER_RE.findall(blob):
            digits = _digits(raw)
            if digits:
                haystack.add(digits)
                # A number may be quoted rounded, e.g. 1 234 567 -> 1.23M.
                haystack.add(digits.lstrip("0") or "0")
    return haystack


def check(payload: dict[str, Any], items: list[NormalizedItem], *, max_chars: int) -> dict[str, Any]:
    """Return a gate verdict: {'passed': bool, 'errors': [...], 'warnings': [...]}"""
    errors: list[str] = []
    warnings: list[str] = []

    body = payload.get("body", "")
    combined = f"{payload.get('title', '')}\n{body}\n{payload.get('cta', '')}"

    # 1. numbers
    allowed_digits = _haystack_digits(items)
    for fact in payload.get("facts") or []:
        allowed_digits.update(_digits(str(fact.get("value", ""))) for _ in (0,))
        digits = _digits(str(fact.get("value", "")))
        if digits:
            allowed_digits.add(digits)
    unsourced = sorted({n for n in extract_numbers(combined) if n not in allowed_digits})
    if unsourced:
        errors.append(
            "Цифры без подтверждения во входных данных: " + ", ".join(unsourced[:8])
        )

    # 2. sources
    input_urls = {canonical_url(i.url) for i in items}
    cited = [canonical_url(str(s.get("url", ""))) for s in (payload.get("sources") or [])]
    if items and not cited:
        errors.append("В посте не указан ни один источник")
    foreign = [u for u in cited if u and input_urls and u not in input_urls]
    if foreign:
        errors.append("Ссылки, которых не было во входных данных: " + ", ".join(foreign[:3]))

    # 3. legal
    lowered = combined.lower()
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, lowered):
            errors.append(f"Запрещённая инвестиционная формулировка (шаблон: {pattern})")

    # 4. limits
    plain_len = len(strip_html(body))
    hard_limit = TG_MESSAGE_LIMIT if payload.get("needs_separate_text") else TG_CAPTION_LIMIT
    if plain_len > TG_MESSAGE_LIMIT:
        errors.append(f"Пост длиннее лимита Telegram: {plain_len} > {TG_MESSAGE_LIMIT}")
    elif plain_len > hard_limit:
        warnings.append(
            f"Пост {plain_len} символов — уйдёт как фото + отдельный текст"
        )
    if plain_len > max_chars * 1.35:
        warnings.append(f"Пост заметно длиннее целевых {max_chars} символов ({plain_len})")

    # 5. language
    if not _CYRILLIC_RE.search(body):
        errors.append("Текст поста не на русском языке")

    # 6. self-check flags the model itself raised
    for flag, value in (payload.get("self_check") or {}).items():
        if value is False:
            errors.append(f"Модель сама отметила проблему: {flag}=false")

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "numbers_checked": len(extract_numbers(combined)),
        "sources_cited": len(cited),
    }
