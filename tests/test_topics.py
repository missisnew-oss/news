"""TOPICS: filler is recognised and sinks in the ranking; nothing is banned."""

from __future__ import annotations

import pytest

from pipeline import score, topics
from pipeline.models import NormalizedItem


def _item(title: str, summary: str = "Подробности в материале источника.") -> NormalizedItem:
    return NormalizedItem(item_id="i", source_id="s", category="lifestyle", title=title, summary=summary,
                          url="https://example.com/x", canonical_url="https://example.com/x",
                          published_at=None, collected_at="", lang="ru")


@pytest.mark.parametrize("title", [
    "В Дубае пройдет первый фестиваль для кудрявых людей",
    "Конкурс: выиграйте ужин на двоих",
    "Гороскоп на неделю для жителей Эмиратов",
    "Топ-10 лучших кафе Дубая для завтрака",
    "Капибары здорового человека: вечеринка в субботу",
])
def test_filler_is_recognised(title):
    assert topics.junk_reason(_item(title))


@pytest.mark.parametrize("title", [
    "Сделки с недвижимостью в Дубае выросли на 12%",
    "Новый закон о салонах красоты: штрафы до 50 000 AED",
    "Chairman of Emaar unveils the new tower masterplan",   # 'hair' inside chairman
    "Etihad Rail открыли продажу билетов Дубай — Абу-Даби",
    "Coldplay сыграют на стадионе Zayed Sports City",        # a city-scale event is not filler
    "В Дубае откроют новый ресторанный квартал у канала",  # a topic is not a ban
])
def test_real_news_is_not_filler(title):
    assert topics.junk_reason(_item(title)) is None


def test_filler_loses_to_real_news_in_the_score():
    filler = _item("В Дубае пройдет первый фестиваль для кудрявых людей")
    news = _item("В Дубае открыли новую станцию метро")
    assert score.penalty(filler) > score.penalty(news) + 0.5
