"""Regression tests for defects found during the QA pass.

Every test here pins down a bug that shipped and was fixed. The comment above
each one says what used to happen, so a future refactor that reintroduces the
behaviour fails loudly instead of silently breaking the channel.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
from datetime import datetime, timezone

import pytest

from pipeline import factcheck, score, state
from pipeline.config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT, Settings
from pipeline.logging_setup import RedactingFilter, setup_logging
from pipeline.models import NormalizedItem
from pipeline.textutil import (
    escape_text, plan_delivery, sanitize_telegram_html, split_for_telegram,
    truncate_html,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^<>]*>")


def assert_balanced(fragment: str) -> None:
    """Every opened Telegram tag is closed, in order, and nothing is stray."""
    stack: list[str] = []
    for match in _TAG.finditer(fragment):
        name = match.group(1).lower()
        if match.group(0).startswith("</"):
            assert stack and stack[-1] == name, (
                f"закрывающий </{name}> без пары в: {fragment[:120]!r}"
            )
            stack.pop()
        else:
            stack.append(name)
    assert not stack, f"незакрытые теги {stack} в: {fragment[:120]!r}"


def _item(title: str, summary: str, url: str = "https://example.com/a") -> NormalizedItem:
    return NormalizedItem(
        item_id="i1", source_id="s1", category="realty_news", title=title,
        summary=summary, url=url, canonical_url=url, published_at=None,
        collected_at=datetime.now(timezone.utc).isoformat(), lang="ru",
        raw_text=summary,
    )


def _payload(body: str, facts=None) -> dict:
    return {
        "rubric": "market_pulse", "title": "Заголовок", "body": body,
        "hashtags": [], "cta": "", "image": {"mode": "card"},
        "sources": [{"source_id": "s1", "title": "t", "url": "https://example.com/a"}],
        "facts": facts or [],
        "self_check": {
            "all_numbers_have_source": True, "no_guaranteed_returns": True,
            "language_is_russian": True, "no_verbatim_copy": True,
            "fits_telegram_limit": True,
        },
        "length_chars": len(body), "needs_separate_text": False,
    }


# --------------------------------------------------------------------------
# 1. Telegram HTML: bare &, < and > used to be sent unescaped
# --------------------------------------------------------------------------

def test_bare_ampersand_and_angle_brackets_are_escaped():
    """Was: "Emaar & Nakheel" reached Telegram raw and sendMessage failed with
    "can't parse entities", losing the post permanently."""
    clean = sanitize_telegram_html("Emaar & Nakheel: цена < 1 млн, рост > 5%")
    assert "&amp;" in clean and "&lt;" in clean and "&gt;" in clean
    assert " & " not in clean


def test_existing_entities_are_not_double_escaped():
    assert escape_text("уже &amp; и &#1055;") == "уже &amp; и &#1055;"


def test_unbalanced_markup_from_the_model_is_repaired():
    """Was: a stray </u> or an unclosed <b> went straight to the API."""
    clean = sanitize_telegram_html("<b>жирный без закрытия и стрей </u> дальше")
    assert_balanced(clean)


def test_unsafe_link_is_dropped_but_text_survives():
    clean = sanitize_telegram_html('<a href="javascript:alert(1)">клик</a> дальше')
    assert "javascript" not in clean
    assert "клик дальше" in clean
    assert_balanced(clean)


def test_tg_spoiler_is_allowed():
    assert sanitize_telegram_html("<tg-spoiler>тайна</tg-spoiler>") == (
        "<tg-spoiler>тайна</tg-spoiler>"
    )


# --------------------------------------------------------------------------
# 2. Splitting used to cut <b>…</b> in half
# --------------------------------------------------------------------------

def test_split_never_leaves_a_tag_open_across_chunks():
    """Was: chunk 1 ended with an unclosed <b> and chunk 2 began with a stray
    </b>; Telegram rejected both messages."""
    text = sanitize_telegram_html("<b>" + "слово " * 900 + "</b>")
    chunks = split_for_telegram(text, TG_MESSAGE_LIMIT)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TG_MESSAGE_LIMIT
        assert_balanced(chunk)


def test_caption_split_keeps_both_halves_valid():
    body = sanitize_telegram_html(
        "х" * 1010 + "<b>жирный разрезаемый кусок</b>" + "я" * 3000
    )
    plan = plan_delivery(body, has_photo=True)
    assert plan["mode"] == "photo_plus_text"
    assert len(plan["caption"]) <= TG_CAPTION_LIMIT
    assert_balanced(plan["caption"])
    for chunk in plan["texts"]:
        assert len(chunk) <= TG_MESSAGE_LIMIT
        assert_balanced(chunk)


def test_truncate_html_does_not_cut_inside_a_tag():
    text = "слово " * 50 + '<a href="https://example.com/very/long/path">ссылка</a>'
    for limit in range(40, 400, 7):
        out = truncate_html(text, limit)
        assert_balanced(out)


# --------------------------------------------------------------------------
# 3. Fact-check gate
# --------------------------------------------------------------------------

def test_declared_fact_with_a_made_up_source_does_not_whitelist_a_number():
    """Was: any value listed in ``facts`` was trusted, so the model could
    invent a figure, cite a URL that was never collected, and pass the gate."""
    items = [_item("Рынок Дубая", "Описание без конкретных цифр.")]
    verdict = factcheck.check(
        _payload(
            "Доходность составит 12,7% годовых, цена 1 850 000 дирхам.",
            facts=[{"value": "12,7%", "source_url": "https://made-up.example/x"}],
        ),
        items, max_chars=900,
    )
    assert verdict["passed"] is False
    assert any("без подтверждения" in e for e in verdict["errors"])
    assert any("вне входных данных" in w for w in verdict["warnings"])


def test_declared_fact_with_a_real_input_source_is_accepted():
    items = [_item("Рынок Дубая", "Описание без конкретных цифр.")]
    verdict = factcheck.check(
        _payload(
            "Доходность составит 12,7% годовых.",
            facts=[{"value": "12,7", "source_url": "https://example.com/a"}],
        ),
        items, max_chars=900,
    )
    assert verdict["passed"] is True


def test_numbers_are_not_fused_across_a_sentence_boundary():
    """Was: "Дубай, 2026. 15 проектов" produced the phantom number 202615,
    which could never match the input, so the post was always rejected."""
    assert factcheck.extract_numbers("Дубай, 2026. 15 проектов") == ["2026", "15"]
    assert "202615" not in factcheck.extract_numbers("Дубай, 2026. 15 проектов")


def test_thousand_groups_and_decimals_still_parse():
    assert factcheck.extract_numbers("1 850 000 дирхам, ставка 12,7%") == [
        "1850000", "127",
    ]


def test_the_current_year_is_not_treated_as_an_unsourced_claim():
    year = datetime.now(timezone.utc).year
    items = [_item("Новость про Дубай", "Обычное описание без чисел.")]
    verdict = factcheck.check(
        _payload(f"В {year} году рынок продолжает расти."), items, max_chars=900
    )
    assert verdict["passed"] is True


def test_an_invented_number_is_still_caught():
    items = [_item("Новость про Дубай", "Обычное описание без чисел.")]
    verdict = factcheck.check(
        _payload("Цена квартиры — 1 850 000 дирхам."), items, max_chars=900
    )
    assert verdict["passed"] is False


# --------------------------------------------------------------------------
# 4. Selling share (brief §4: max ~25% of the feed)
# --------------------------------------------------------------------------

def _scored(category: str, item_id: str, value: float) -> NormalizedItem:
    item = _item("Заголовок материала", "Достаточно длинное описание материала.")
    item.item_id = item_id
    item.category = category
    item.score = value
    return item


def test_plan_never_fills_a_run_with_only_selling_rubrics():
    """Was: rubrics were ranked by score alone, the selling ones always won
    (they feed on the highest-weight categories), and a run came out 100%
    selling against the brief's 25% ceiling."""
    items = [
        _scored("developers", "a", 9.0),
        _scored("realty_news", "b", 8.5),
        _scored("official_data", "c", 8.0),
        _scored("lifestyle", "d", 7.0),
        _scored("city_gov", "e", 6.0),
        _scored("events", "f", 5.0),
    ]
    plan = score.plan_rubrics(items, max_posts=2, history=[])
    assert plan, "план не должен быть пустым"
    assert not (set(plan) & score.SELLING_RUBRICS), (
        f"на пустой истории продающие рубрики недопустимы: {plan}"
    )


