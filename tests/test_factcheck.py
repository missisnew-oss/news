"""The anti-hallucination gate: every number must trace back to the input."""

from __future__ import annotations

from datetime import datetime, timezone

from pipeline.factcheck import check, extract_numbers
from pipeline.models import NormalizedItem


def _item(summary: str, url: str = "https://example.com/a") -> NormalizedItem:
    return NormalizedItem(
        item_id="i1", source_id="s1", category="realty_news",
        title="Источник данных о рынке", summary=summary, url=url,
        canonical_url=url, published_at="2026-09-19T08:00:00+00:00",
        collected_at=datetime.now(timezone.utc).isoformat(), lang="en",
        raw_text=summary,
    )


def _payload(body: str, sources=None, facts=None) -> dict:
    return {
        "rubric": "market_pulse", "title": "Заголовок", "body": body,
        "hashtags": [], "cta": "",
        "image": {"mode": "card"},
        "sources": sources if sources is not None else [
            {"source_id": "s1", "title": "t", "url": "https://example.com/a"}
        ],
        "facts": facts or [],
        "self_check": {
            "all_numbers_have_source": True, "no_guaranteed_returns": True,
            "language_is_russian": True, "no_verbatim_copy": True,
            "fits_telegram_limit": True,
        },
        "length_chars": len(body), "needs_separate_text": False,
    }


def test_extract_numbers_ignores_urls_and_small_numbers():
    numbers = extract_numbers("Смотри https://example.com/2026 — всего 5 штук и 43000 сделок")
    assert "43000" in numbers
    assert "5" not in numbers


def test_number_present_in_input_passes():
    items = [_item("Total registered transactions: 43000 in the quarter.")]
    verdict = check(_payload("За квартал зарегистрировано 43000 сделок."), items, max_chars=900)
    assert verdict["passed"], verdict["errors"]


def test_invented_number_is_rejected():
    items = [_item("Total registered transactions: 43000 in the quarter.")]
    verdict = check(_payload("Доходность составила 87654 дирхама."), items, max_chars=900)
    assert not verdict["passed"]
    assert any("без подтверждения" in e for e in verdict["errors"])


def test_number_declared_in_facts_is_accepted():
    items = [_item("Transactions reported.")]
    payload = _payload(
        "Средняя цена 1450000 дирхамов.",
        facts=[{"claim": "средняя цена", "value": "1450000", "source_url": "https://example.com/a"}],
    )
    assert check(payload, items, max_chars=900)["passed"]


def test_guaranteed_return_wording_is_rejected():
    items = [_item("Market update.")]
    verdict = check(_payload("Это гарантированная доходность для инвестора."), items, max_chars=900)
    assert not verdict["passed"]
    assert any("формулировка" in e for e in verdict["errors"])


def test_foreign_source_url_is_rejected():
    items = [_item("Market update.")]
    payload = _payload("Текст без цифр.", sources=[
        {"source_id": "x", "title": "t", "url": "https://not-in-input.example.org/z"}
    ])
    verdict = check(payload, items, max_chars=900)
    assert not verdict["passed"]
    assert any("не было во входных данных" in e for e in verdict["errors"])


def test_post_without_any_source_is_rejected():
    items = [_item("Market update.")]
    verdict = check(_payload("Текст без цифр.", sources=[]), items, max_chars=900)
    assert not verdict["passed"]


def test_non_russian_body_is_rejected():
    items = [_item("Market update.")]
    verdict = check(_payload("This post is entirely in English."), items, max_chars=900)
    assert not verdict["passed"]
    assert any("русском" in e for e in verdict["errors"])


def test_model_self_reported_problem_fails_the_gate():
    items = [_item("Market update.")]
    payload = _payload("Текст без цифр.")
    payload["self_check"]["no_verbatim_copy"] = False
    verdict = check(payload, items, max_chars=900)
    assert not verdict["passed"]


def test_long_post_warns_but_does_not_fail():
    items = [_item("Market update.")]
    verdict = check(_payload("Текст. " * 250), items, max_chars=900)
    assert verdict["passed"], verdict["errors"]
    assert verdict["warnings"]


