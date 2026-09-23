"""Telegram channels as a source: the t.me/s/<name> preview parser and its wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import collect, verify_sources
from pipeline.config import ALLOWED_SOURCE_TYPES, load_sources, validate_sources_doc
from pipeline.normalize import normalize_one, scrub_untrusted
from pipeline.telegram_source import channel_username, has_preview, parse_preview, preview_url

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = {"id": "tg_demo", "type": "telegram", "category": "city_gov", "lang": "ru",
          "url": "https://t.me/s/demo_channel"}


@pytest.fixture
def preview_html() -> bytes:
    return (FIXTURES / "tme_preview_sample.html").read_bytes()


@pytest.fixture
def no_preview_html() -> bytes:
    return (FIXTURES / "tme_no_preview_sample.html").read_bytes()


def test_preview_parses_text_posts_newest_first(preview_html):
    items = parse_preview(preview_html, SOURCE)
    assert [i["url"] for i in items] == [
        "https://t.me/demo_channel/103", "https://t.me/demo_channel/101",
    ], "пост 102 — только фото без текста, его пересказывать нечего"
    newest = items[0]
    assert newest["source_id"] == "tg_demo"
    assert newest["title"] == "Застройщик открыл продажи в новой башне на набережной"
    assert "320 квартир, рассрочка 60/40" in newest["summary"]
    assert newest["published_at"] == "2026-09-20T10:15:03+00:00"
    assert newest["image_url"] == "https://cdn4.telesco.pe/file/demo103.jpg"
    assert newest["views"] == 1_200_000
    assert items[1]["views"] == 12_300
    assert items[1]["image_url"] is None


def test_entities_and_markup_do_not_leak_into_text(preview_html):
    items = parse_preview(preview_html, SOURCE)
    old = items[1]
    assert "&amp;" not in old["summary"] and "регулятора & в приложении" in old["summary"]
    assert "<" not in items[0]["summary"]


def test_injection_inside_a_channel_post_is_scrubbed_by_normalize(preview_html):
    items = parse_preview(preview_html, SOURCE)
    item = normalize_one(items[0], {"tg_demo": SOURCE})
    assert item is not None
    assert "ignore previous instructions" not in item.summary.lower()
    assert "ignore previous instructions" not in scrub_untrusted(items[0]["raw_text"]).lower()
    assert item.category == "city_gov" and item.lang == "ru"


def test_private_or_hidden_channel_is_detected(no_preview_html, preview_html):
    assert has_preview(preview_html)
    assert not has_preview(no_preview_html)
    assert parse_preview(no_preview_html, SOURCE) == []


def test_limit_is_respected(preview_html):
    assert len(parse_preview(preview_html, SOURCE, limit=1)) == 1


@pytest.mark.parametrize("url,expected", [
    ("https://t.me/s/uaegeneralnews", "uaegeneralnews"),
    ("https://t.me/uaegeneralnews", "uaegeneralnews"),
    ("https://t.me/+AbCdEf", None),
    ("https://example.com/s/x", None),
])
def test_channel_username_extraction(url, expected):
    assert channel_username(url) == expected


def test_preview_url_strips_at_sign():
    assert preview_url("@NeginskiUAE") == "https://t.me/s/NeginskiUAE"


def test_collector_knows_the_telegram_type():
    assert "telegram" in ALLOWED_SOURCE_TYPES
    assert collect.PARSERS["telegram"] is parse_preview


def test_registry_rejects_a_telegram_source_with_a_wrong_url():
    doc = load_sources()
    bad = dict(doc["sources"][0], id="tg_bad", type="telegram", url="https://t.me/+invite")
    with pytest.raises(ValueError, match="t.me/s"):
        validate_sources_doc({**doc, "sources": [bad]})


def test_owner_channels_are_registered_and_allowed():
    doc = load_sources()
    tg = [s for s in doc["sources"] if s["type"] == "telegram"]
    usernames = {s["channel"].lower() for s in tg}
    assert usernames >= {
        "@eltsovairina_80", "@offplanmariya", "@dubaimap", "@neginskiuae",
        "@burjuyinvest", "@dubai_invest1", "@uaegeneralnews", "@russianemiratesnews",
    }
    for s in tg:
        assert s["url"] == f"https://t.me/s/{s['channel'][1:]}"
        assert s["role"] in {"news", "signal"}
        assert s["enabled"], "разрешён в реестре; включает его живая проверка, не YAML"


def test_verify_marks_hidden_preview_as_failed(monkeypatch, no_preview_html, preview_html):
    class Resp:
        def __init__(self, body):
            self.status_code, self.content, self.headers = 200, body, {"Content-Type": "text/html"}

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(no_preview_html))
    verdict = verify_sources.check_source(SOURCE, timeout=5, user_agent="t")
    assert verdict["status"] == "failed" and "превью" in verdict["note"]

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(preview_html))
    verdict = verify_sources.check_source(SOURCE, timeout=5, user_agent="t")
    assert verdict["status"] == "ok"
    assert verdict["items_found"] == 2
    assert verdict["latest_item_at"] == "2026-09-20T10:15:03+00:00"


def test_text_with_nested_divs_and_no_footer_is_still_extracted():
    block = (
        '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="c/7">'
        '<div class="tgme_widget_message_text js-message_text" dir="auto">Цитата ниже:'
        '<div class="quote">Внутренний блок с 15 словами о рынке</div> и хвост текста</div></div></div>'
    )
    items = parse_preview(block, SOURCE)
    assert len(items) == 1
    assert items[0]["summary"] == "Цитата ниже: Внутренний блок с 15 словами о рынке и хвост текста"


# --------------------------------------------------------------------------
# Paging: a busy channel is read back 36 hours, not just its last 20 posts
# --------------------------------------------------------------------------

def _page(username: str, ids: list[int], when: str) -> bytes:
    blocks = []
    for i in ids:
        blocks.append(
            f'<div class="tgme_widget_message_wrap"><div data-post="{username}/{i}">'
            f'<div class="tgme_widget_message_text js-message_text">Новость номер {i} про Дубай, длинная строка</div>'
            f'<time datetime="{when}"></time></div></div>'
        )
    return ("<html>" + "".join(blocks) + "</html>").encode()


def test_fetch_telegram_pages_back_until_the_lookback(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from pipeline import collect

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(hours=60)).isoformat()
    calls: list[str] = []

    def fake_get(url, *, timeout, user_agent):
        calls.append(url)
        if "before=" not in url:
            return 200, _page("busy", list(range(81, 101)), recent), "text/html"
        if url.endswith("before=81"):
            return 200, _page("busy", list(range(61, 81)), recent), "text/html"
        return 200, _page("busy", list(range(41, 61)), old), "text/html"

    src = {"id": "tg_busy", "type": "telegram", "url": "https://t.me/s/busy", "category": "city_gov", "lang": "ru"}
    items = collect.fetch_telegram(src, timeout=5, user_agent="t", lookback_hours=36, max_pages=5,
                                   max_items=80, http_get=fake_get)
    assert calls == ["https://t.me/s/busy", "https://t.me/s/busy?before=81", "https://t.me/s/busy?before=61"]
    assert len(items) == 60 and items[0]["url"].endswith("/100") and items[-1]["url"].endswith("/41")


def test_fetch_telegram_stops_at_max_pages_and_max_items(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from pipeline import collect

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def fake_get(url, *, timeout, user_agent):
        start = 200 if "before=" not in url else int(url.rsplit("=", 1)[1]) - 20
        return 200, _page("busy", list(range(start, start + 20)), recent), "text/html"

    src = {"id": "tg_busy", "type": "telegram", "url": "https://t.me/s/busy", "category": "city_gov", "lang": "ru"}
    items = collect.fetch_telegram(src, timeout=5, user_agent="t", lookback_hours=36, max_pages=2,
                                   max_items=80, http_get=fake_get)
    assert len(items) == 40
    items = collect.fetch_telegram(src, timeout=5, user_agent="t", lookback_hours=36, max_pages=10,
                                   max_items=30, http_get=fake_get)
    assert len(items) == 30


def test_fetch_telegram_first_page_failure_is_empty_not_fatal():
    from pipeline import collect

    src = {"id": "tg_busy", "type": "telegram", "url": "https://t.me/s/busy", "category": "city_gov", "lang": "ru"}
    items = collect.fetch_telegram(src, timeout=5, user_agent="t", lookback_hours=36, max_pages=3,
                                   max_items=80, http_get=lambda url, **kw: (404, b"", "text/html"))
    assert items == []
