"""Collect the owner's own posts as tone-of-voice samples.

`python -m pipeline.voice_samples --channel nudeassphilosophy` reads the public
preview of the channel (same parser the collector uses) and writes the texts
to docs/voice/samples_<channel>.md. The file is the raw material for
docs/TONE_OF_VOICE.md: real sentences beat any description of a style.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT as ROOT_DIR
from .telegram_source import parse_preview, preview_url

log = logging.getLogger("pipeline.voice_samples")
VOICE_DIR = ROOT_DIR / "docs" / "voice"


def fetch_samples(channel: str, *, limit: int = 40, timeout: int = 20) -> list[dict]:
    import requests

    url = preview_url(channel)
    items: list[dict] = []
    before: int | None = None
    # The preview shows ~20 posts per page; older pages hang off ?before=<id>.
    for _ in range(4):
        page_url = url if before is None else f"{url}?before={before}"
        response = requests.get(page_url, timeout=timeout,
                                headers={"User-Agent": "DubaiNewsBot/1.0 (voice samples)"})
        response.raise_for_status()
        page = parse_preview(response.content, {"id": channel}, limit=100)
        if not page:
            break
        items.extend(page)
        before = min(int(i["url"].rsplit("/", 1)[1]) for i in page)
        if len(items) >= limit:
            break
    seen: set[str] = set()
    unique = [i for i in items if not (i["url"] in seen or seen.add(i["url"]))]
    unique.sort(key=lambda i: i.get("published_at") or "", reverse=True)
    return unique[:limit]


def write_markdown(channel: str, items: list[dict]) -> Path:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    path = VOICE_DIR / f"samples_{channel}.md"
    lines = [
        f"# Образцы голоса: @{channel}",
        "",
        f"Собрано {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}, постов: {len(items)}.",
        "Это тексты владельца канала — сырьё для docs/TONE_OF_VOICE.md, не для публикации.",
        "",
    ]
    for item in items:
        lines += [f"## {item.get('published_at', '')[:10]} — {item['url']}", "", item["raw_text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Образцы голоса из публичного Telegram-канала")
    parser.add_argument("--channel", required=True, help="username без @")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    channel = args.channel.lstrip("@").strip()
    items = fetch_samples(channel, limit=args.limit)
    if not items:
        log.error("@%s: превью не отдало ни одного текстового поста", channel)
        return 1
    path = write_markdown(channel, items)
    total = sum(len(i["raw_text"]) for i in items)
    log.info("@%s: %d постов, %d символов → %s", channel, len(items), total, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
