"""CLI entry point: ``python -m pipeline.run``.

Stages can be run one by one (what the GitHub Actions workflows do) or all at
once (``--dry-run``, what a human does locally to see the whole contour work).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from . import analytics as analytics_stage
from . import approve as approve_stage
from . import collect as collect_stage
from . import illustrate as illustrate_stage
from . import inbox as inbox_stage
from . import normalize as normalize_stage
from . import postqueue, publish as publish_stage, score as score_stage, state
from .config import OUT_DIR, Settings, load_settings, load_sources
from .generate import generate
from .logging_setup import setup_logging
from .models import PostDraft

log = logging.getLogger("pipeline.run")

STAGES = ("collect", "inbox", "generate", "approve", "publish", "analytics", "all")


def stage_collect(settings: Settings) -> list[Any]:
    sources_doc = load_sources()
    raw = collect_stage.collect(settings, sources_doc)
    items = normalize_stage.normalize_and_dedupe(raw, sources_doc, persist=not settings.dry_run)
    items = score_stage.score_items(items, sources_doc)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "items.json").write_text(
        json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Собрано и отскорено %d материалов → out/items.json", len(items))
    if not settings.dry_run:
        # Compact one-line snapshot of the real corpus: lets the clustering
        # threshold be tuned offline on what the channels actually publish.
        compact = [{"id": i.item_id, "s": i.source_id, "c": i.category, "d": i.published_at,
                    "t": i.title[:200], "x": (i.summary or "")[:300], "u": i.url}
                   for i in items]
        state.save("corpus_sample.json", {"version": 1, "items": compact}, compact_json=True)
    return items


MAX_REWRITES = 3

REWRITE_INSTRUCTION = (
    "ЭТО ПЕРЕПИСЫВАНИЕ. Владелец канала отклонил предыдущую версию поста "
    "кнопкой «Переписать». Напиши по тем же входным данным ДРУГОЙ текст: "
    "другой заход в первой строке, другая структура, другой угол подачи. "
    "Факты и цифры менять нельзя — только подачу."
)


def regenerate_rewrites(settings: Settings, provider: Any = None) -> list[PostDraft]:
    """Regenerate posts the owner sent back with «Переписать».

    Without this the button was a dead end: the post was moved out of the
    "queued" state, nothing ever picked it up again, and it sat in the queue
    forever while docs/SETUP.md promised it would be rewritten.
    """
    from .generate import generate_for_rubric
    from .models import NormalizedItem

    queue = postqueue.load_queue()
    pending = [p for p in (queue.get("posts") or []) if p.get("status") == "rewrite"]
    if not pending:
        return []

    drafts: list[PostDraft] = []
    for post in pending:
        post_id = post.get("post_id")
        rounds = int(post.get("rewrite_count") or 0) + 1
        if rounds > MAX_REWRITES:
            log.warning("Пост %s переписан %d раз — отклоняем", post_id, rounds - 1)
            post["status"] = "rejected"
            continue
        snapshot = post.get("source_items") or []
        if not snapshot:
            # Drafts created before source_items existed cannot be rebuilt.
            log.warning("У поста %s нет сохранённой фактуры — переписать нельзя", post_id)
            post["status"] = "rejected"
            continue
        items = [NormalizedItem.from_dict(row) for row in snapshot]
        draft = generate_for_rubric(
            settings, post.get("rubric", ""), items, provider=provider,
            extra_instruction=REWRITE_INSTRUCTION,
        )
        if not draft or draft.status == "failed":
            log.warning("Переписать пост %s не удалось, остаётся в очереди", post_id)
            continue
        # Keep the original identity: the queue entry, the idempotency key and
        # the analytics record must all still refer to the same post.
        draft.post_id = post_id
        draft.rewrite_count = rounds
        draft.slot_at = post.get("slot_at")
        image_path, image_meta = illustrate_stage.illustrate(settings, draft)
        draft.image_path = image_path
        draft.image_meta = image_meta
        # The regenerated draft keeps the same post_id (same rubric + items),
        # so enqueue() updates the existing entry and sets it back to
        # "queued" — which makes APPROVE send a fresh preview.
        post["status"] = "queued"
        post["approval"] = {}
        drafts.append(draft)
        log.info("Пост %s переписан (попытка %d)", post_id, rounds)

    postqueue.save_queue(queue)
    if drafts:
        postqueue.enqueue(drafts, persist=True)
    return drafts


def stage_inbox(settings: Settings, *, send_previews: bool = True) -> list[PostDraft]:
    """The owner's inbox → drafts. Returns immediately when nothing is new.

    Runs after every approve poll (so a forwarded screenshot becomes a preview
    within about half an hour) and at the start of stage_generate. With
    ``send_previews`` the fresh drafts go to the owner right away instead of
    waiting for the next poll.
    """
    drafts = inbox_stage.drafts_from_inbox(settings)
    if drafts:
        log.info("Черновиков из копилки владельца: %d", len(drafts))
        if send_previews:
            approve_stage.send_previews(settings)
    return drafts


def stage_generate(settings: Settings, items: list[Any] | None = None, max_posts: int = 2) -> list[PostDraft]:
    rewritten = regenerate_rewrites(settings)
    if rewritten:
        log.info("Переписано по кнопке «Переписать»: %d", len(rewritten))
    # What the owner sent comes before the planned rubrics; previews for it
    # are sent by the approve step that follows in the workflow.
    try:
        inbox_drafts = stage_inbox(settings, send_previews=False)
    except Exception as exc:
        log.error("Копилка владельца не обработана, плановые рубрики продолжаются: %s", exc)
        inbox_drafts = []
    items = items if items is not None else stage_collect(settings)
    plan = score_stage.plan_rubrics(items, max_posts=max_posts)
    log.info("План рубрик на этот прогон: %s", ", ".join(plan) or "пусто")
    drafts = generate(settings, items, plan)
    if drafts and not settings.dry_run:
        used_ids = {i for d in drafts for i in (d.item_ids or [])}
        normalize_stage.mark_used([i for i in items if i.item_id in used_ids])

    for draft in drafts:
        image_path, image_meta = illustrate_stage.illustrate(settings, draft)
        draft.image_path = image_path
        draft.image_meta = image_meta

    postqueue.enqueue(drafts, persist=True)
    drafts = inbox_drafts + drafts  # inbox drafts are already queued
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "drafts.json").write_text(
        json.dumps([d.to_dict() for d in drafts], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Сгенерировано черновиков: %d → out/drafts.json", len(drafts))
    return drafts


def stage_approve(settings: Settings, rounds: int = 1, poll_timeout: int = 25) -> list[dict[str, Any]]:
    return approve_stage.run(settings, rounds=rounds, poll_timeout=poll_timeout)


def stage_publish(settings: Settings) -> list[dict[str, Any]]:
    return publish_stage.run(settings)


def stage_analytics(settings: Settings) -> dict[str, Any]:
    return analytics_stage.run(settings)


def run_all(settings: Settings, max_posts: int = 2) -> dict[str, Any]:
    """Full contour. In DRY_RUN the approval step auto-approves so PUBLISH runs."""
    if settings.dry_run:
        # Start the demo from a clean slate so `make dry-run` gives the same
        # result every time instead of "everything is already published".
        for stale in state.STATE_DIR.glob("*.json"):
            stale.unlink()
    items = stage_collect(settings)
    drafts = stage_generate(settings, items, max_posts=max_posts)
    stage_approve(settings, rounds=1, poll_timeout=0 if settings.dry_run else 25)

    if settings.dry_run:
        queue = postqueue.load_queue()
        for post in queue.get("posts") or []:
            if post.get("status") == "queued":
                post["status"] = "approved"
                post["slot_at"] = None  # publish immediately in a dry run
        postqueue.save_queue(queue)

    published = stage_publish(settings)
    report = stage_analytics(settings)
    return {"items": len(items), "drafts": len(drafts), "published": published, "report": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run", description="Контент-пайплайн канала")
    parser.add_argument("--stage", choices=STAGES, default="all")
    parser.add_argument("--dry-run", action="store_true", help="принудительно включить DRY_RUN")
    parser.add_argument("--max-posts", type=int, default=2, help="сколько постов генерировать за прогон")
    parser.add_argument("--rounds", type=int, default=1, help="сколько раундов long-poll в стадии approve")
    parser.add_argument("--poll-timeout", type=int, default=25)
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.dry_run:
        settings.dry_run = True
    setup_logging(settings.log_level, settings.secret_values)

    if settings.dry_run:
        # A dry run must not touch the repository's real state. It used to
        # write state/queue.json and state/published.json, so a second
        # `make dry-run` found everything already published and produced
        # nothing — and the demo run polluted the committed state files.
        state.STATE_DIR = OUT_DIR / "state"
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        log.info("DRY_RUN: состояние пишется в %s, файлы в state/ не трогаем", state.STATE_DIR)

    log.info(
        "Старт: stage=%s DRY_RUN=%s провайдер=%s модель=%s",
        args.stage, settings.dry_run, settings.llm_provider, settings.llm_model,
    )
    if not settings.dry_run:
        settings.require_telegram()

    if args.stage == "collect":
        stage_collect(settings)
    elif args.stage == "inbox":
        drafts = stage_inbox(settings)
        print(f"черновиков из копилки: {len(drafts)}")
    elif args.stage == "generate":
        stage_generate(settings, max_posts=args.max_posts)
    elif args.stage == "approve":
        stage_approve(settings, rounds=args.rounds, poll_timeout=args.poll_timeout)
    elif args.stage == "publish":
        stage_publish(settings)
    elif args.stage == "analytics":
        result = stage_analytics(settings)
        print("\n" + result["report"])
    else:
        result = run_all(settings, max_posts=args.max_posts)
        print("\n=== СУХОЙ ПРОГОН ЗАВЕРШЁН ===" if settings.dry_run else "\n=== ПРОГОН ЗАВЕРШЁН ===")
        print(f"материалов: {result['items']}, черновиков: {result['drafts']}, "
              f"публикаций: {len(result['published'])}")
        print("\n" + result["report"]["report"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
