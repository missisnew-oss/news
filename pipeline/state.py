"""Persistent state stored as JSON files inside the repository.

GitHub Actions runners are stateless, so the repository itself is the
database: every stage reads and writes ``state/*.json`` and a workflow step
commits the diff. Writes are atomic (temp file + os.replace) so an
interrupted run cannot leave a half-written file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import STATE_DIR

# Known state files and their empty shape.
DEFAULTS: dict[str, Any] = {
    "seen_items.json": {"version": 1, "items": {}},
    "queue.json": {"version": 1, "posts": []},
    "published.json": {"version": 1, "posts": [], "keys": []},
    "telegram_offset.json": {"version": 1, "offset": 0, "updated_at": None},
    "analytics.json": {"version": 1, "posts": [], "weekly": []},
    "rubric_weights.json": {"version": 1, "weights": {}, "updated_at": None},
    "sources_health.json": {"version": 1, "checked_at": None, "sources": {}},
    # Everything the owner forwards or writes to the bot: voice samples,
    # floor plans, payment plans, FAQ drafts — raw material for later stages.
    "inbox.json": {"version": 1, "items": []},
}


def state_path(name: str) -> Path:
    return STATE_DIR / name


def load(name: str) -> dict[str, Any]:
    path = state_path(name)
    if not path.exists():
        return json.loads(json.dumps(DEFAULTS.get(name, {})))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupted state file must not wedge the pipeline forever.
        return json.loads(json.dumps(DEFAULTS.get(name, {})))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULTS.get(name, {})))
    return data


def save(name: str, data: dict[str, Any], *, compact_json: bool = False) -> Path:
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # compact_json keeps big snapshots on one line so the state commit
            # guard (diff-size limit) does not refuse them.
            json.dump(data, fh, ensure_ascii=False, indent=None if compact_json else 2,
                      sort_keys=False)
            fh.write("\n")
        # mkstemp creates 0600; state files are ordinary tracked files.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def prune_seen(seen: dict[str, Any], keep_days: int = 45) -> dict[str, Any]:
    """Drop dedupe entries older than keep_days so state stays small."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    items = seen.get("items") or {}
    kept = {}
    for key, meta in items.items():
        raw = (meta or {}).get("first_seen_at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept[key] = meta
    seen["items"] = kept
    return seen