def test_selling_rubric_is_allowed_once_the_feed_has_room():
    history = ["market_pulse"] * 20
    items = [_scored("developers", "a", 9.0), _scored("lifestyle", "b", 1.0)]
    plan = score.plan_rubrics(items, max_posts=1, history=history)
    assert set(plan) & score.SELLING_RUBRICS


def test_selling_share_stays_under_the_cap_over_a_long_horizon():
    items = [
        _scored("developers", "a", 9.0),
        _scored("realty_news", "b", 8.5),
        _scored("official_data", "c", 8.0),
        _scored("lifestyle", "d", 7.0),
        _scored("city_gov", "e", 6.0),
        _scored("events", "f", 5.0),
    ]
    history: list[str] = []
    for _ in range(30):
        history.extend(score.plan_rubrics(items, max_posts=2, history=history))
    selling = sum(1 for r in history if r in score.SELLING_RUBRICS)
    assert selling / len(history) <= score.SELLING_SHARE_CAP + 1e-9, (
        f"продающих {selling}/{len(history)}"
    )


def test_recent_rubrics_counts_published_and_still_queued_posts():
    state.save("published.json", {"version": 1, "posts": [{"rubric": "new_launch"}], "keys": []})
    state.save("queue.json", {"version": 1, "posts": [
        {"rubric": "market_pulse", "status": "queued"},
        {"rubric": "case_story", "status": "published"},  # already in published.json
    ]})
    assert score.recent_rubrics() == ["new_launch", "market_pulse"]


# --------------------------------------------------------------------------
# 5. Secret redaction in logs
# --------------------------------------------------------------------------

