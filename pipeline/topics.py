"""Topic gate: the channel is about real estate, architecture and the rules
of living in the UAE — nothing else reaches the prompts.

The owner: «публиковать только посты о недвижимости, архитектуре, законах,
без всяких вьющихся волос и прочего». Source channels mix that with beauty,
food, celebrities and sport; this module drops such items right after
collection, before scoring picks a story for a rubric.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from .models import NormalizedItem

log = logging.getLogger("pipeline.topics")

# Anything matching one of these (Russian stems and English words) is off
# topic unless an ON_TOPIC term is present as well: a law about hair salons
# is still a law.
OFF_TOPIC = (
    # beauty & fashion
    "волос", "причес", "причёс", "кудряв", "маникюр", "педикюр", "макияж", "косметик",
    "парикмахер", "салон красоты", "бьюти", "beauty", "hair", "makeup", "make-up",
    "manicure", "cosmetic", "skincare", "fashion week", "мода ", "модный показ",
    "подиум", "дизайнер одежды", "бренд одежды", "коллекция одежды",
    # food & nightlife
    "ресторан", "кафе ", "бранч", "brunch", "меню", "шеф-повар", "chef", "restaurant",
    "bar ", "ночной клуб", "nightclub", "вечеринк", "кофейн", "coffee shop", "десерт",
    "рецепт", "food festival", "гастроном",
    # celebrities & show business
    "звезда", "знаменитост", "celebrity", "селебрити", "kardashian", "инфлюенсер",
    "influencer", "блогер", "blogger", "тикток", "tiktok", "актёр", "актер", "певиц",
    "певец", "singer", "рэпер", "rapper", "dj ", "концерт", "concert", "гастрол",
    "сериал", "кинофестивал", "movie", "film festival",
    # sport & fitness
    "матч", "футбол", "football", "теннис", "tennis", "гольф", "golf", "марафон",
    "marathon", "фитнес", "fitness", "йога", "yoga", "диет", "похуд",
    # shopping & misc lifestyle
    "распродаж", "скидк", "sale ", "shopping festival", "шопинг", "гороскоп",
    "свадьб", "wedding", "знакомств", "dating",
)

# Real estate, architecture, the city and the law: a match keeps the item
# even when an OFF_TOPIC word is also there.
ON_TOPIC = (
    "недвиж", "квартир", "апартамент", "вилл", "таунхаус", "застройщ", "девелопер",
    "аренд", "ипотек", "рассрочк", "сделк", "dld", "rera", "ejari", "фрихолд",
    "freehold", "off-plan", "offplan", "handover", "property", "real estate",
    "apartment", "villa", "townhouse", "developer", "mortgage", "rent", "rental", "tenant",
    "propert",
    "landlord", "закон", "штраф", "правил", "постановлен", "указ", "law", "fine",
    "regulation", "decree", "виз", "visa", "резидент", "resident", "emirates id",
    "архитект", "architect", "небоскрёб", "небоскреб", "башн", "tower", "мастер-план",
    "masterplan", "master plan", "район", "district", "community", "метро", "metro",
    "дорог", "road", "мост", "bridge", "строительств", "construction", "инфраструктур",
    "infrastructure", "dewa", "salik", "rta", "парковк", "parking", "urban", "city plan",
    "муниципалитет", "municipality", "cityscape", "экспо", "expo",
    # transport, hotels as buildings, public holidays that close offices
    "поезд", "train", "rail", "аэропорт", "airport", "транспорт", "transport",
    "автобус", "bus", "курорт", "resort", "отел", "hotel", "реконструкц", "renovation",
    "траур", "выходн", "праздничн", "holiday",
)

def _pattern(keywords: tuple[str, ...], *, prefix: bool) -> re.Pattern[str]:
    """Russian entries are stems and match anywhere. Latin entries start at a
    word boundary (``hair`` must not fire on ``chairman``, ``rent`` on
    ``current``); with ``prefix`` they may continue (``propert`` → properties,
    ``tower`` → towers), otherwise they are whole words."""
    parts = []
    for kw in keywords:
        kw = kw.strip()
        if re.fullmatch(r"[a-z][a-z .-]*", kw, re.IGNORECASE):
            parts.append(r"\b" + re.escape(kw) + ("" if prefix else r"\b"))
        else:
            parts.append(re.escape(kw))
    return re.compile("|".join(parts), re.IGNORECASE)


_OFF_RE = _pattern(OFF_TOPIC, prefix=False)
_ON_RE = _pattern(ON_TOPIC, prefix=True)


def off_topic_reason(item: NormalizedItem) -> str | None:
    """The off-topic word that sinks the item, or None when it may stay."""
    haystack = f"{item.title} {item.summary}"
    hit = _OFF_RE.search(haystack)
    if not hit:
        return None
    if _ON_RE.search(haystack):
        return None
    return hit.group(0).strip().lower()


def keep_on_topic(items: Iterable[NormalizedItem]) -> list[NormalizedItem]:
    kept: list[NormalizedItem] = []
    dropped: list[tuple[str, str]] = []
    for item in items:
        reason = off_topic_reason(item)
        if reason:
            dropped.append((reason, item.title[:60]))
        else:
            kept.append(item)
    if dropped:
        sample = "; ".join(f"«{t}» ({r})" for r, t in dropped[:4])
        log.info("Не по теме канала отброшено %d: %s", len(dropped), sample)
    return kept
