"""config/sources.yml is a contract between the researcher and the pipeline."""

from __future__ import annotations

import pytest

from pipeline.config import (
    ALLOWED_REUSE, ALLOWED_SOURCE_TYPES, SOURCES_FILE, enabled_sources,
    load_sources, validate_sources_doc,
)

REQUIRED_CATEGORIES = {
    "realty_news", "official_data", "developers", "city_gov", "events", "lifestyle",
}


def test_registry_file_exists():
    assert SOURCES_FILE.exists(), "config/sources.yml отсутствует"


def test_registry_loads_and_validates():
    doc = load_sources()
    assert doc["version"] == 1
    assert doc["sources"], "реестр источников пуст"


def test_all_six_brief_categories_are_declared():
    doc = load_sources()
    assert REQUIRED_CATEGORIES <= set(doc["categories"]), (
        "в реестре не хватает категорий из раздела 5 брифа"
    )


def test_every_category_has_at_least_one_source():
    doc = load_sources()
    covered = {s["category"] for s in doc["sources"]}
    missing = REQUIRED_CATEGORIES - covered
    assert not missing, f"категории без источников: {sorted(missing)}"


def test_source_ids_are_unique_and_snake_case():
    doc = load_sources()
    ids = [s["id"] for s in doc["sources"]]
    assert len(ids) == len(set(ids))
    assert all(i.islower() and " " not in i for i in ids)


def test_types_and_licences_are_from_the_allowed_sets():
    doc = load_sources()
    for source in doc["sources"]:
        assert source["type"] in ALLOWED_SOURCE_TYPES
        assert (source.get("license") or {}).get("reuse", "unknown") in ALLOWED_REUSE


def test_registry_alone_never_switches_a_source_on():
    """The brief forbids collecting from a URL nobody ever requested:
    without a health snapshot from a real check nothing is active."""
    doc = load_sources()
    assert enabled_sources(doc, health={}) == []
    assert enabled_sources(doc, health={"sources": {}}) == []


def test_enabled_sources_helper_filters_correctly():
    doc = {
        "categories": {"realty_news": {"weight": 1.0}},
        "sources": [
            {"id": "a", "title": "A", "url": "https://example.com/a", "type": "rss",
             "category": "realty_news", "lang": "en", "enabled": True},
            {"id": "b", "title": "B", "url": "https://example.com/b", "type": "rss",
             "category": "realty_news", "lang": "en", "enabled": True},
            {"id": "c", "title": "C", "url": "https://example.com/c", "type": "rss",
             "category": "realty_news", "lang": "en", "enabled": False},
        ],
    }
    validate_sources_doc(doc)
    health = {"sources": {"a": {"status": "ok"}, "b": {"status": "failed"}, "c": {"status": "ok"}}}
    assert [s["id"] for s in enabled_sources(doc, health)] == ["a"], (
        "a — разрешён и живой; b — разрешён, но упал; c — живой, но выключен владельцем"
    )


def test_enabled_sources_reads_the_health_snapshot_from_state(isolated_state):
    from pipeline import state

    doc = {"categories": {"realty_news": {}}, "sources": [
        {"id": "a", "title": "A", "url": "https://example.com/a", "type": "rss",
         "category": "realty_news", "lang": "en", "enabled": True}]}
    assert enabled_sources(doc) == []
    state.save("sources_health.json", {"version": 1, "sources": {"a": {"status": "ok"}}})
    assert [s["id"] for s in enabled_sources(doc)] == ["a"]


def test_validator_rejects_a_duplicate_id():
    doc = {
        "categories": {"realty_news": {"weight": 1.0}},
        "sources": [
            {"id": "a", "title": "A", "url": "https://example.com/a", "type": "rss",
             "category": "realty_news", "lang": "en"},
            {"id": "a", "title": "A2", "url": "https://example.com/c", "type": "rss",
             "category": "realty_news", "lang": "en"},
        ],
    }
    with pytest.raises(ValueError, match="дублирующийся"):
        validate_sources_doc(doc)


def test_validator_rejects_unknown_category():
    doc = {
        "categories": {"realty_news": {"weight": 1.0}},
        "sources": [{"id": "a", "title": "A", "url": "https://example.com/a", "type": "rss",
                     "category": "nope", "lang": "en"}],
    }
    with pytest.raises(ValueError, match="category"):
        validate_sources_doc(doc)