# Built at runtime on purpose: a literal of this shape in a tracked file
# is exactly what scripts/check_no_secrets.py is supposed to reject.
FAKE_TOKEN = "1234567890" + ":" + "AA" + "b7Qx" * 8 + "zyx"
FAKE_ANTHROPIC_KEY = "sk" + "-ant-" + "api03-" + "AbCdEfGh" * 3


def test_token_inside_an_exception_argument_is_redacted(caplog):
    """Was: pipeline/telegram.py logs the requests exception on every network
    error, and that exception carries the full API URL — including the bot
    token — into the GitHub Actions log."""
    filt = RedactingFilter([FAKE_TOKEN])
    record = logging.LogRecord(
        "pipeline.telegram", logging.WARNING, __file__, 1,
        "telegram.%s сетевая ошибка (%s)", ("sendMessage",
        Exception(f"url: https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")), None,
    )
    filt.filter(record)
    assert FAKE_TOKEN not in record.getMessage()
    assert "***" in record.getMessage()


def test_unregistered_credentials_are_redacted_by_shape():
    filt = RedactingFilter([])
    record = logging.LogRecord(
        "pipeline.llm", logging.WARNING, __file__, 1,
        "ключ %s отклонён", (FAKE_ANTHROPIC_KEY,), None,
    )
    filt.filter(record)
    assert FAKE_ANTHROPIC_KEY not in record.getMessage()
    assert "***" in record.getMessage()


def test_ordinary_messages_are_untouched():
    filt = RedactingFilter([FAKE_TOKEN])
    record = logging.LogRecord(
        "pipeline.run", logging.INFO, __file__, 1, "Собрано %d материалов", (6,), None,
    )
    filt.filter(record)
    assert record.getMessage() == "Собрано 6 материалов"


# --------------------------------------------------------------------------
# 6. Card rendering: long Russian words used to run off the edge
# --------------------------------------------------------------------------

def test_long_russian_word_is_hyphenated_inside_the_card():
    """Was: a word wider than the text box was emitted as one line and ran
    off the right edge of the generated PNG."""
    from PIL import Image, ImageDraw

    from pipeline.illustrate import CARD_SIZE, _load_font, _wrap

    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    box_width = CARD_SIZE[0] - 72 * 2
    for headline in (
        "Экспериментально-технологический комплекс",
        "Достопримечательности Джумейры",
        "Частнопредпринимательский",
    ):
        for size in (74, 66, 58, 50, 44, 38):
            font = _load_font(size, bold=True)
            lines = _wrap(draw, headline, font, box_width)
            widest = max(draw.textlength(line, font=font) for line in lines)
            if len(lines) <= 4 and widest <= box_width:
                break
        assert widest <= box_width, f"{headline!r} вылезает за карточку"


# --------------------------------------------------------------------------
# 7. LLM retry policy
# --------------------------------------------------------------------------

def test_client_errors_are_not_retried():
    """Was: RETRY_STATUSES was declared but never used, so a bad API key was
    retried four times with sleeps before failing anyway."""
    from pipeline.llm import is_retryable

    class Err(Exception):
        def __init__(self, status):
            super().__init__("boom")
            self.status_code = status

    assert is_retryable(Err(429)) is True
    assert is_retryable(Err(529)) is True
    assert is_retryable(Err(503)) is True
    assert is_retryable(Err(401)) is False
    assert is_retryable(Err(400)) is False
    assert is_retryable(Err(404)) is False


# --------------------------------------------------------------------------
# 8. Approval callbacks
# --------------------------------------------------------------------------

def _callback_update(update_id: int, action: str, post_id: str, from_id: str = "42") -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": int(from_id)},
            "data": f"{action}:{post_id}",
            "message": {"message_id": 500 + update_id},
        },
    }


def _queue(status: str = "queued") -> dict:
    return {"version": 1, "posts": [{
        "post_id": "market_pulse-abc123", "rubric": "market_pulse",
        "title": "Заголовок", "body": "Текст", "status": status,
        "slot_at": "2026-09-21T05:30:00+00:00", "approval": {},
        "source_items": [],
    }]}


class _FakeClient:
    """Records calls and replays a fixed list of updates."""

    def __init__(self, updates):
        self._updates = list(updates)
        self.calls: list[tuple] = []

    def get_updates(self, offset, timeout=25):
        out = [u for u in self._updates if u["update_id"] >= offset]
        return out

    def answer_callback(self, callback_id, text=""):
        self.calls.append(("answer", callback_id, text))
        return {"ok": True}

    def edit_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.calls.append(("edit", message_id))
        return {"ok": True}


def test_callbacks_are_ignored_when_no_owner_is_configured(settings):
    """Was: an empty TELEGRAM_OWNER_ID skipped the identity check entirely,
    so anyone who found the bot could publish to the channel."""
    from pipeline import approve

    settings.telegram_owner_id = ""
    client = _FakeClient([_callback_update(1, "ok", "market_pulse-abc123", from_id="999")])
    queue = _queue()
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions == []
    assert queue["posts"][0]["status"] == "queued"


