"""Data contracts shared across pipeline stages.

NormalizedItem is the single normalised shape every collector must produce.
PostDraft is what GENERATE produces and QUEUE/APPROVE/PUBLISH carry forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

NORMALIZED_ITEM_FIELDS = (
    "item_id", "source_id", "category", "title", "summary", "url",
    "canonical_url", "published_at", "collected_at", "lang", "image_url",
    "tags", "raw_text", "dedupe_hash", "score", "score_breakdown",
)


@dataclass
class NormalizedItem:
    item_id: str
    source_id: str
    category: str
    title: str
    summary: str
    url: str
    canonical_url: str
    published_at: str | None
    collected_at: str
    lang: str
    image_url: str | None = None
    tags: list[str] = field(default_factory=list)
    raw_text: str = ""
    dedupe_hash: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NormalizedItem":
        known = {k: v for k, v in data.items() if k in NORMALIZED_ITEM_FIELDS}
        return cls(**known)


@dataclass
class PostDraft:
    """A generated post waiting for approval and publication."""

    post_id: str
    rubric: str
    title: str
    body: str
    hashtags: list[str] = field(default_factory=list)
    cta: str = ""
    image: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    self_check: dict[str, bool] = field(default_factory=dict)
    length_chars: int = 0
    needs_separate_text: bool = False

    # Pipeline bookkeeping (not produced by the LLM).
    status: str = "draft"           # draft|queued|approved|rejected|postponed|published|failed
    created_at: str = ""
    slot_at: str | None = None      # ISO timestamp, Asia/Dubai slot proposed by QUEUE
    image_path: str | None = None
    image_meta: dict[str, Any] = field(default_factory=dict)
    item_ids: list[str] = field(default_factory=list)
    gate: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    publish_key: str = ""           # idempotency key, see pipeline.publish

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PostDraft":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)
