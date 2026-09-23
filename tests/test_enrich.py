"""ENRICH: the article behind a channel post reaches the model."""

from __future__ import annotations

from pipeline import enrich
from pipeline.config import Settings
from pipeline.models import NormalizedItem

PAGE = b"""<html><head><title>Dubizzle launches direct booking | Gulf News</title>
<script>var x = 1;</script><style>p{}</style></head><body><nav><p>Home News Business Menu links here</p></nav>
<article><h1>Dubizzle launches direct booking for holiday homes</h1>
<p>Dubizzle has launched direct booking for short-term rentals across the UAE, letting guests reserve licensed holiday homes on the platform.</p>
<p>More than 10,000 short-term rental listings are posted on the platform every month, the company said, all managed by professional operators.</p>
<p>Short.</p></article><footer><p>Copyright notice that should be removed from the article text entirely.</p></footer></body></html>"""


def _item(summary: str, url: str = "https://t.me/dubaimap/1", raw: str = "") -> NormalizedItem:
    return NormalizedItem(item_id="i1", source_id="tg_dubaimap", category="lifestyle", title="Dubizzle",
                          summary=summary, url=url, canonical_url=url, published_at=None, collected_at="",
                          lang="ru", raw_text=raw or summary)


def test_extract_article_keeps_paragraphs_and_drops_chrome():
    text = enrich.extract_article(PAGE)
    assert "10,000 short-term rental listings" in text
    assert "Copyright" not in text and "Menu links" not in text and "var x" not in text
    assert "Short." not in text


def test_linked_url_skips_social_links_and_picks_the_article():
    item = _item("Новость. Подпишитесь: https://max.ru/russianemirates и https://t.me/x "
                 "Подробнее: https://gulfnews.com/business/property/dubizzle-launches-1.23?ref=telegram.")
    assert enrich.linked_url(item) == "https://gulfnews.com/business/property/dubizzle-launches-1.23?ref=telegram"
    assert enrich.linked_url(_item("Без ссылок вообще")) is None


def test_own_page_is_fetched_only_for_teasers():
    teaser = _item("Short teaser.", url="https://gulfnews.com/uae/story-1")
    assert enrich.own_page_url(teaser) == "https://gulfnews.com/uae/story-1"
    full = _item("x" * 600, url="https://gulfnews.com/uae/story-1")
    assert enrich.own_page_url(full) is None
    assert enrich.own_page_url(_item("Short", url="https://t.me/dubaimap/1")) is None


def test_enrich_appends_the_article_and_is_idempotent(monkeypatch):
    enrich._CACHE.clear()
    calls = []

    def fake_get(url, *, timeout, user_agent):
        calls.append(url)
        return 200, PAGE, "text/html; charset=utf-8"

    item = _item("Dubizzle запустил бронирование. Подробнее: https://gulfnews.com/story")
    n = enrich.enrich_items([item], Settings(dry_run=False), http_get=fake_get)
    assert n == 1 and enrich.LABEL in item.summary and "10,000" in item.summary
    assert enrich.enrich_items([item], Settings(dry_run=False), http_get=fake_get) == 0
    assert calls == ["https://gulfnews.com/story"]


def test_enrich_is_silent_in_dry_run_and_on_errors():
    def boom(url, *, timeout, user_agent):
        raise RuntimeError("no network")

    item = _item("Подробнее: https://gulfnews.com/story2")
    assert enrich.enrich_items([item], Settings(dry_run=True), http_get=boom) == 0
    enrich._CACHE.clear()
    assert enrich.enrich_items([item], Settings(dry_run=False), http_get=boom) == 0
    assert enrich.LABEL not in item.summary
