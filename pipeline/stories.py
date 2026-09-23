"""Story clustering: one news event, many retellings.

Eighteen of the registered sources are Telegram channels that write about the
same launch, tariff or rule change in their own words within a few hours of
each other. ``normalize.dedupe`` only collapses identical URLs and identical
normalised titles, so those retellings reach SCORE as separate items and the
model was handed four random takes on one story instead of the whole story.

This module groups items into ``Story`` objects without any external
dependency or LLM call:

1. text = title + summary, lower-cased, punctuation stripped, RU/EN stop
   words removed, crude stemming (tokens cut to 5-6 characters);
2. tokens are weighted: named entities (capitalised words in the original,
   Latin-script names such as Emaar, DEWA, RTA) x3, numbers x2, plain
   tokens x1;
3. two items are "the same story" when the weighted Jaccard similarity of
   their token sets reaches ``threshold`` and they were published within
   ``window_hours`` of each other;
4. items are merged greedily in a deterministic order (single linkage:
   an item joins the story containing its most similar member).

A story's score is the best item score with a bonus for independent sources:
a fact five channels report is more important than a fact one channel
reports.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import NormalizedItem
from .textutil import sha1

log = logging.getLogger("pipeline.stories")

DEFAULT_THRESHOLD = 0.3
WINDOW_HOURS = 72
STEM_LENGTH = 5
ENTITY_WEIGHT = 3.0
NUMBER_WEIGHT = 2.0
SOURCE_BONUS = 0.15
SOURCE_BONUS_CAP = 1.6

STOP_WORDS = frozenset("""
а без более бы был была были было быть в вам вас весь вот все всего всех вы где да
даже для до его ее её ей ему если есть ещё еще же за здесь и из или им их к как ко
когда кто ли либо мне может мы на над надо наш не него нее неё нет ни них но ну о об
однако он она они оно от очень по под при с со так также такой там те тем то того
тоже той только том ты у уже хотя чего чей чем что чтобы чье чья эта эти это этот я
новый новая новое новые новых стал стала стали будет будут года году год этом этой
теперь сейчас также ещё уже
the a an and or of to in on at for by with from as is are was were be been being this
that these those it its into over under about after before between during without
will would can could may might should shall has have had do does did not no yes than
then there here their they them he she we you your our his her who whom which what
when where why how all any each more most other some such only own same so too very
just also new says said say announced announces up out off via per
""".split())

# Words that would otherwise count as named entities but appear in nearly
# every item the channel collects. Treating "Dubai" as a x3 entity would glue
# unrelated stories together.
COMMON_ENTITIES = frozenset(
    "dubai uae emirates emirate дубай дубае дубая оаэ эмират эмираты эмиратах "
    "abu dhabi абу даби sharjah шардж ajman аджман".split()
)

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_LATIN_RE = re.compile(r"^[A-Za-z][A-Za-z&'-]*$")


@dataclass
class Story:
    story_id: str
    items: list[NormalizedItem] = field(default_factory=list)
    keywords: set[str] = field(default_factory=set)
    score: float = 0.0
    source_count: int = 0
    categories: set[str] = field(default_factory=set)

    @property
    def title(self) -> str:
        return self.items[0].title if self.items else ""

    @property
    def source_ids(self) -> set[str]:
        return {i.source_id for i in self.items}


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

def _stem(token: str) -> str:
    """Crude stemmer: cut to STEM_LENGTH (6 for long words) so that
    «застройщик / застройщика / застройщики» collapse onto one token."""
    if len(token) <= STEM_LENGTH:
        return token
    return token[: STEM_LENGTH + 1] if len(token) > 8 else token[:STEM_LENGTH]


def _number_key(raw: str) -> str:
    """'1,2' -> '1.2', '320' -> '320'. Thousand groups are joined upstream."""
    return raw.replace(",", ".").rstrip(".")


def tokenize(text: str) -> dict[str, float]:
    """Weighted token bag for one item: {token: weight}."""
    text = unicodedata.normalize("NFKC", text or "")
    # Join "1 850 000" into "1850000" before scanning numbers.
    text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)
    bag: dict[str, float] = {}
    for raw in _WORD_RE.findall(text):
        if raw.isdigit() or _NUMBER_RE.fullmatch(raw):
            key = "#" + _number_key(raw)
            bag[key] = max(bag.get(key, 0.0), NUMBER_WEIGHT)
            continue
        lowered = raw.lower()
        if len(lowered) < 3 or lowered in STOP_WORDS:
            continue
        is_entity = (
            raw[0].isupper() or _LATIN_RE.match(raw) is not None
        ) and lowered not in COMMON_ENTITIES
        key = _stem(lowered)
        weight = ENTITY_WEIGHT if is_entity else 1.0
        bag[key] = max(bag.get(key, 0.0), weight)
    return bag


def item_tokens(item: NormalizedItem) -> dict[str, float]:
    return tokenize(f"{item.title}. {item.summary}")


def similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Weighted Jaccard: shared weight over union weight."""
    if not a or not b:
        return 0.0
    shared = sum(min(a[t], b[t]) for t in a.keys() & b.keys())
    union = sum(max(a.get(t, 0.0), b.get(t, 0.0)) for t in a.keys() | b.keys())
    return shared / union if union else 0.0


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def _published(item: NormalizedItem) -> datetime | None:
    if not item.published_at:
        return None
    try:
        ts = datetime.fromisoformat(item.published_at)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _within_window(a: NormalizedItem, b: NormalizedItem, hours: float) -> bool:
    ta, tb = _published(a), _published(b)
    if ta is None or tb is None:
        return True  # unknown date: do not block the merge on it
    return abs((ta - tb).total_seconds()) <= hours * 3600


