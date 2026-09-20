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
