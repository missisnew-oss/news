"""Rubric ids are a contract shared by code, prompts and documentation."""

from __future__ import annotations

import re

from pipeline.config import PROMPTS_DIR, RUBRICS, ROOT, SELLING_RUBRICS
from pipeline.prompts import build_prompt, load_rubric_prompt, load_system_prompt

RUBRICS_DOC = ROOT / "docs" / "RUBRICS.md"


def test_every_rubric_has_a_prompt_file():
    missing = [rid for rid in RUBRICS if not (PROMPTS_DIR / f"{rid}.md").exists()]
    assert not missing, f"нет файлов промптов: {missing}"


def test_system_prompt_file_exists():
    assert (PROMPTS_DIR / "system_tone.md").exists()
    assert len(load_system_prompt()) > 200


def test_no_stray_prompt_files_for_unknown_rubrics():
    known = set(RUBRICS) | {"system_tone", "README"}
    stray = [p.stem for p in PROMPTS_DIR.glob("*.md") if p.stem not in known]
    assert not stray, f"промпты без соответствующей рубрики в коде: {stray}"


def test_every_rubric_is_documented():
    text = RUBRICS_DOC.read_text(encoding="utf-8")
    missing = [rid for rid in RUBRICS if rid not in text]
    assert not missing, f"рубрики не описаны в docs/RUBRICS.md: {missing}"


def test_rubric_prompts_mention_the_json_schema():
    """A prompt that forgets the schema produces unparsable answers."""
    weak = []
    for rid in RUBRICS:
        text = load_rubric_prompt(rid)
        if "self_check" not in text and "JSON" not in text.upper():
            weak.append(rid)
    assert not weak, f"в промптах нет требования JSON-ответа: {weak}"


def test_build_prompt_substitutes_every_placeholder():
    system, user = build_prompt("market_pulse", [], today="2026-09-20")
    assert system
    assert "{{items}}" not in user and "{{rubric}}" not in user
    assert "{{max_chars}}" not in user and "{{today}}" not in user
    assert "RUBRIC_ID: market_pulse" in user
    assert "<INPUT_ITEMS>" in user


def test_selling_share_stays_within_the_brief_limit():
    """docs/CONTENT_STRATEGY.md promises <=25% selling content."""
    total = sum(meta["per_week"] for meta in RUBRICS.values())
    selling = sum(RUBRICS[rid]["per_week"] for rid in SELLING_RUBRICS)
    assert selling / total <= 0.25 + 1e-9, f"продающего контента {selling}/{total}"


def test_personal_rubric_is_marked_manual():
    assert RUBRICS["personal"].get("manual") is True
