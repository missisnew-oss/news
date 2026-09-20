"""Stage 6 — QUEUE: park drafts with a proposed publication slot.

Slots are expressed in Asia/Dubai time (the audience's clock) and stored as
UTC ISO timestamps. The queue is idempotent by ``post_id``: re-running
GENERATE over the same items updates the existing entry instead of adding a
duplicate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import state
from .config import TIMEZONE
from .models import PostDraft

log = logging.getLogger("pipeline.queue")

DUBAI = ZoneInfo(TIMEZONE)

# Publication slots, local Dubai time. Morning slot catches commuters,
# evening slot catches the after-work scroll.
SLOTS = ("09:30", "19:30")

ACTIVE_STATUSES = {"draft", "queued", "approved", "postponed"}


def next_slots(count: int, *, now: datetime | None = None) -> list[datetime]:
    """The next ``count`` free slot datetimes, in UTC."""
    now = (now or datetime.now(DUBAI)).astimezone(DUBAI)
    result: list[datetime] = []
    day_offset = 0
    while len(result) < count and day_offset < 14:
        day = (now + timedelta(days=day_offset)).date()
        for slot in SLOTS:
            hour, minute = (int(part) for part in slot.split(":"))
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=DUBAI)
            if candidate > now:
                result.append(candidate.astimezone(ZoneInfo("UTC")))
                if len(result) == count:
                    break
        day_offset += 1
    return result


def load_queue() -> dict[str, Any]:
    return state.load("queue.json")


def save_queue(queue: dict[str, Any]) -> None:
    state.save("queue.json", queue)


def enqueue(drafts: list[PostDraft], *, persist: bool = True, now: datetime | None = None) -> dict[str, Any]:
    queue = load_queue()
    posts: list[dict[str, Any]] = queue.setdefault("posts", [])
    index = {p.get("post_id"): p for p in posts}

    taken = {p.get("slot_at") for p in posts if p.get("status") in ACTIVE_STATUSES}
    fresh = [d for d in drafts if d.post_id not in index]
    slots = [s for s in next_slots(len(fresh) + len(taken) + 2, now=now)
             if s.isoformat() not in taken]

    for draft in drafts:
        existing = index.get(draft.post_id)
        if existing:
            # Never move an already approved or published post.
            if existing.get("status") in {"approved", "published"}:
                log.info("Пост %s уже %s, пропускаем", draft.post_id, existing["status"])
                continue
            existing.update(draft.to_dict())
            existing["status"] = "queued"
            log.info("Пост %s обновлён в очереди", draft.post_id)
            continue
        draft.status = "queued"
        if slots:
            draft.slot_at = slots.pop(0).isoformat()
        payload = draft.to_dict()
        posts.append(payload)
        index[draft.post_id] = payload
        log.info("Пост %s поставлен в очередь на %s", draft.post_id, draft.slot_at)

    if persist:
        save_queue(queue)
    return queue


def pending_approval(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        p for p in (queue.get("posts") or [])
        if p.get("status") == "queued" and not (p.get("approval") or {}).get("sent_at")
    ]


def due_for_publication(queue: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    from datetime import timezone

    now = now or datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    for post in queue.get("posts") or []:
        if post.get("status") != "approved":
            continue
        slot = post.get("slot_at")
        if not slot:
            due.append(post)
            continue
        try:
            slot_dt = datetime.fromisoformat(slot)
        except ValueError:
            due.append(post)
            continue
        if slot_dt.tzinfo is None:
            slot_dt = slot_dt.replace(tzinfo=timezone.utc)
        if slot_dt <= now:
            due.append(post)
    return due


def set_status(queue: dict[str, Any], post_id: str, status: str, **extra: Any) -> dict[str, Any] | None:
    for post in queue.get("posts") or []:
        if post.get("post_id") == post_id:
            post["status"] = status
            post.update(extra)
            return post
    return None


def postpone(queue: dict[str, Any], post_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    slots = next_slots(4, now=now)
    post = None
    for candidate in queue.get("posts") or []:
        if candidate.get("post_id") == post_id:
            post = candidate
            break
    if not post:
        return None
    taken = {p.get("slot_at") for p in (queue.get("posts") or []) if p is not post}
    for slot in slots:
        iso = slot.isoformat()
        if iso not in taken and iso != post.get("slot_at"):
            post["slot_at"] = iso
            break
    post["status"] = "queued"
    approval = post.setdefault("approval", {})
    approval["sent_at"] = None
    return post
