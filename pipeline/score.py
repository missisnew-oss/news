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
from .stories import Story, cluster, pick_items

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


# How many items of the leading story go into one prompt (one per source),
# and how many single items from other stories may be added as context.
MAX_STORY_ITEMS = 6
EXTRA_STORY_ITEMS = 2
# A digest rubric (``RUBRICS[r]["digest"]``, e.g. the events listing) needs
# many different things, not many accounts of one thing: one item per story,
# this many stories at most.
DIGEST_STORIES = 6


def stories_for_rubric(stories: list[Story], rubric: str) -> list[Story]:
    """Stories that touch at least one source category of the rubric."""
    allowed = set(RUBRICS.get(rubric, {}).get("categories") or [])
    if not allowed:
        return []
    return [s for s in stories if s.categories & allowed]


def select_for_rubric(
    items: list[NormalizedItem],
    rubric: str,
    limit: int = 4,
    *,
    stories: list[Story] | None = None,
    exclude_story_ids: set[str] | None = None,
) -> list[NormalizedItem]:
    """Material for one post: the leading story in full, plus a little context.

    Selection is done over stories, not items. The best story of the rubric
    contributes all its items (up to ``MAX_STORY_ITEMS``, best score first,
    one per source), so the model sees every retelling and can merge them
    into one complete post. If ``limit`` leaves room, up to
    ``EXTRA_STORY_ITEMS`` best items from other stories are added as context.

    A digest rubric (``RUBRICS[rubric]["digest"]``) inverts this: the post is
    a list of different events, so it gets the best item of each of the top
    stories (up to ``limit``, at least ``DIGEST_STORIES``) and never several
    retellings of one event.

    ``stories`` lets the caller cluster once for several rubrics; when it is
    omitted the items are clustered here. ``exclude_story_ids`` keeps a story
    already used by another rubric in the same run from producing a twin post.
    """
    meta = RUBRICS.get(rubric, {})
    if not meta.get("categories"):
        return []
    if stories is None:
        stories = cluster(items)
    excluded = exclude_story_ids or set()
    pool = [s for s in stories_for_rubric(stories, rubric) if s.story_id not in excluded]
    if not pool:
        return []

    if meta.get("digest"):
        picked = []
        for story in pool[: max(limit, DIGEST_STORIES)]:
            picked.extend(pick_items(story, 1))
        return picked

    lead, rest = pool[0], pool[1:]
    picked = pick_items(lead, MAX_STORY_ITEMS)
    room = min(EXTRA_STORY_ITEMS, limit - len(picked))
    for story in rest:
        if room <= 0:
            break
        extra = pick_items(story, 1)
        if extra:
            picked.extend(extra)
            room -= 1
    return picked


def lead_story_for_rubric(
    stories: list[Story], rubric: str, exclude_story_ids: set[str] | None = None
) -> Story | None:
    excluded = exclude_story_ids or set()
    for story in stories_for_rubric(stories, rubric):
        if story.story_id not in excluded:
            return story
    return None


def stories_used(stories: list[Story], rubric: str, pool: list[NormalizedItem]) -> set[str]:
    """Story ids a post of ``rubric`` consumes: the leading story, or — for a
    digest — every story one of its items was taken from."""
    if not pool:
        return set()
    if RUBRICS.get(rubric, {}).get("digest"):
        ids = {i.item_id for i in pool}
        return {s.story_id for s in stories if any(i.item_id in ids for i in s.items)}
    lead_id = pool[0].item_id
    return {s.story_id for s in stories if any(i.item_id == lead_id for i in s.items)}


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

    Rubrics are ranked by the score of their leading story, so an event that
    five channels report outranks a single-source item of the same weight.
    """
    stories = cluster(items)
    available: dict[str, float] = {}
    for rubric_id, meta in RUBRICS.items():
        if meta.get("manual"):
            continue
        lead = lead_story_for_rubric(stories, rubric_id)
        if lead is None:
            continue
        available[rubric_id] = lead.score
    ordered = sorted(available, key=lambda r: available[r], reverse=True)

    past = recent_rubrics() if history is None else list(history)
    selling_so_far = sum(1 for r in past if r in SELLING_RUBRICS)
    total_so_far = len(past)

    plan: list[str] = []
    deferred: list[str] = []
    for rubric_id in ordered:
        if len(plan) >= max_posts:
            break
        projected_total = total_so_far + len(plan) + 1
        if rubric_id in SELLING_RUBRICS:
            if (selling_so_far + 1) / projected_total > cap:
                deferred.append(rubric_id)
                continue
        # A rubric with its own share cap (the meme) is fed by four categories
        # and would otherwise outrank the news rubrics on every run.
        max_share = RUBRICS[rubric_id].get("max_share")
        if max_share is not None:
            so_far = sum(1 for r in past + plan if r == rubric_id)
            if (so_far + 1) / projected_total > max_share:
                deferred.append(rubric_id)
                continue
        if rubric_id in SELLING_RUBRICS:
            selling_so_far += 1
        plan.append(rubric_id)

    if deferred:
        log.info(
            "Рубрики отложены ради потолков (продающие %.0f%%, свои доли): %s",
            cap * 100, ", ".join(deferred),
        )
    if len(plan) < max_posts:
        log.info(
            "План короче запрошенного (%d из %d): не хватает непродающего материала",
            len(plan), max_posts,
        )
    return plan