# --------------------------------------------------------------------------
# Confidence of facts vs. wording of the text
# --------------------------------------------------------------------------

def _tg_item(item_id: str, source_id: str, summary: str, category: str = "realty_news") -> NormalizedItem:
    url = f"https://t.me/{source_id}/{item_id}"
    return NormalizedItem(
        item_id=item_id, source_id=source_id, category=category,
        title="Пересказ новости каналом", summary=summary, url=url,
        canonical_url=url, published_at="2026-09-19T08:00:00+00:00",
        collected_at=datetime.now(timezone.utc).isoformat(), lang="ru",
        raw_text=summary,
    )


def _official_item(summary: str) -> NormalizedItem:
    url = "https://dubailand.gov.ae/report"
    return NormalizedItem(
        item_id="o1", source_id="dld_open_data_portal", category="official_data",
        title="Официальный отчёт регулятора", summary=summary, url=url,
        canonical_url=url, published_at="2026-09-19T08:00:00+00:00",
        collected_at=datetime.now(timezone.utc).isoformat(), lang="en",
        raw_text=summary,
    )


def _fact(value: str, confidence: str, sources: list[str]) -> dict:
    return {"claim": "цифра", "value": value, "source_url": sources[0],
            "confidence": confidence, "sources": sources}


def test_single_source_fact_without_a_hedge_is_rejected():
    items = [_tg_item("1", "tg_one", "Застройщик продал 357 вилл за день.")]
    payload = _payload(
        "Застройщик продал 357 вилл за день.",
        sources=[{"source_id": "tg_one", "title": "t", "url": items[0].url}],
        facts=[_fact("357", "single_source", [items[0].url])],
    )
    verdict = check(payload, items, max_chars=900, source_index={})
    assert not verdict["passed"]
    assert any("маркера неуверенности" in e for e in verdict["errors"])


def test_single_source_fact_with_a_hedge_passes():
    items = [_tg_item("1", "tg_one", "Застройщик продал 357 вилл за день.")]
    payload = _payload(
        "По данным канала, застройщик продал 357 вилл за день. "
        "Официального подтверждения пока нет.",
        sources=[{"source_id": "tg_one", "title": "t", "url": items[0].url}],
        facts=[_fact("357", "single_source", [items[0].url])],
    )
    verdict = check(payload, items, max_chars=900, source_index={})
    assert verdict["passed"], verdict["errors"]


def test_confirmed_needs_two_independent_sources_or_an_official_one():
    a = _tg_item("1", "tg_one", "Продано 357 вилл.")
    b = _tg_item("2", "tg_two", "Всего 357 вилл ушли за день.")
    body = "Застройщик продал 357 вилл за день."
    sources = [{"source_id": "tg_one", "title": "t", "url": a.url}]

    # One channel only: downgraded to single_source, and the text has no hedge.
    lonely = _payload(body, sources=sources, facts=[_fact("357", "confirmed", [a.url])])
    verdict = check(lonely, [a, b], max_chars=900, source_index={})
    assert not verdict["passed"]
    assert lonely["facts"][0]["confidence"] == "single_source"
    assert any("понижен до single_source" in w for w in verdict["warnings"])

    # Two independent channels: confirmed stands, no hedge needed.
    two = _payload(body, sources=sources, facts=[_fact("357", "confirmed", [a.url, b.url])])
    verdict = check(two, [a, b], max_chars=900, source_index={})
    assert verdict["passed"], verdict["errors"]
    assert two["facts"][0]["confidence"] == "confirmed"

    # A single official source is enough on its own.
    official = _official_item("357 villas sold.")
    one_official = _payload(
        body, sources=[{"source_id": official.source_id, "title": "t", "url": official.url}],
        facts=[_fact("357", "confirmed", [official.url])],
    )
    verdict = check(one_official, [official], max_chars=900, source_index={})
    assert verdict["passed"], verdict["errors"]

    # A made-up second URL does not count as a second source.
    padded = _payload(body, sources=sources,
                      facts=[_fact("357", "confirmed", [a.url, "https://invented.example/x"])])
    assert not check(padded, [a, b], max_chars=900, source_index={})["passed"]


