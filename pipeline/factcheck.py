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
  5. language — the body is actually Russian;
  6. confidence — every fact carries a ``confidence`` level and the text is
                worded accordingly: a ``confirmed`` fact needs two independent
                sources or one official/developer source, a ``single_source``
                fact needs a hedge («по данным …, официального подтверждения
                пока нет»), a ``rumour`` may not put its numbers in the text
                as plain fact;
  7. instagram — no link to Instagram anywhere; no mention of the platform
                in a post built from the owner's forwarded screenshot
                (docs/LEGAL.md: retold, never attributed to a platform).
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from .config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT
from .models import NormalizedItem
from .textutil import canonical_url, strip_html

log = logging.getLogger("pipeline.factcheck")

CONFIDENCE_LEVELS = ("confirmed", "single_source", "rumour")

# Source categories whose word alone confirms a fact (config/sources.yml).
# city_gov is authoritative too — but only for real government/media feeds,
# not for Telegram channels filed under the same category; see _authoritative.
AUTHORITATIVE_CATEGORIES = {"official_data", "developers"}
AUTHORITATIVE_IF_NOT_TELEGRAM = {"city_gov"}
TELEGRAM_SOURCE_TYPES = {"telegram", "telegram_private"}

# Wording that marks a single-source claim as such.
HEDGE_RE = re.compile(
    r"по данным|по информации|сообщает|сообщают|пишет|пишут|официального подтверждения"
    r"|пока не подтвержд|предположительно|ожидается|по слухам|возможно",
    re.I,
)
# Wording that must sit in the same sentence as a number taken from a rumour.
RUMOUR_RE = re.compile(r"слух|ожида|возможно|предполож|не подтвержд", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

# Forbidden investment wording — see docs/LEGAL.md for the full "нельзя → можно" table.
# The model likes to end with «PDF-гид — заберите в боте» or «в карточках выше»:
# there is no PDF, no cards, no button. The only real call to action is to
# message the owner.
PHANTOM_OFFER_PATTERNS = (
    r"\bpdf\b",
    r"по кнопке",
    r"заберите",
    r"забирайте",
    r"в боте",
    r"в карточк",
    r"каталог",
    r"чек-?лист",
    r"гайд",
)

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

# docs/LEGAL.md and prompts/from_owner.md: a screenshot of somebody else's
# Instagram post is retold in our own words, never linked and never
# attributed to the platform. A link is forbidden in every rubric; naming
# the platform at all is forbidden in the owner's-material rubric, where
# the model sees a transcribed screenshot and likes to write «в Instagram
# пишут…».
INSTAGRAM_LINK_PATTERNS = (r"instagram\.com", r"instagr\.am")
INSTAGRAM_MENTION_PATTERNS = (r"\binstagram\b", r"\binsta\b", r"инстаграм", r"\bинст[аы]\b")
INSTAGRAM_MENTION_RUBRICS = {"from_owner"}

# Numbers that carry no factual claim and never need a source.
_NOISE_NUMBERS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "0", "100"}

def _calendar_years() -> set[str]:
    """The current year and its neighbours: "в 2026 году" is a calendar fact, not a claim."""
    from datetime import datetime, timezone

    year = datetime.now(timezone.utc).year
    return {str(year - 1), str(year), str(year + 1)}


# A number is digits, optionally with space-separated thousand groups of
# exactly three digits, plus one decimal tail: "1 850 000", "12,7", "3.5".
# The previous pattern let the separator class run across a sentence
# boundary, so "Дубай, 2026. 15 проектов" yielded the phantom number
# 202615 and the post was rejected for a figure nobody had written.
_NUMBER_RE = re.compile(r"\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d+)?")
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


@lru_cache(maxsize=1)
def _registry_source_index() -> dict[str, dict[str, Any]]:
    """{source_id: source} from config/sources.yml; empty if it cannot be read."""
    try:
        from .config import load_sources

        return {s["id"]: s for s in (load_sources().get("sources") or [])}
    except Exception as exc:  # pragma: no cover - defensive: gate must not crash
        log.warning("Реестр источников недоступен для факт-чека: %s", exc)
        return {}


def _authoritative(item: NormalizedItem, source_index: dict[str, dict[str, Any]]) -> bool:
    """True when this source alone is enough to call a fact confirmed."""
    if item.category in AUTHORITATIVE_CATEGORIES:
        return True
    if item.category in AUTHORITATIVE_IF_NOT_TELEGRAM:
        source = source_index.get(item.source_id) or {}
        return source.get("type", "") not in TELEGRAM_SOURCE_TYPES and not item.source_id.startswith("tg_")
    return False


def _fact_urls(fact: dict[str, Any]) -> list[str]:
    urls = [str(u) for u in (fact.get("sources") or []) if isinstance(u, (str, bytes)) and str(u).strip()]
    primary = str(fact.get("source_url") or fact.get("url") or "").strip()
    if primary:
        urls.append(primary)
    return [canonical_url(u) for u in urls if u]


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(strip_html(text or "")) if s.strip()]