def test_a_stranger_cannot_approve(settings):
    from pipeline import approve

    client = _FakeClient([_callback_update(1, "ok", "market_pulse-abc123", from_id="999")])
    queue = _queue()
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions == []
    assert queue["posts"][0]["status"] == "queued"
    assert any(call[0] == "answer" and "прав" in call[2] for call in client.calls)


def test_button_on_an_already_published_post_changes_nothing(settings):
    """Was: «Отложить» on a published post moved slot_at, which changed the
    idempotency key and let the post go out to the channel a second time."""
    from pipeline import approve

    queue = _queue(status="published")
    original_slot = queue["posts"][0]["slot_at"]
    client = _FakeClient([_callback_update(1, "later", "market_pulse-abc123")])
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions == []
    assert queue["posts"][0]["status"] == "published"
    assert queue["posts"][0]["slot_at"] == original_slot


def test_repeated_approve_callback_is_idempotent(settings):
    from pipeline import approve

    queue = _queue()
    client = _FakeClient([_callback_update(1, "ok", "market_pulse-abc123")])
    approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert queue["posts"][0]["status"] == "approved"
    queue["posts"][0]["status"] = "published"
    client2 = _FakeClient([_callback_update(2, "ok", "market_pulse-abc123")])
    assert approve.poll_once(settings, client=client2, queue=queue, persist=False) == []
    assert queue["posts"][0]["status"] == "published"


def test_offset_advances_even_if_one_update_is_malformed(settings):
    """Was: an exception inside the loop aborted poll_once before the offset
    was saved, so the whole batch replayed on the next run."""
    from pipeline import approve

    bad = {"update_id": 7, "callback_query": None, "message_reaction_count": {"nope": True}}
    good = _callback_update(8, "ok", "market_pulse-abc123")
    client = _FakeClient([bad, good])
    queue = _queue()
    approve.poll_once(settings, client=client, queue=queue, persist=True)
    assert state.load("telegram_offset.json")["offset"] == 9


def test_rewrite_moves_the_post_into_the_regeneration_queue(settings):
    """Was: «Переписать» set the post to "draft", nothing ever picked it up
    again, and docs/SETUP.md promised it would be regenerated."""
    from pipeline import approve

    queue = _queue()
    client = _FakeClient([_callback_update(1, "redo", "market_pulse-abc123")])
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions and decisions[0]["status"] == "rewrite"
    assert queue["posts"][0]["status"] == "rewrite"


# --------------------------------------------------------------------------
# 9. «Переписать» really regenerates
# --------------------------------------------------------------------------

def test_regenerate_rewrites_produces_a_fresh_draft(settings):
    from pipeline import postqueue, run as run_module
    from pipeline.generate import snapshot_items

    items = [_item("Новый проект в Дубае", "Материал про рынок недвижимости Дубая.")]
    queue = {"version": 1, "posts": [{
        "post_id": "market_pulse-abc123", "rubric": "market_pulse",
        "title": "Старый заголовок", "body": "Старый текст", "status": "rewrite",
        "slot_at": "2026-09-21T05:30:00+00:00", "approval": {"action": "redo"},
        "source_items": snapshot_items(items), "rewrite_count": 0,
    }]}
    postqueue.save_queue(queue)

    drafts = run_module.regenerate_rewrites(settings)
    assert len(drafts) == 1
    stored = postqueue.load_queue()["posts"][0]
    assert stored["status"] == "queued", "переписанный пост снова уходит на апрув"
    assert not (stored.get("approval") or {}).get("sent_at"), "превью должно уйти заново"
    assert stored["rewrite_count"] == 1


def test_rewrite_gives_up_after_the_limit(settings):
    from pipeline import postqueue, run as run_module

    queue = {"version": 1, "posts": [{
        "post_id": "market_pulse-abc123", "rubric": "market_pulse",
        "title": "t", "body": "b", "status": "rewrite", "slot_at": None,
        "source_items": [], "rewrite_count": run_module.MAX_REWRITES,
    }]}
    postqueue.save_queue(queue)
    assert run_module.regenerate_rewrites(settings) == []
    assert postqueue.load_queue()["posts"][0]["status"] == "rejected"


# --------------------------------------------------------------------------
# 10. Partial publication must not be retried from the start
# --------------------------------------------------------------------------

def test_partial_send_keeps_the_key_so_the_photo_is_not_resent(settings):
    """Was: sendPhoto succeeded, the follow-up sendMessage failed, the key was
    released, and the next run posted the photo to the channel a second time."""
    from pipeline import publish
    from pipeline.telegram import TelegramClient

    class HalfBrokenClient(TelegramClient):
        def send_message(self, *a, **kw):
            raise RuntimeError("Bad Gateway")

    client = HalfBrokenClient("x", dry_run=True)
    published = {"version": 1, "posts": [], "keys": []}
    post = {
        "post_id": "market_pulse-partial", "rubric": "market_pulse",
        "title": "Заголовок", "body": "т" * 3000, "hashtags": [], "cta": "",
        "sources": [], "status": "approved", "slot_at": None,
        "image_path": "/tmp/card.png", "length_chars": 3000,
    }
    result = publish.publish_one(settings, post, client, published, persist=False)
    assert result["published"] is False
    assert result["partial"] is True
    key = publish.publish_key(post)
    assert publish.already_published(published, key), (
        "ключ должен остаться занятым, иначе фото уйдёт повторно"
    )

    second = publish.publish_one(settings, post, client, published, persist=False)
    assert second.get("skipped") is True


