"""INBOX: material about a post already in the queue is merged into it."""

from __future__ import annotations

from pipeline import inbox, run as run_stage

DUBIZZLE = {
    "post_id": "dubai_life-1", "rubric": "dubai_life", "status": "queued",
    "title": "Dubizzle теперь бронирует посуточное жильё напрямую",
    "body": "На Dubizzle появилось прямое бронирование посуточного жилья: квартиру или виллу можно "
            "забронировать прямо на площадке. Раньше это была доска объявлений. Краткосрочная аренда "
            "уходит в приложения, владельцы будут считать комиссию площадок.",
    "source_items": [{"item_id": "i1", "source_id": "tg_dubaimap", "url": "https://t.me/dubaimap/1"}],
    "item_ids": ["i1"], "approval": {},
}
DIPLOMA = {
    "post_id": "dubai_life-2", "rubric": "dubai_life", "status": "queued",
    "title": "За поддельный диплом в ОАЭ — до 3 лет",
    "body": "Проверка дипломов ужесточается: штраф и срок. Если документ поддельный — уголовное дело.",
    "source_items": [], "item_ids": [], "approval": {},
}
NOTE = ("📍 Если хозяин предлагает оплатить «напрямую, без комиссии» — не соглашайтесь. Как только "
        "бронь уходит с платформы, вы теряете защиту.\n📍 Уточните разрешение DTCM на краткосрочную "
        "аренду.\n📍 Проверьте условия отмены бронирования.")


def test_related_post_finds_the_post_the_note_is_about():
    queue = {"posts": [DIPLOMA, DUBIZZLE]}
    assert inbox.related_post(queue, NOTE)["post_id"] == "dubai_life-1"
    assert inbox.related_post(queue, "Погода в Дубае на выходных: жара спадает") is None
    published = {**DUBIZZLE, "status": "published"}
    assert inbox.related_post({"posts": [published]}, NOTE) is None


def test_merge_attaches_the_material_and_sends_the_post_back_to_the_model():
    post = {**DUBIZZLE, "source_items": list(DUBIZZLE["source_items"]), "item_ids": ["i1"]}
    entry = {"message_id": 36, "kind": "note", "text": NOTE, "received_at": "2026-09-23T11:13:00+00:00", "origin": {}}
    inbox.merge_into_post(post, inbox.as_item(entry), note=NOTE)
    assert post["status"] == "rewrite" and post["approval"]["merge"] is True
    assert [i["source_id"] for i in post["source_items"]] == ["tg_dubaimap", "owner_inbox"]
    assert "owner-36" in post["item_ids"]
    assert "DTCM" in post["approval"]["instruction"]
    inbox.merge_into_post(post, inbox.as_item(entry), note=NOTE)  # idempotent
    assert len(post["source_items"]) == 2


def test_regenerate_uses_the_merge_instruction(settings, monkeypatch):
    from pipeline import postqueue

    post = {**DUBIZZLE, "status": "rewrite", "approval": {"merge": True, "instruction": "вплести четыре совета"},
            "source_items": [{"item_id": "i1", "source_id": "s", "category": "realty_news", "title": "Новость",
                              "summary": "Текст", "url": "https://example.com/n", "canonical_url": "https://example.com/n",
                              "published_at": None, "collected_at": "2026-09-23T00:00:00+00:00", "lang": "ru"}]}
    queue = {"version": 1, "posts": [post]}
    monkeypatch.setattr(postqueue, "load_queue", lambda: queue)
    monkeypatch.setattr(postqueue, "save_queue", lambda q: None)
    monkeypatch.setattr(postqueue, "enqueue", lambda drafts, persist=True: None)
    seen = {}

    def fake_generate(settings, rubric, items, provider=None, *, extra_instruction=""):
        seen["instruction"] = extra_instruction
        return None

    monkeypatch.setattr("pipeline.generate.generate_for_rubric", fake_generate)
    run_stage.regenerate_rewrites(settings)
    assert "ОБЪЕДИНЕНИЕ" in seen["instruction"] and "вплести четыре совета" in seen["instruction"]