def _sort_key(item: NormalizedItem) -> tuple:
    return (-item.score, item.published_at or "", item.item_id)


def story_score(items: list[NormalizedItem]) -> float:
    """Best item score, boosted by the number of independent sources."""
    if not items:
        return 0.0
    best = max(i.score for i in items)
    sources = len({i.source_id for i in items})
    return round(best * min(SOURCE_BONUS_CAP, 1.0 + SOURCE_BONUS * (sources - 1)), 4)


def cluster(
    items: list[NormalizedItem],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    window_hours: float = WINDOW_HOURS,
) -> list[Story]:
    """Group items into stories. Deterministic: same input, same output.

    Items are visited best-score first; each joins the existing story whose
    most similar member clears ``threshold`` (and lies within the date
    window), otherwise it seeds a new story. Stories come back sorted by
    ``Story.score`` descending, members by item score descending.
    """
    ordered = sorted(items, key=_sort_key)
    bags = {i.item_id: item_tokens(i) for i in ordered}
    groups: list[list[NormalizedItem]] = []

    for item in ordered:
        bag = bags[item.item_id]
        best_group, best_sim = None, 0.0
        for group in groups:
            for member in group:
                if not _within_window(item, member, window_hours):
                    continue
                sim = similarity(bag, bags[member.item_id])
                if sim >= threshold and sim > best_sim:
                    best_group, best_sim = group, sim
        if best_group is None:
            groups.append([item])
        else:
            best_group.append(item)

    stories: list[Story] = []
    for group in groups:
        members = sorted(group, key=_sort_key)
        stories.append(Story(
            story_id="story-" + sha1("|".join(sorted(i.item_id for i in members)))[:12],
            items=members,
            keywords=_keywords(members, bags),
            score=story_score(members),
            source_count=len({i.source_id for i in members}),
            categories={i.category for i in members},
        ))
    stories.sort(key=lambda s: (-s.score, s.story_id))
    _log_summary(stories, len(items))
    return stories


def _keywords(members: list[NormalizedItem], bags: dict[str, dict[str, float]]) -> set[str]:
    """Entities and numbers of the seed item, plus tokens shared by ≥2 members."""
    seed = bags[members[0].item_id]
    keywords = {t for t, w in seed.items() if w > 1.0}
    if len(members) > 1:
        counts: dict[str, int] = {}
        for member in members:
            for token in bags[member.item_id]:
                counts[token] = counts.get(token, 0) + 1
        keywords |= {t for t, n in counts.items() if n >= 2}
    return keywords


def _log_summary(stories: list[Story], total_items: int) -> None:
    if not stories:
        return
    largest = sorted(stories, key=lambda s: (-len(s.items), -s.score))[:3]
    described = "; ".join(
        f"{s.title[:70]} ({s.source_count} источн.)" for s in largest if len(s.items) > 1
    )
    log.info(
        "Историй: %d из %d материалов; крупнейшие: %s",
        len(stories), total_items, described or "все истории из одного материала",
    )


# ---------------------------------------------------------------------------
# Selection helpers used by SCORE
# ---------------------------------------------------------------------------

def pick_items(story: Story, limit: int) -> list[NormalizedItem]:
    """Up to ``limit`` best items of a story, one per source.

    The whole point of clustering is to hand the model every independent
    account of the event, so two posts from the same channel never crowd out
    a second source.
    """
    picked: list[NormalizedItem] = []
    seen_sources: set[str] = set()
    for item in sorted(story.items, key=_sort_key):
        if item.source_id in seen_sources:
            continue
        seen_sources.add(item.source_id)
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked
