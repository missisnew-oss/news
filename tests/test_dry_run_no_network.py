"""DRY_RUN must run the whole contour without opening a single socket."""

from __future__ import annotations

import socket

import pytest

from pipeline import run as run_module
from pipeline.telegram import TelegramClient


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open a socket during the test fails loudly."""

    def forbidden(*args, **kwargs):
        raise AssertionError("DRY_RUN попытался выйти в сеть")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return True


def test_telegram_client_makes_no_calls_in_dry_run(no_network):
    client = TelegramClient("token", dry_run=True)
    client.send_message("@chan", "привет")
    client.send_photo("@chan", "/nonexistent.png", caption="подпись")
    assert [c["method"] for c in client.calls] == ["sendMessage", "sendPhoto"]


def test_get_updates_returns_empty_in_dry_run(no_network):
    assert TelegramClient("token", dry_run=True).get_updates(0, timeout=0) == []


def test_offline_llm_provider_needs_no_network(no_network, settings):
    from pipeline.llm import get_provider

    provider = get_provider(settings)
    assert provider.name == "offline"
    out = provider.complete("system", "RUBRIC_ID: market_pulse\n<INPUT_ITEMS>\n[]\n</INPUT_ITEMS>")
    assert '"rubric"' in out


def test_full_dry_run_end_to_end(no_network, settings, monkeypatch):
    """python -m pipeline.run --dry-run, in-process."""
    result = run_module.run_all(settings, max_posts=2)
    assert result["items"] > 0, "демо-материалы не загрузились"
    assert result["drafts"] > 0, "пайплайн не собрал ни одного черновика"
    assert result["report"]["report"].startswith("<b>Недельный отчёт")


def test_collector_does_not_hit_network_in_dry_run(no_network, settings, sources_doc):
    from pipeline.collect import collect

    items = collect(settings, sources_doc)
    assert isinstance(items, list)
    assert items, "config/sample_items.json должен давать материалы для сухого прогона"