def test_total_failure_still_releases_the_key(settings):
    from pipeline import publish
    from pipeline.telegram import TelegramClient

    class DeadClient(TelegramClient):
        def send_photo(self, *a, **kw):
            raise RuntimeError("network down")

        def send_message(self, *a, **kw):
            raise RuntimeError("network down")

    client = DeadClient("x", dry_run=True)
    published = {"version": 1, "posts": [], "keys": []}
    post = {
        "post_id": "market_pulse-dead", "rubric": "market_pulse",
        "title": "З", "body": "короткий текст", "hashtags": [], "cta": "",
        "sources": [], "status": "approved", "slot_at": None,
        "image_path": "/tmp/card.png", "length_chars": 14,
    }
    result = publish.publish_one(settings, post, client, published, persist=False)
    assert result["published"] is False and not result.get("partial")
    assert not publish.already_published(published, publish.publish_key(post))


def _message_update(update_id, text, from_id="777", chat_type="private"):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "text": text,
        "from": {"id": int(from_id)}, "chat": {"id": int(from_id), "type": chat_type},
    }}


class _ChattyClient(_FakeClient):
    def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", str(chat_id), text))
        return {"ok": True}


def test_start_in_private_chat_replies_with_the_senders_id(settings):
    """Was: TELEGRAM_OWNER_ID held the bot's own id and previews failed with
    «the bot can't send messages to the bot» — the owner had no way to learn
    the right number from inside the bot."""
    from pipeline import approve

    client = _ChattyClient([_message_update(1, "/start", from_id="777")])
    approve.poll_once(settings, client=client, queue=_queue(), persist=False)
    sent = [c for c in client.calls if c[0] == "send"]
    assert sent and sent[0][1] == "777" and "777" in sent[0][2] and "TELEGRAM_OWNER_ID" in sent[0][2]


def test_owner_gets_a_confirmation_and_groups_are_ignored(settings):
    from pipeline import approve

    client = _ChattyClient([
        _message_update(1, "/id", from_id=settings.telegram_owner_id),
        _message_update(2, "/id", from_id="555", chat_type="supergroup"),
        _message_update(3, "спасибо", from_id=settings.telegram_owner_id),
    ])
    approve.poll_once(settings, client=client, queue=_queue(), persist=False)
    sent = [c for c in client.calls if c[0] == "send"]
    assert len(sent) == 2 and "владелец" in sent[0][2]
    assert "Сохранила" in sent[1][2], "обычный текст владельца — заметка в копилку"


def test_a_stranger_typing_plain_id_without_slash_still_gets_the_answer(settings):
    """Was: the owner typed «id» (no slash) and the bot stayed silent."""
    from pipeline import approve

    client = _ChattyClient([_message_update(1, "id", from_id="252033658")])
    approve.poll_once(settings, client=client, queue=_queue(), persist=False)
    sent = [c for c in client.calls if c[0] == "send"]
    assert sent and "252033658" in sent[0][2]


def test_truncated_model_answer_is_reported_not_parsed(monkeypatch):
    from pipeline import llm

    class _Block:
        type, text = "text", '{"title": "x", "body": "cut off'

    class _Resp:
        content, stop_reason = [_Block()], "max_tokens"

    class _Messages:
        def create(self, **kw):
            assert kw["output_config"] == {"effort": "medium"}
            return _Resp()

    class _Client:
        messages = _Messages()

    provider = llm.AnthropicProvider.__new__(llm.AnthropicProvider)
    provider.model, provider._client = "claude-opus-5", _Client()
    with pytest.raises(llm.LLMError, match="max_tokens"):
        provider.complete("s", "u")


def test_missing_card_is_redrawn_before_preview(settings, tmp_path, monkeypatch):
    """Was: the card lived in assets/generated/ of the generate run only; the
    approve run (a fresh checkout) failed with FileNotFoundError and the owner
    never saw the preview."""
    from pipeline import approve, illustrate

    monkeypatch.setattr(illustrate, "GENERATED_DIR", tmp_path, raising=False)
    queue = _queue()
    post = queue["posts"][0]
    post["image_path"] = str(tmp_path / "gone" / "card-old.png")
    post["image_meta"] = {"provider": "own_card", "headline": "Заголовок карточки", "accent": "12%"}

    class _Client(_FakeClient):
        def send_photo(self, chat_id, photo_path=None, *, file_id=None, caption="", reply_markup=None):
            assert file_id is None and photo_path and Path(photo_path).exists()
            self.calls.append(("photo", photo_path))
            return {"ok": True, "result": {"message_id": 5, "photo": [{"file_id": "small"}, {"file_id": "BIG"}]}}

    client = _Client([])
    assert approve.send_previews(settings, client=client, queue=queue, persist=False) == 1
    assert post["telegram_file_id"] == "BIG"
    assert Path(post["image_path"]).exists()


