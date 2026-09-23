"""Junk detector: obvious filler is pushed down in the ranking.

The owner does not want topics banned — a story about anything can matter to
an expat or an investor. What she does not want is filler: contests,
giveaways, horoscopes, celebrity gossip, «10 лучших кафе» listicles («конкурс
среди кудрявых волос — это бред»). This module spots such items by wording
and hands ``score.penalty`` a reason, so they sink below real news instead of
being dropped outright. The judgement of *significance* itself is made by the
model against the «Кому это важно» test in prompts/system_tone.md.
"""

from __future__ import annotations

import logging
import re

from .models import NormalizedItem

log = logging.getLogger("pipeline.topics")

# Wording of filler content. Russian entries are stems (match anywhere),
# Latin entries are whole words.
JUNK = (
    # contests, giveaways, promo
    "конкурс", "розыгрыш", "giveaway", "промокод", "promo code", "скидка по коду",
    "успейте", "только сегодня", "реклама", "партнёрский материал", "партнерский материал",
    # horoscopes, dating, gossip
    "гороскоп", "horoscope", "знакомств", "dating", "сплетн", "gossip", "звезда",
    "знаменитост", "celebrity", "kardashian", "инфлюенсер", "influencer", "тикток", "tiktok",
    # beauty and body as entertainment
    "кудряв", "вьющ", "волос", "маникюр", "макияж", "причёск", "прическ", "hair", "makeup",
    "manicure",
    # listicles and filler formats
    "топ-10", "топ 10", "top 10", "лучших кафе", "лучшие кафе", "лучших ресторанов",
    "рецепт", "recipe", "тест:", "quiz", "угадай", "мем дня", "капибар",
    # food and nightlife as such
    "бранч", "brunch", "вечеринк", "party", "ночной клуб", "nightclub", "шеф-повар", "chef",
    # sport results and fitness
    "матч", "football", "теннис", "tennis", "гольф", "golf", "фитнес", "fitness", "йога", "yoga",
)


def _pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    parts = []
    for kw in keywords:
        kw = kw.strip()
        if re.fullmatch(r"[a-z][a-z .:-]*", kw, re.IGNORECASE):
            parts.append(r"\b" + re.escape(kw) + r"\b")
        else:
            parts.append(re.escape(kw))
    return re.compile("|".join(parts), re.IGNORECASE)


_JUNK_RE = _pattern(JUNK)


def junk_reason(item: NormalizedItem) -> str | None:
    """The filler wording found in the item, or None."""
    hit = _JUNK_RE.search(f"{item.title} {item.summary}")
    return hit.group(0).strip().lower() if hit else None