def check_confidence(
    payload: dict[str, Any],
    items: list[NormalizedItem],
    *,
    source_index: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Confidence rules for ``facts[]``; returns (errors, warnings).

    A fact without ``confidence`` is left alone (older prompts and the
    offline stub may omit it) but reported as a warning. ``confirmed`` is
    downgraded to ``single_source`` in place when its sources do not justify
    it, so the queue keeps the corrected level.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if source_index is None:
        source_index = _registry_source_index()
    by_url = {canonical_url(i.url): i for i in items if i.url}
    body = payload.get("body", "")
    plain_body = strip_html(body)
    sentences = _sentences(body)
    hedged = bool(HEDGE_RE.search(plain_body))

    for fact in payload.get("facts") or []:
        level = fact.get("confidence")
        label = str(fact.get("claim") or fact.get("value") or "?")[:60]
        if level is None:
            warnings.append(f"У факта {label!r} не указан confidence")
            continue
        if level not in CONFIDENCE_LEVELS:
            warnings.append(f"У факта {label!r} неизвестный confidence {level!r} — считаю single_source")
            level = fact["confidence"] = "single_source"

        if level == "confirmed":
            matched = [by_url[u] for u in _fact_urls(fact) if u in by_url]
            independent = {i.source_id for i in matched}
            official = any(_authoritative(i, source_index) for i in matched)
            if len(independent) < 2 and not official:
                warnings.append(
                    f"Факт {label!r} помечен confirmed без двух независимых источников "
                    "и без официального — понижен до single_source"
                )
                level = fact["confidence"] = "single_source"

        if level == "single_source" and not hedged:
            errors.append(
                f"Факт {label!r} из одного источника, а в тексте нет маркера неуверенности "
                "(«по данным …», «официального подтверждения пока нет»)"
            )

        if level == "rumour":
            digits = _digits(str(fact.get("value", "")))
            if not digits:
                continue
            for sentence in sentences:
                if digits in {_digits(n) for n in _NUMBER_RE.findall(sentence)} and not RUMOUR_RE.search(sentence):
                    errors.append(
                        f"Цифра {fact.get('value')!r} из слуха подана как факт: «{sentence[:80]}»"
                    )
                    break
    return errors, warnings


def check(
    payload: dict[str, Any],
    items: list[NormalizedItem],
    *,
    max_chars: int,
    source_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a gate verdict: {'passed': bool, 'errors': [...], 'warnings': [...]}

    ``source_index`` ({source_id: source dict from sources.yml}) tells the
    confidence check which city_gov sources are real feeds rather than
    Telegram channels; when omitted the registry is read from disk once.
    """
    errors: list[str] = []
    warnings: list[str] = []

    body = payload.get("body", "")
    combined = f"{payload.get('title', '')}\n{body}\n{payload.get('cta', '')}"

    input_urls = {canonical_url(i.url) for i in items if i.url}

    # 1. numbers
    allowed_digits = _haystack_digits(items)
    allowed_digits |= _calendar_years()
    # A declared fact only vouches for its number if it points at a URL that
    # really was in the input. Without this check the model can invent any
    # figure, list it in ``facts`` with a made-up source_url and walk straight
    # through the gate — which defeats the whole anti-hallucination rule of
    # the brief (§4). Verified by tests/test_factcheck.py.
    for fact in payload.get("facts") or []:
        digits = _digits(str(fact.get("value", "")))
        if not digits:
            continue
        fact_url = canonical_url(str(fact.get("source_url") or fact.get("url") or ""))
        if fact_url and fact_url in input_urls:
            allowed_digits.add(digits)
        else:
            warnings.append(
                f"Факт {fact.get('value')!r} ссылается на источник вне входных данных"
            )
    unsourced = sorted({n for n in extract_numbers(combined) if n not in allowed_digits})
    if unsourced:
        errors.append(
            "Цифры без подтверждения во входных данных: " + ", ".join(unsourced[:8])
        )

    # 2. sources
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
    for pattern in PHANTOM_OFFER_PATTERNS:
        if re.search(pattern, lowered):
            errors.append(
                f"Обещание несуществующего материала или механики (шаблон: {pattern}); "
                "единственный CTA — написать владелице"
            )

    mention_patterns = INSTAGRAM_LINK_PATTERNS + (
        INSTAGRAM_MENTION_PATTERNS if payload.get("rubric") in INSTAGRAM_MENTION_RUBRICS else ()
    )
    for pattern in mention_patterns:
        if re.search(pattern, lowered):
            errors.append(
                f"Ссылка или упоминание Instagram (шаблон: {pattern}); чужой пост пересказываем "
                "своими словами, платформу и аккаунт не называем (docs/LEGAL.md)"
            )
            break

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

    # 7. confidence of each fact vs. the wording of the text
    conf_errors, conf_warnings = check_confidence(payload, items, source_index=source_index)
    errors.extend(conf_errors)
    warnings.extend(conf_warnings)

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "numbers_checked": len(extract_numbers(combined)),
        "sources_cited": len(cited),
    }
