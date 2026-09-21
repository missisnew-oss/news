"""Shared fixtures. Every test runs against a throwaway state directory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import state  # noqa: E402
from pipeline.config import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect state/*.json into a temp dir so tests never touch the repo."""
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path / "state"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        dry_run=True,
        telegram_bot_token="test-token",
        telegram_channel_id="@test_channel",
        telegram_owner_id="42",
        llm_provider="anthropic",
        llm_model="claude-opus-5",
    )


@pytest.fixture
def sources_doc() -> dict:
    return {
        "version": 1,
        "categories": {
            "realty_news": {"title": "Новости", "weight": 1.0},
            "events": {"title": "События", "weight": 0.8},
        },
        "defaults": {"timeout_sec": 5, "user_agent": "test", "max_items_per_source": 5},
        "sources": [
            {
                "id": "demo_realty_news", "title": "Demo", "url": "https://example.com/feed",
                "homepage": "https://example.com", "type": "rss", "category": "realty_news",
                "lang": "en", "reliability": 4, "weight": 1.0, "enabled": True,
                "license": {"reuse": "summary_with_link", "note": ""},
                "verification": {"status": "ok", "http_code": 200},
            }
        ],
    }
