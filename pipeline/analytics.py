"""Stage 9 — ANALYTICS: weekly report and feedback into SCORE.

What the Bot API can and cannot give us is documented in docs/ANALYTICS.md.
In short: subscriber count is available (``getChatMemberCount``); per-post
views and reactions are NOT available to a bot for a channel post, so those
fields stay ``null`` until the owner fills them in from the Telegram app.
The report is therefore explicit about which numbers are automatic and which
are manual.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import state
from .config import RUBRICS, Settings
from .telegram import TelegramClient

log = logging.getLogger("pipeline.analytics")

# Rubric weights stay inside this band so one good week cannot dominate SCORE.
WEIGHT_MIN, WEIGHT_MAX = 0.6, 1.6
LEARNING_RATE = 0.25
MIN_SAMPLE = 3  # fewer posts than this in a rubric -> weight untouched


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def posts_in_window(analytics: dict[str, Any], days: int = 7,
                    *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    result = []
    for post in analytics.get("posts") or []:
        published = _parse(post.get("published_at"))
        if published and published >= cutoff:
            result.append(post)
    return result


def engagement_rate(post: dict[str, Any]) -> float | None:
    metrics = post.get("metrics") or {}
    views = metrics.get("views")
    if not views:
        return None
    reactions = metrics.get("reactions") or 0
    comments = metrics.get("comments") or 0
    return round((reactions + comments) / views, 4)


def rubric_stats(posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for post in posts:
        rubric = post.get("rubric") or "unknown"
        row = stats.setdefault(rubric, {"posts": 0, "er_values": [], "views": []})
        row["posts"] += 1
        er = engagement_rate(post)
        if er is not None:
            row["er_values"].append(er)
        views = (post.get("metrics") or {}).get("views")
        if views:
            row["views"].append(views)
    for rubric, row in stats.items():
        row["avg_er"] = round(sum(row["er_values"]) / len(row["er_values"]), 4) if row["er_values"] else None
        row["avg_views"] = round(sum(row["views"]) / len(row["views"])) if row["views"] else None
        row["measured"] = len(row["er_values"])
    return stats


def recalc_weights(stats: dict[str, dict[str, Any]], current: dict[str, float] | None = None) -> dict[str, float]:
    """Nudge rubric weights toward the rubrics that actually engage readers.

    A rubric moves only when it has at least MIN_SAMPLE measured posts, the
    step is damped by LEARNING_RATE, and the result is clamped to
    [WEIGHT_MIN, WEIGHT_MAX]; so a single viral post cannot take over the feed.
    """
    weights = {rid: float((current or {}).get(rid, 1.0)) for rid in RUBRICS}
    measured = {
        rid: row["avg_er"]
        for rid, row in stats.items()
        if row.get("avg_er") is not None and row.get("measured", 0) >= MIN_SAMPLE
    }
    if len(measured) < 2:
        log.info("Недостаточно данных для пересчёта весов рубрик (%d рубрик)", len(measured))
        return weights
    mean_er = sum(measured.values()) / len(measured)
    if mean_er <= 0:
        return weights
    for rubric, er in measured.items():
        target = er / mean_er
        updated = weights.get(rubric, 1.0) * (1 - LEARNING_RATE) + target * LEARNING_RATE
        weights[rubric] = round(min(WEIGHT_MAX, max(WEIGHT_MIN, updated)), 3)
    return weights


def build_report(analytics: dict[str, Any], leads: dict[str, Any],
                 subscribers: int | None, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    posts = posts_in_window(analytics, 7, now=now)
    stats = rubric_stats(posts)
    measured = [p for p in posts if engagement_rate(p) is not None]
    avg_er = (
        round(sum(engagement_rate(p) or 0 for p in measured) / len(measured), 4)
        if measured else None
    )
    selling = sum(1 for p in posts if RUBRICS.get(p.get("rubric"), {}).get("selling"))
    share = round(selling / len(posts) * 100) if posts else 0
    lead_count = len(leads.get("leads") or [])

    lines = [
        f"<b>Недельный отчёт канала</b> ({now.strftime('%d.%m.%Y')})",
        "",
        f"Опубликовано постов: <b>{len(posts)}</b>",
        f"Продающих: <b>{selling}</b> ({share}% — целевой потолок 25%)",
        f"Заявок на лончи: <b>{lead_count}</b>",
        f"Подписчиков: <b>{subscribers if subscribers is not None else 'нет данных'}</b>",
        f"Средний ER: <b>{avg_er if avg_er is not None else 'нет данных (заполните вручную)'}</b>",
        "",
        "<b>По рубрикам:</b>",
    ]
    for rubric, row in sorted(stats.items(), key=lambda kv: kv[1]["posts"], reverse=True):
        title = RUBRICS.get(rubric, {}).get("title", rubric)
        er = row["avg_er"]
        lines.append(
            f"• {title}: {row['posts']} шт., ER "
            + (f"{er:.2%}" if er is not None else "—")
        )
    if not stats:
        lines.append("• за неделю публикаций не было")

    unmeasured = len(posts) - len(measured)
    if unmeasured:
        lines += [
            "",
            f"⚠️ У {unmeasured} постов нет просмотров/реакций. "
            "Bot API их не отдаёт — впишите цифры из статистики Telegram "
            "в state/analytics.json (см. docs/ANALYTICS.md).",
        ]
    return "\n".join(lines)


def run(settings: Settings, *, persist: bool = True, now: datetime | None = None) -> dict[str, Any]:
    analytics = state.load("analytics.json")
    leads = state.load("leads.json") if (state.state_path("leads.json")).exists() else {"leads": []}
    client = TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)

    subscribers: int | None = None
    if settings.telegram_channel_id:
        try:
            subscribers = client.get_chat_member_count(settings.telegram_channel_id)
        except Exception as exc:
            log.warning("Не удалось получить число подписчиков: %s", exc)

    posts = posts_in_window(analytics, 7, now=now)
    stats = rubric_stats(posts)
    weights_state = state.load("rubric_weights.json")
    weights = recalc_weights(stats, weights_state.get("weights") or {})
    report = build_report(analytics, leads, subscribers, now=now)

    if persist:
        weights_state["weights"] = weights
        weights_state["updated_at"] = (now or datetime.now(timezone.utc)).isoformat()
        state.save("rubric_weights.json", weights_state)
        analytics.setdefault("weekly", []).append({
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "posts": len(posts),
            "subscribers": subscribers,
            "rubrics": stats,
        })
        state.save("analytics.json", analytics)

    if settings.telegram_owner_id:
        try:
            client.send_message(settings.telegram_owner_id, report)
        except Exception as exc:
            log.warning("Отчёт не отправлен: %s", exc)

    return {"report": report, "weights": weights, "stats": stats, "subscribers": subscribers}
