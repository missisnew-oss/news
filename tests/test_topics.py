"""TOPICS: only real estate, architecture and the law reach the prompts."""

from __future__ import annotations

import pytest

from pipeline import topics
from pipeline.models import NormalizedItem


def _item(title: str, summary: str = "") -> NormalizedItem:
    return NormalizedItem(item_id="i", source_id="s", category="lifestyle", title=title, summary=summary,
                          url="https://example.com/x", canonical_url="https://example.com/x",
                          published_at=None, collected_at="", lang="ru")


@pytest.mark.parametrize("title", [
    "В Дубае пройдет первый фестиваль для кудрявых людей",
    "Chef Saradhi brings the Mediterranean to the table",
    "Valery Meladze Concert at The Agenda in Dubai",
    "Капибары здорового человека: вечеринка в субботу",
    "Dubai Summer Sale: скидки до 70% в моллах",
])
def test_beauty_food_show_business_and_shopping_are_dropped(title):
    assert topics.off_topic_reason(_item(title))


@pytest.mark.parametrize("title", [
    "Properties for Sale in Dubai Marina",              # sale, but property
    "Etihad Rail открыли продажу билетов со скидкой",     # скидка, but train
    "Новый закон о салонах красоты: штрафы до 50 000 AED",  # beauty, but law
    "Chairman of Emaar unveils the new tower masterplan",  # 'hair' inside chairman
    "В Дубае обновят курорт Atlantis: реконструкция и рестораны",
    "Сделки с недвижимостью в Дубае выросли на 12%",
    "В ОАЭ объявлен трёхдневный траур, госучреждения закрыты",
])
def test_real_estate_architecture_law_and_transport_stay(title):
    assert topics.off_topic_reason(_item(title)) is None


def test_keep_on_topic_filters_and_logs(caplog):
    items = [_item("Сделки в Дубае выросли"), _item("Маникюр недели: тренды осени")]
    with caplog.at_level("INFO"):
        kept = topics.keep_on_topic(items)
    assert [i.title for i in kept] == ["Сделки в Дубае выросли"]
    assert "отброшено 1" in caplog.text