def test_publish_reuses_the_uploaded_file_id(settings, monkeypatch):
    from pipeline import publish

    queue = _queue(status="approved")
    post = queue["posts"][0]
    post["telegram_file_id"] = "BIG"
    post["image_path"] = "/nonexistent/card.png"
    post["slot_at"] = "2020-01-01T00:00:00+00:00"

    class _Client(_FakeClient):
        def send_photo(self, chat_id, photo_path=None, *, file_id=None, caption="", reply_markup=None):
            self.calls.append(("photo", photo_path, file_id))
            return {"ok": True, "result": {"message_id": 9}}

        def send_message(self, chat_id, text, **kw):
            self.calls.append(("text", text))
            return {"ok": True, "result": {"message_id": 10}}

    client = _Client([])
    settings.dry_run = False
    publish.publish_one(settings, post, client, published={"keys": [], "posts": []}, persist=False)
    photo_calls = [c for c in client.calls if c[0] == "photo"]
    assert photo_calls and photo_calls[0][2] == "BIG" and photo_calls[0][1] is None


def test_owner_id_equal_to_the_bots_own_id_is_called_out(settings, caplog):
    from pipeline import approve

    class _Me(_FakeClient):
        def call(self, method, payload=None, **kw):
            assert method == "getMe"
            return {"ok": True, "result": {"id": 42, "username": "mary_news_bot"}}

    settings.telegram_owner_id = "42"
    with caplog.at_level(logging.INFO):
        approve._log_bot_identity(_Me([]), settings)
    assert "@mary_news_bot" in caplog.text and "равен id самого бота" in caplog.text


def test_owner_forwards_are_kept_in_the_inbox(settings, isolated_state):
    from pipeline import approve, state

    fwd = {"update_id": 7, "message": {
        "message_id": 70, "text": "Мой старый пост про рассрочку 60/40 и почему я её не люблю.",
        "from": {"id": int(settings.telegram_owner_id)},
        "chat": {"id": int(settings.telegram_owner_id), "type": "private"},
        "forward_origin": {"type": "channel", "chat": {"username": "nudeassphilosophy", "title": "NAP"},
                           "message_id": 415, "date": 1},
    }}
    note = _message_update(8, "заметка себе: разобрать планировку 2BR", from_id=settings.telegram_owner_id)
    stranger = {**_message_update(9, "привет", from_id="999")}
    client = _ChattyClient([fwd, note, stranger])
    approve.poll_once(settings, client=client, queue=_queue(), persist=True)
    items = state.load("inbox.json")["items"]
    assert [i["kind"] for i in items] == ["forward", "note"]
    assert items[0]["origin"]["chat_username"] == "nudeassphilosophy"
    sent = [c for c in client.calls if c[0] == "send"]
    assert "копилке: 1" in sent[0][2] and "копилке: 2" in sent[1][2]
    assert "999" in sent[2][2], "посторонний по-прежнему получает свой id"


def test_model_may_skip_a_rubric_instead_of_stretching(settings, monkeypatch):
    """Was: an Abu Dhabi wedding-cancellation story became a «rules_and_laws» post
    about property deals."""
    from pipeline import generate
    from pipeline.normalize import NormalizedItem

    class _Provider:
        name = "fake"

        def complete(self, system, user, **kw):
            return '{"skip": true, "skip_reason": "во входных данных нет материала по рубрике"}'

    item = NormalizedItem(item_id="i", source_id="s", category="city_gov", title="Суд взыскал расходы на свадьбу",
                          summary="История про отменённую свадьбу", url="https://example.com/a",
                          canonical_url="https://example.com/a", published_at=None,
                          collected_at="2026-09-22T00:00:00Z", lang="ru")
    assert generate.generate_for_rubric(settings, "rules_and_laws", [item], provider=_Provider()) is None


def test_phantom_offers_in_cta_are_rejected():
    from pipeline import factcheck
    from pipeline.normalize import NormalizedItem

    item = NormalizedItem(item_id="i", source_id="s", category="lifestyle", title="Dubai Marina",
                          summary="район", url="https://example.com/m", canonical_url="https://example.com/m",
                          published_at=None, collected_at="2026-09-22T00:00:00Z", lang="ru")
    base = {"title": "Dubai Marina", "body": "Район у воды. Подходит тем, кто живёт без машины.",
            "hashtags": [], "image_prompt": "", "sources": [{"source_id": "s", "url": "https://example.com/m"}],
            "self_check": {}}
    ok = factcheck.check({**base, "cta": "Напишите мне — расскажу подробнее"}, [item], max_chars=600)
    assert ok["passed"]
    bad = factcheck.check({**base, "cta": "PDF-гид по району — заберите в боте"}, [item], max_chars=600)
    assert not bad["passed"] and any("несуществующего" in e for e in bad["errors"])