def test_city_gov_counts_as_official_only_for_real_feeds_not_telegram_channels():
    url = "https://mediaoffice.ae/news/1"
    feed = NormalizedItem(
        item_id="g1", source_id="dubai_media_office_news", category="city_gov",
        title="Government announcement about fees", summary="Fee is 1500 AED.", url=url,
        canonical_url=url, published_at=None, collected_at=datetime.now(timezone.utc).isoformat(),
        lang="en", raw_text="Fee is 1500 AED.",
    )
    channel = _tg_item("7", "tg_city", "Пошлина — 1500 AED.", category="city_gov")
    index = {
        "dubai_media_office_news": {"id": "dubai_media_office_news", "type": "html", "category": "city_gov"},
        "tg_city": {"id": "tg_city", "type": "telegram", "category": "city_gov"},
    }
    body = "Пошлина составляет 1500 AED."
    ok = _payload(body, sources=[{"source_id": feed.source_id, "title": "t", "url": url}],
                  facts=[_fact("1500", "confirmed", [url])])
    assert check(ok, [feed], max_chars=900, source_index=index)["passed"]
    bad = _payload(body, sources=[{"source_id": "tg_city", "title": "t", "url": channel.url}],
                   facts=[_fact("1500", "confirmed", [channel.url])])
    assert not check(bad, [channel], max_chars=900, source_index=index)["passed"]


def test_rumour_number_needs_a_rumour_marker_in_the_same_sentence():
    items = [_tg_item("1", "tg_one", "По нашей информации, старт продаж — от 1200000 AED.")]
    sources = [{"source_id": "tg_one", "title": "t", "url": items[0].url}]

    plain = _payload(
        "Пока это на уровне слухов. Цены стартуют от 1200000 AED.",
        sources=sources, facts=[_fact("1200000", "rumour", [items[0].url])],
    )
    verdict = check(plain, items, max_chars=900, source_index={})
    assert not verdict["passed"]
    assert any("из слуха подана как факт" in e for e in verdict["errors"])

    hedged = _payload(
        "По слухам, цены стартуют от 1200000 AED. Официального подтверждения пока нет.",
        sources=sources, facts=[_fact("1200000", "rumour", [items[0].url])],
    )
    verdict = check(hedged, items, max_chars=900, source_index={})
    assert verdict["passed"], verdict["errors"]


def test_fact_without_confidence_only_warns():
    items = [_item("Transactions: 43000.")]
    payload = _payload(
        "За квартал 43000 сделок.",
        facts=[{"claim": "сделки", "value": "43000", "source_url": "https://example.com/a"}],
    )
    verdict = check(payload, items, max_chars=900, source_index={})
    assert verdict["passed"], verdict["errors"]
    assert any("не указан confidence" in w for w in verdict["warnings"])


# --- QA round 2 -------------------------------------------------------------

def test_instagram_link_is_rejected_in_any_rubric():
    items = [_item("Total registered transactions: 43000 in the quarter.")]
    payload = _payload('За квартал 43000 сделок, подробнее на <a href="https://instagram.com/x">странице</a>.')
    verdict = check(payload, items, max_chars=900)
    assert not verdict["passed"]
    assert any("Instagram" in e for e in verdict["errors"])


def test_instagram_mention_is_rejected_only_for_owner_material():
    items = [_item("Total registered transactions: 43000 in the quarter.")]
    body = "В Инстаграме пишут: за квартал 43000 сделок."
    assert check(_payload(body), items, max_chars=900)["passed"], "в новостной рубрике слово само по себе допустимо"
    payload = _payload(body)
    payload["rubric"] = "from_owner"
    verdict = check(payload, items, max_chars=900)
    assert not verdict["passed"]
    assert any("Instagram" in e for e in verdict["errors"])
