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


def test_select_for_rubric_sends_the_whole_story_and_nothing_else():
    """Items of other stories used to be added «for context»; the Dubizzle
    post got a wrestling tournament next to it. The pool is the lead story."""
    items = [LAUNCH_A, LAUNCH_B, LAUNCH_C, NAKHEEL_OTHER, VISA]
    pool = score.select_for_rubric(items, "market_pulse", limit=4)
    ids = [i.item_id for i in pool]
    assert ids == ["a1", "c1", "b1"], "все материалы ведущей истории и только они"
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


# --- QA round 2 -------------------------------------------------------------

PARK_LANE = _item(
    "pl", "tg_ruble",
    "Emaar запустил Park Lane 2 в Dubai Hills Estate",
    "Emaar открыл продажи Park Lane 2 в Dubai Hills Estate. Квартиры 1–3 спальни, "
    "от 1,6 млн AED, план оплаты 80/20, сдача 2028.",
    score_=5.0,
)
GOLF_GRAND = _item(
    "gg", "tg_whitewill",
    "Emaar запустил Golf Grand 3 в Dubai Hills Estate",
    "Emaar открыл продажи Golf Grand 3 в Dubai Hills Estate. Квартиры 1–3 спальни, "
    "от 1,9 млн AED, план оплаты 80/20, сдача 2029.",
    score_=4.0,
)
PARK_LANE_EN = _item(
    "ple", "gulf_news_rss",
    "Emaar launches Park Lane 2 in Dubai Hills Estate",
    "Emaar Properties has launched Park Lane 2 at Dubai Hills Estate with one- to "
    "three-bedroom apartments starting at AED 1.6 million, 80/20 payment plan and "
    "handover in 2028.",
    score_=4.5,
)


def test_same_developer_same_district_different_projects_do_not_merge():
    """Same channel template, same developer and district, but two launches.

    Plain token similarity is 0.59 here — higher than between a Russian and an
    English retelling of one launch — so only the proper names tell them apart.
    """
    assert similarity(tokenize(PARK_LANE.title + PARK_LANE.summary),
                      tokenize(GOLF_GRAND.title + GOLF_GRAND.summary)) > 0.5
    stories = cluster([PARK_LANE, GOLF_GRAND, PARK_LANE_EN])
    by_item = {i.item_id: s.story_id for s in stories for i in s.items}
    assert by_item["pl"] == by_item["ple"], "русский и английский пересказ одного лонча — одна история"
    assert by_item["pl"] != by_item["gg"], "Park Lane 2 и Golf Grand 3 — разные лончи"


def test_proper_names_skip_sentence_starts_and_common_words():
    from pipeline.stories import names_conflict, proper_names

    names = proper_names("Застройщик Emaar открыл продажи Park Lane 2 в Dubai Hills. Квартиры от 1,6 млн AED.")
    assert "emaar" in names and "park" in names and "lane" in names and "hills" in names
    assert "кварт" not in names and "застр" not in names, "слово в начале предложения — не имя"
    assert "dubai" not in names, "«Dubai» есть везде"
    assert not names_conflict({"emaar", "park", "lane"}, {"emaar", "park", "lane", "prope"})
    assert names_conflict({"emaar", "park", "lane"}, {"emaar", "golf", "grand"})


def _event(n: int, source: str, title: str, summary: str, score_: float) -> NormalizedItem:
    return _item(f"ev{n}", source, title, summary, category="events", score_=score_)


AFISHA = [
    _event(1, "visitdubai_events_calendar", "Dubai Fitness Challenge стартует 1 ноября",
           "30 дней бесплатных тренировок в парках города, открытие на Kite Beach.", 5.0),
    _event(2, "visitdubai_events_calendar", "Концерт Coldplay на Etihad Arena",
           "Выступление 12 октября, билеты от 350 AED, площадка на Yas Island.", 4.8),
    _event(3, "platinumlist_arena_events", "Coldplay в Etihad Arena — билеты в продаже",
           "12 октября, Etihad Arena, от 350 AED.", 4.6),
    _event(4, "dubai_culture_events", "Ярмарка Global Village открывает сезон",
           "Сезон 30 открывается 15 октября, вход 30 AED, парковка бесплатная.", 4.4),
    _event(5, "visitdubai_events_calendar", "Выставка GITEX в DWTC",
           "13–17 октября, Dubai World Trade Centre, регистрация онлайн.", 4.2),
    _event(6, "dwtc_events", "Cityscape Global в DWTC: даты", "11–13 ноября, выставка недвижимости.", 4.0),
]


def test_digest_rubric_gets_one_item_per_event_from_several_stories():
    """The events listing needs many different events, not the retellings of one."""
    from pipeline.config import RUBRICS
    from pipeline.generate import ITEMS_PER_POST

    assert RUBRICS["events_afisha"].get("digest") is True
    pool = score.select_for_rubric(AFISHA, "events_afisha", limit=ITEMS_PER_POST)
    ids = [i.item_id for i in pool]
    assert len(ids) >= 5, f"дайджест получил слишком мало событий: {ids}"
    assert not ({"ev2", "ev3"} <= set(ids)), "два пересказа одного концерта — одно событие"
    assert ids.count("ev1") == 1 and "ev4" in ids and "ev5" in ids


def test_digest_marks_every_story_it_took_as_used():
    stories = cluster(AFISHA)
    pool = score.select_for_rubric(AFISHA, "events_afisha", limit=4, stories=stories)
    used = score.stories_used(stories, "events_afisha", pool)
    assert len(used) == len(pool)
    lead = score.lead_story_for_rubric(stories, "market_pulse")
    assert lead is None, "events не питают market_pulse"
    # A normal rubric consumes only its leading story.
    pool = score.select_for_rubric([LAUNCH_A, LAUNCH_B, LAUNCH_C, NAKHEEL_OTHER], "market_pulse", limit=4)
    used = score.stories_used(cluster([LAUNCH_A, LAUNCH_B, LAUNCH_C, NAKHEEL_OTHER]), "market_pulse", pool)
    assert len(used) == 1


def test_meme_rubric_is_capped_by_its_share_of_the_feed():
    """Fed by four categories the meme would win every run; max_share holds it
    to roughly one post in eight."""
    from pipeline import score
    from pipeline.config import RUBRICS

    assert RUBRICS["meme"]["max_share"] < 0.2
    history = ["dubai_life"] * 3 + ["meme"] + ["rules_and_laws"] * 3
    ranked = ["meme", "dubai_life", "market_pulse"]
    assert score.plan_rubrics.__doc__  # sanity
    # Same items for every rubric: only the cap can drop the meme.
    import pipeline.score as sc
    stories = []
    original = sc.lead_story_for_rubric

    class _Lead:
        score = 5.0

    sc.lead_story_for_rubric = lambda stories, rubric, excluded=None: _Lead() if rubric in ranked else None
    try:
        plan = sc.plan_rubrics([], max_posts=2, history=history)
        assert "meme" not in plan and len(plan) == 2
        plan_fresh = sc.plan_rubrics([], max_posts=3, history=["dubai_life"] * 12)
        assert "meme" in plan_fresh
        # The best news rubric keeps the first slot; the meme is the second post.
        assert plan_fresh[0] != "meme" and plan_fresh[1] == "meme"
        assert sc.plan_rubrics([], max_posts=1, history=["dubai_life"] * 12) != ["meme"]
        # No meme in the recent feed at all: one is due even on a short feed.
        assert "meme" in sc.plan_rubrics([], max_posts=2, history=["dubai_life"] * 2)
    finally:
        sc.lead_story_for_rubric = original
