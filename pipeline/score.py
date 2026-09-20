"""Stage 3 — SCORE: rank normalised items for the target audience.

score = freshness * w_fresh
      + category_weight * source_weight * w_source
      + keyword_hits * w_keywords
      - penalties

Rubric weights learned from analytics (state/rubric_weights.json) modulate the
category term, which is how ANALYTICS feeds back into SCORE.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from . import state
from .config import RUBRICS, SELLING_RUBRICS
from .models import NormalizedItem

log = logging.getLogger("pipeline.score")

# Terms that matter to a Russian-speaking investor / UAE resident audience.
KEYWORDS_STRONG = (
    "launch", "off-plan", "offplan", "handover", "golden visa", "mortgage",
    "price index", "transactions", "rera", "dld", "escrow", "freehold",
    "title deed", "service charge", "rental yield", "new project",
)
KEYWORDS_SOFT = (
    "dubai", "abu dhabi", "uae", "sharjah", "ras al khaimah", "property",
    "real estate", "visa", "festival", "concert", "expo", "metro", "tariff",
)
NEGATIVE = ("sponsored", "advertorial", "promoted content", "press release distribution")

WEIGHTS = {"freshness": 3.0, "source": 2.0, "keywords": 1.5, "penalty": 2.0}


def freshness(item: NormalizedItem, *, half_life_hours: float = 36.0) -> float:
    """1.0 for a brand-new item, decaying exponentially; 0.35 if date unknown."""
    if not item.published_at:
        return 0.35
    try:
        published = datetime.fromisoformat(item.published_at)
    except ValueError:
        return 0.35
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    return math.exp(-age_hours / half_life_hours)


def keyword_score(item: NormalizedItem) -> float:
    haystack = f"{item.title} {item.summary}".lower()
    strong = sum(1 for kw in KEYWORDS_STRONG if kw in haystack)
    soft = sum(1 for kw in KEYWORDS_SOFT if kw in haystack)
    return min(1.0, strong * 0.30 + soft * 0.08)


def penalty(item: NormalizedItem) -> float:
    haystack = f"{item.title} {item.summary}".lower()
    hits = sum(1 for kw in NEGATIVE if kw in haystack)
    short = 0.3 if len(item.summary) < 40 else 0.0
    return min(1.0, hits * 0.5 + short)


def score_items(
    items: list[NormalizedItem],
    sources_doc: dict[str, Any],
    rubric_weights: dict[str, float] | None = None,
) -> list[NormalizedItem]:
    categories = sources_doc.get("categories") or {}
    source_index = {s["id"]: s for s in (sources_doc.get("sources") or [])}
    learned = rubric_weights if rubric_weights is not None else (
        state.load("rubric_weights.json").get("weights") or {}
    )
    # Map learned rubric weights onto source categories.
    category_bonus: dict[str, float] = {}
    for rubric_id, meta in RUBRICS.items():
        weight = float(learned.get(rubric_id, 1.0))
        for category in meta["categories"]:
            category_bonus[category] = max(category_bonus.get(category, 0.0), weight)

    for item in items:
        source = source_index.get(item.source_id, {})
        cat_weight = float((categories.get(item.category) or {}).get("weight", 1.0))
        src_weight = float(source.get("weight", 1.0))
        reliability = float(source.get("reliability", 3)) / 5.0
        learned_mult = category_bonus.get(item.category, 1.0)

        parts = {
            "freshness": freshness(item) * WEIGHTS["freshness"],
            "source": cat_weight * src_weight * reliability * learned_mult * WEIGHTS["source"],
            "keywords": keyword_score(item) * WEIGHTS["keywords"],
            "penalty": -penalty(item) * WEIGHTS["penalty"],
        }
        item.score_breakdown = {k: round(v, 4) for k, v in parts.items()}
        item.score = round(sum(parts.values()), 4)

    items.sort(key=lambda i: i.score, reverse=True)
    return items


def select_for_rubric(items: list[NormalizedItem], rubric: str, limit: int = 4) -> list[NormalizedItem]:
    """Top items whose source category feeds the given rubric."""
    allowed = set(RUBRICS.get(rubric, {}).get("categories") or [])
    if not allowed:
        return []
    picked = [i for i in items if i.category in allowed]
    return picked[:limit]


# Brief §4: selling content is capped at ~20-25% of the feed.
SELLING_SHARE_CAP = 0.25
# How many recent posts count as "the feed" when measuring that share.
RECENT_WINDOW = 28


def recent_rubrics(limit: int = RECENT_WINDOW) -> list[str]:
    """Rubrics of the posts that are already out, or committed to go out.

    Read from ``state/published.json`` plus the part of ``state/queue.json``
    that is not published yet, so a post approved this morning already counts
    against the selling share of the post planned this evening.
    """
    history = [
        post.get("rubric")
        for post in (state.load("published.json").get("posts") or [])
        if post.get("rubric")
    ]
    history += [
        post.get("rubric")
        for post in (state.load("queue.json").get("posts") or [])
        if post.get("rubric") and post.get("status") in {"queued", "approved", "postponed"}
    ]
    return history[-limit:]


def plan_rubrics(
    items: list[NormalizedItem],
    max_posts: int = 2,
    *,
    history: list[str] | None = None,
    cap: float = SELLING_SHARE_CAP,
) -> list[str]:
    """Choose which rubrics to generate this run.

    Rubrics are ranked by the quality of the material available for them, but
    the selection is then filtered so the share of selling posts in the recent
    feed stays under ``cap``. Ranking by score alone always picked the selling
    rubrics — they feed on the highest-weight source categories — and produced
    runs that were 100% selling, against the brief's 20-25% ceiling.

    ``personal`` is excluded: those posts are written by the owner from a brief.
    """
    available: dict[str, float] = {}
    for rubric_id, meta in RUBRICS.items():
        if meta.get("manual"):
            continue
        pool = select_for_rubric(items, rubric_id, limit=3)
        if not pool:
            continue
        available[rubric_id] = sum(i.score for i in pool) / len(pool)
    ordered = sorted(available, key=lambda r: available[r], reverse=True)

    past = recent_rubrics() if history is None else list(history)
    selling_so_far = sum(1 for r in past if r in SELLING_RUBRICS)
    total_so_far = len(past)

    plan: list[str] = []
    deferred: list[str] = []
    for rubric_id in ordered:
        if len(plan) >= max_posts:
            break
        if rubric_id in SELLING_RUBRICS:
            projected_total = total_so_far + len(plan) + 1
            if (selling_so_far + 1) / projected_total > cap:
                deferred.append(rubric_id)
                continue
            selling_so_far += 1
        plan.append(rubric_id)

    if deferred:
        log.info(
            "Продающие рубрики отложены ради потолка %.0f%%: %s",
            cap * 100, ", ".join(deferred),
        )
    if len(plan) < max_posts:
        log.info(
            "План короче запрошенного (%d из %d): не хватает непродающего материала",
            len(plan), max_posts,
        )
    return plan