# --------------------------------------------------------------------------
# Editing a post by replying to its preview
# --------------------------------------------------------------------------

def _reply_update(update_id: int, text: str, *, reply_to: int = 22, from_id: str = "42"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 900 + update_id,
            "chat": {"id": int(from_id), "type": "private"},
            "from": {"id": int(from_id)},
            "text": text,
            "reply_to_message": {"message_id": reply_to},
        },
    }


class _ChatClient(_FakeClient):
    def send_message(self, chat_id, text, **kw):
        self.calls.append(("text", text))
        return {"ok": True, "result": {"message_id": 77}}

    def send_photo(self, chat_id, photo_path=None, *, file_id=None, caption="", reply_markup=None):
        self.calls.append(("photo", caption))
        return {"ok": True, "result": {"message_id": 78}}


def test_reply_to_preview_replaces_the_text_and_previews_again(settings, monkeypatch):
    """Was: «Переписать» only asked the model for another version; the owner
    had no way to hand over her own wording."""
    from pipeline import approve, state

    queue = _queue()
    post = queue["posts"][0]
    post["approval"] = {"sent_at": "2026-09-23T09:00:00+00:00", "preview_message_id": 22}
    post["cta"] = "Напишите мне."
    post["hashtags"] = ["#дубай"]
    client = _ChatClient([_reply_update(1, "Мой текст поста.\n\n#дубай #новости")])
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions and decisions[0]["action"] == "edit"
    assert post["body"] == "Мой текст поста.\n\n#дубай #новости"
    assert post["cta"] == "" and post["hashtags"] == []
    assert post["status"] == "queued" and not post["approval"].get("sent_at")
    assert ("edit", 22) in client.calls  # old buttons gone
    assert any(c[0] == "text" and "Заменила" in c[1] for c in client.calls)
    # The inbox did not swallow the reply as material.
    assert not any(c[0] == "text" and "Сохранила" in c[1] for c in client.calls)
    # A fresh preview goes out and carries the new text.
    approve.send_previews(settings, client=client, queue=queue, persist=False)
    assert any(c[0] == "text" and "Мой текст поста." in c[1] for c in client.calls)


def test_reply_with_rewrite_wish_sends_the_post_back_to_the_model(settings, monkeypatch):
    from pipeline import approve, state

    queue = _queue()
    post = queue["posts"][0]
    post["approval"] = {"sent_at": "2026-09-23T09:00:00+00:00", "preview_message_id": 22}
    client = _ChatClient([_reply_update(1, "Переписать: короче и без цифр")])
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions[0]["action"] == "redo"
    assert post["status"] == "rewrite"
    assert post["approval"]["instruction"] == "короче и без цифр"
    assert post["body"] == "Текст"  # untouched: the model rewrites it


def test_reply_from_a_stranger_or_to_an_unknown_message_is_not_an_edit(settings, monkeypatch):
    from pipeline import approve, state

    queue = _queue()
    post = queue["posts"][0]
    post["approval"] = {"sent_at": "x", "preview_message_id": 22}
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    monkeypatch.setattr(state, "load", lambda name: {"items": []})
    client = _ChatClient([_reply_update(1, "Чужой текст", from_id="999"),
                          _reply_update(2, "Ответ не на превью", reply_to=5)])
    approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert post["body"] == "Текст"


def test_rewrite_wish_reaches_the_model_prompt(settings, monkeypatch, tmp_path):
    from pipeline import run as run_stage, postqueue

    queue = _queue(status="rewrite")
    post = queue["posts"][0]
    post["approval"] = {"instruction": "короче и без цифр"}
    post["source_items"] = [{
        "item_id": "i1", "source_id": "s", "category": "realty_news", "title": "Новость",
        "summary": "Текст новости", "url": "https://example.com/n", "canonical_url": "https://example.com/n",
        "published_at": None, "collected_at": "2026-09-23T00:00:00+00:00", "lang": "ru",
    }]
    monkeypatch.setattr(postqueue, "load_queue", lambda: queue)
    monkeypatch.setattr(postqueue, "save_queue", lambda q: None)
    monkeypatch.setattr(postqueue, "enqueue", lambda drafts, persist=True: None)
    seen = {}

    def fake_generate(settings, rubric, items, provider=None, *, extra_instruction=""):
        seen["instruction"] = extra_instruction
        return None

    monkeypatch.setattr("pipeline.generate.generate_for_rubric", fake_generate)
    run_stage.regenerate_rewrites(settings)
    assert "короче и без цифр" in seen["instruction"]


