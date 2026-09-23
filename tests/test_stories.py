"""Story clustering: several retellings of one event become one story."""

from __future__ import annotations

from datetime import datetime, timezone

from pipeline import score
from pipeline.models import NormalizedItem
from pipeline.stories import cluster, pick_items, similarity, story_score, tokenize

NOW = datetime.now(timezone.utc).isoformat()


def _item(item_id, source_id, title, summary, *, category="realty_news",
          published="2026-09-20T08:00:00+00:00", score_=1.0) -> NormalizedItem:
    return NormalizedItem(
        item_id=item_id, source_id=source_id, category=category, title=title,
        summary=summary, url=f"https://{source_id}.example/{item_id}",
        canonical_url=f"https://{source_id}.example/{item_id}", published_at=published,
        collected_at=NOW, lang="ru", score=score_,
    )


# Three channels, one launch, three wordings.
LAUNCH_A = _item(
    "a1", "tg_channel_one",
    "Nakheel открыл продажи вилл на Dubai Islands",
    "Застройщик Nakheel начал продажи 357 вилл и таунхаусов на Dubai Islands. "
    "Таунхаус с 3 спальнями — от 6,1 млн AED.",
    score_=5.0,
)
LAUNCH_B = _item(
    "b1", "tg_channel_two",
    "Dubai Islands: Nakheel запускает новый проект вилл",
    "На Dubai Islands стартовали продажи: всего 357 домов от Nakheel, "
    "цены на таунхаусы начинаются от 6,1 млн AED.",
    published="2026-09-20T11:00:00+00:00", score_=4.0,
)
LAUNCH_C = _item(
    "c1", "gulf_news_rss",
    "Nakheel launches villas on Dubai Islands",
    "Nakheel has launched 357 villas and townhouses on Dubai Islands, "
    "townhouses from AED 6.1 million.",
    published="2026-09-21T02:00:00+00:00", score_=4.5,
)
# Same developer, a different story.
NAKHEEL_OTHER = _item(
    "d1", "tg_channel_one",
    "Nakheel сдал вторую очередь Palm Jebel Ali раньше срока",
    "Застройщик Nakheel передал ключи владельцам 120 вилл на Palm Jebel Ali. "
    "Сдача прошла на 4 месяца раньше графика.",
    score_=3.0,
)
VISA = _item(
    "e1", "tg_channel_two",
    "ОАЭ обновили сроки оформления резидентских виз",
    "Оформление визы резидента теперь занимает до 5 рабочих дней.",
    category="city_gov", score_=2.0,
)


def test_three_retellings_of_one_launch_become_one_story():
    stories = cluster([LAUNCH_A, LAUNCH_B, LAUNCH_C, VISA])
    launch = next(s for s in stories if LAUNCH_A in s.items)
    assert {i.item_id for i in launch.items} == {"a1", "b1", "c1"}
    assert launch.source_count == 3
    assert len(stories) == 2


def test_two_different_stories_about_the_same_developer_stay_apart():
    stories = cluster([LAUNCH_A, NAKHEEL_OTHER, LAUNCH_B])
    launch = next(s for s in stories if LAUNCH_A in s.items)
    assert NAKHEEL_OTHER not in launch.items
    assert len(stories) == 2


def test_identical_headlines_ten_days_apart_are_not_merged():
    early = _item("f1", "tg_channel_one", LAUNCH_A.title, LAUNCH_A.summary,
                  published="2026-09-01T08:00:00+00:00")
    late = _item("f2", "tg_channel_two", LAUNCH_A.title, LAUNCH_A.summary,
                 published="2026-09-11T08:00:00+00:00")
    assert similarity(tokenize(early.title), tokenize(late.title)) == 1.0
    stories = cluster([early, late])
    assert len(stories) == 2


def test_clustering_is_deterministic_regardless_of_input_order():
    forward = cluster([LAUNCH_A, LAUNCH_B, LAUNCH_C, NAKHEEL_OTHER, VISA])
    backward = cluster([VISA, NAKHEEL_OTHER, LAUNCH_C, LAUNCH_B, LAUNCH_A])
    assert [s.story_id for s in forward] == [s.story_id for s in backward]
    assert [[i.item_id for i in s.items] for s in forward] == \
        [[i.item_id for i in s.items] for s in backward]


def test_story_score_rewards_independent_sources_with_a_cap():
    assert story_score([LAUNCH_A]) == 5.0
    assert story_score([LAUNCH_A, LAUNCH_B, LAUNCH_C]) == 5.0 * 1.3
    same_source_twice = [LAUNCH_A, _item("x", "tg_channel_one", "t", "s", score_=1.0)]
    assert story_score(same_source_twice) == 5.0, "два поста одного канала — один источник"
    many = [_item(f"m{n}", f"src{n}", "t", "s", score_=2.0) for n in range(10)]
    assert story_score(many) == 2.0 * 1.6


def test_entities_and_numbers_weigh_more_than_plain_words():
    bag = tokenize("Emaar открыл продажи 320 квартир в новой башне")
    assert bag["emaar"] == 3.0
    assert bag["#320"] == 2.0
    assert bag["прода"] == 1.0, "обычное слово после грубого стемминга"
    islands = tokenize("Dubai Islands")
    assert islands["dubai"] == 1.0, "«Dubai» есть везде и сущностью не считается"
    assert islands["islan"] == 3.0


def test_pick_items_takes_one_item_per_source():
    story = cluster([LAUNCH_A, LAUNCH_B, LAUNCH_C,
                     _item("a2", "tg_channel_one", LAUNCH_A.title, LAUNCH_A.summary, score_=4.9)])
    launch = next(s for s in story if LAUNCH_A in s.items)
    picked = pick_items(launch, 6)
    assert [i.source_id for i in picked] == ["tg_channel_one", "gulf_news_rss", "tg_channel_two"]
    assert picked[0].item_id == "a1", "из одного канала берётся лучший по score"


def test_select_for_rubric_sends_the_whole_story_plus_context():
    items = [LAUNCH_A, LAUNCH_B, LAUNCH_C, NAKHEEL_OTHER, VISA]
    pool = score.select_for_rubric(items, "market_pulse", limit=4)
    ids = [i.item_id for i in pool]
    assert ids[:3] == ["a1", "c1", "b1"], "сначала все материалы ведущей истории"
    assert ids[3:] == ["d1"], "затем контекст из другой истории в пределах лимита"
    assert "e1" not in ids, "city_gov не питает market_pulse"


def test_select_for_rubric_can_skip_a_story_already_used():
    items = [LAUNCH_A, LAUNCH_B, LAUNCH_C, NAKHEEL_OTHER]
    stories = cluster(items)
    lead = score.lead_story_for_rubric(stories, "market_pulse")
    pool = score.select_for_rubric(items, "market_pulse", stories=stories,
                                   exclude_story_ids={lead.story_id})
    assert [i.item_id for i in pool] == ["d1"]


def test_plan_rubrics_prefers_the_rubric_with_the_multi_source_story():
    single = _item("s1", "src_a", "Жизнь в Дубае: где оформить Ejari быстрее",
                   "Ejari оформляется онлайн за один день.", category="lifestyle", score_=5.0)
    items = [LAUNCH_A, LAUNCH_B, LAUNCH_C, single]
    plan = score.plan_rubrics(items, max_posts=1, history=[])
    assert plan == ["market_pulse"]
