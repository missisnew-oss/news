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
from . import normalize as normalize_stage
from . import postqueue, publish as publish_stage, score as score_stage, state
from .config import OUT_DIR, Settings, load_settings, load_sources
from .generate import generate
from .logging_setup import setup_logging
from .models import PostDraft

log = logging.getLogger("pipeline.run")

STAGES = ("collect", "generate", "approve", "publish", "analytics", "all")


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
    return items


def stage_generate(settings: Settings, items: list[Any] | None = None, max_posts: int = 2) -> list[PostDraft]:
    items = items if items is not None else stage_collect(settings)
    plan = score_stage.plan_rubrics(items, max_posts=max_posts)
    log.info("План рубрик на этот прогон: %s", ", ".join(plan) or "пусто")
    drafts = generate(settings, items, plan)

    for draft in drafts:
        image_path, image_meta = illustrate_stage.illustrate(settings, draft)
        draft.image_path = image_path
        draft.image_meta = image_meta

    postqueue.enqueue(drafts, persist=True)
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

    log.info(
        "Старт: stage=%s DRY_RUN=%s провайдер=%s модель=%s",
        args.stage, settings.dry_run, settings.llm_provider, settings.llm_model,
    )
    if not settings.dry_run:
        settings.require_telegram()

    if args.stage == "collect":
        stage_collect(settings)
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