def test_status_command_reports_the_queue_in_dubai_time(settings, monkeypatch):
    """The owner believed approved posts were not going out; «статус» shows the
    queue with Dubai times instead of leaving her guessing."""
    from pipeline import approve, state

    queue = _queue(status="approved")
    queue["posts"][0]["slot_at"] = "2026-09-23T15:30:00+00:00"
    queue["posts"].append({"post_id": "meme-1", "rubric": "meme", "title": "Мем про DEWA",
                           "body": "x", "status": "queued", "approval": {}, "source_items": []})
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    update = {"update_id": 5, "message": {"message_id": 901, "chat": {"id": 42, "type": "private"},
                                          "from": {"id": 42}, "text": "Статус"}}
    client = _ChatClient([update])
    approve.poll_once(settings, client=client, queue=queue, persist=False)
    texts = [c[1] for c in client.calls if c[0] == "text"]
    assert texts and "23.09 в 19:30" in texts[0] and "Заголовок" in texts[0]
    assert "ждёт вашего решения" in texts[0] and "Мем про DEWA" in texts[0]
    assert not any("Сохранила" in t for t in texts)  # not stored as inbox material


def test_approval_confirmation_names_the_dubai_slot(settings, monkeypatch):
    from pipeline import approve, state

    queue = _queue()
    queue["posts"][0]["slot_at"] = "2026-09-24T05:30:00+00:00"
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    client = _ChatClient([_callback_update(1, "ok", "market_pulse-abc123", from_id="42")])
    approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert queue["posts"][0]["status"] == "approved"
    texts = [c[1] for c in client.calls if c[0] == "text"]
    assert any("24.09 в 09:30" in t and "одобрен" in t for t in texts)


def test_rewrite_after_approval_is_explained_and_preview_says_to_approve_again(settings, monkeypatch):
    from pipeline import approve, state

    queue = _queue(status="approved")
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    client = _ChatClient([_callback_update(1, "redo", "market_pulse-abc123", from_id="42")])
    approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert queue["posts"][0]["status"] == "rewrite"
    texts = [c[1] for c in client.calls if c[0] == "text"]
    assert any("отменяет публикацию" in t for t in texts)
    queue["posts"][0]["rewrite_count"] = 1
    assert "нажмите «Опубликовать» здесь" in approve.preview_text(queue["posts"][0])


def test_dubai_time_formatting():
    from pipeline import approve

    assert approve.dubai_time("2026-09-23T15:30:00+00:00") == "23.09 в 19:30"
    assert approve.dubai_time(None) == "ближайший свободный слот"


def test_reply_with_dopolni_merges_the_material_into_the_post(settings, monkeypatch):
    from pipeline import approve, state

    queue = _queue()
    post = queue["posts"][0]
    post["approval"] = {"sent_at": "x", "preview_message_id": 22}
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    client = _ChatClient([_reply_update(1, "дополни: проверьте разрешение DTCM и условия отмены")])
    decisions = approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert decisions[0]["action"] == "merge" and post["status"] == "rewrite"
    assert post["approval"]["merge"] is True
    assert post["source_items"][-1]["source_id"] == "owner_inbox"
    assert "DTCM" in post["source_items"][-1]["summary"]
    assert post["body"] == "Текст"  # the model rewrites it, the reply is not the new text


def test_long_preview_is_not_cut_by_the_caption_limit(settings, tmp_path, monkeypatch):
    """Was: a 1500-character preview was truncated to 1024 in the photo caption
    and the owner read the post as unfinished («Пост не дописан»)."""
    from pipeline import approve, illustrate

    monkeypatch.setattr(illustrate, "GENERATED_DIR", tmp_path, raising=False)
    queue = _queue()
    post = queue["posts"][0]
    post["body"] = "Абзац про рынок. " * 70  # ~1200 chars
    post["image_meta"] = {"provider": "own_card", "headline": "Заголовок", "accent": ""}
    client = _ChatClient([])
    assert approve.send_previews(settings, client=client, queue=queue, persist=False) == 1
    kinds = [c[0] for c in client.calls]
    assert kinds == ["photo", "text"]
    caption, text = client.calls[0][1], client.calls[1][1]
    assert len(caption) <= 1024 and "Полный текст" in caption
    assert post["body"].strip() in text and "Поправить" in text
    assert post["approval"]["preview_message_id"] == 77 and post["approval"]["preview_photo_message_id"] == 78
    # a reply to either message edits this post
    assert approve._post_for_reply(queue, {"reply_to_message": {"message_id": 78}}) is post
    assert approve._post_for_reply(queue, {"reply_to_message": {"message_id": 77}}) is post


def test_approval_moves_the_post_to_the_earliest_free_slot(settings, monkeypatch):
    """Was: approved on the 23rd, slot on the 25th, tonight's slot empty."""
    from pipeline import approve, state

    queue = _queue()
    post = queue["posts"][0]
    post["slot_at"] = "2030-01-10T15:30:00+00:00"
    other = {**post, "post_id": "x-2", "status": "approved", "slot_at": None, "approval": {}}
    queue["posts"].append(other)
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    client = _ChatClient([_callback_update(1, "ok", "market_pulse-abc123", from_id="42")])
    approve.poll_once(settings, client=client, queue=queue, persist=False)
    assert post["status"] == "approved"
    assert post["slot_at"] < "2030-01-10", post["slot_at"]
    # the next approval must not land on the same slot
    other["slot_at"] = post["slot_at"]
    from pipeline import postqueue
    assert postqueue.earliest_free_slot(queue, exclude_post_id="none") > post["slot_at"]
