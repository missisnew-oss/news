"""Telegram channels as a source: read the public web preview at t.me/s/<name>.

The Bot API cannot read other people's channels, so the pipeline uses the
public preview page instead. It needs no login, works from GitHub Actions and
returns the last ~20 posts with text, date, permalink and photo.

Limits (documented in docs/SOURCES.md):
* only public channels with a web preview — a private channel or one that
  disabled the preview returns a page without messages;
* the page is HTML meant for browsers, so the parser is deliberately tolerant
  and every result is treated as untrusted input (see normalize.scrub_untrusted).
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

from .textutil import strip_html

PREVIEW_HOST = "t.me"
_MESSAGE_SPLIT = re.compile(r'class="tgme_widget_message_wrap', re.I)
_POST_ID = re.compile(r'data-post="([^"/]+)/(\d+)"')
_TEXT_OPEN = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>', re.I)
_DIV_TAG = re.compile(r"<(/?)div\b[^>]*>", re.I)
_DATE = re.compile(r'<time[^>]+datetime="([^"]+)"', re.I)
_PHOTO = re.compile(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", re.I)
_VIEWS = re.compile(r'class="tgme_widget_message_views">([^<]+)<', re.I)
_TITLE_LIMIT = 120


def channel_username(url: str) -> str | None:
    """``https://t.me/s/uaegeneralnews`` → ``uaegeneralnews``; None if not a preview URL."""
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {PREVIEW_HOST, "www." + PREVIEW_HOST}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) == 2 and parts[0] == "s":
        return parts[1]
    if len(parts) == 1 and not parts[0].startswith("+"):
        return parts[0]
    return None


def preview_url(username: str) -> str:
    return f"https://{PREVIEW_HOST}/s/{username.lstrip('@')}"


def _text_div_inner(block: str) -> str:
    """Body of the message-text div, found by tracking <div> nesting.

    The text may contain nested divs (quotes, tg-emoji wrappers) and what
    follows it changes between Telegram releases, so a lazy regex up to the
    next </div> is not reliable.
    """
    start = _TEXT_OPEN.search(block)
    if not start:
        return ""
    depth = 1
    for tag in _DIV_TAG.finditer(block, start.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return block[start.end():tag.start()]
    return block[start.end():]


def _message_text(block: str) -> str:
    raw = _text_div_inner(block)
    if not raw:
        return ""
    # <br> is the only line structure a channel post has; keep it as a newline
    # so the first line can serve as the headline.
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    lines = [strip_html(line) for line in raw.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _headline(text: str) -> str:
    first = text.split("\n", 1)[0].strip()
    if len(first) > _TITLE_LIMIT:
        cut = first[:_TITLE_LIMIT].rsplit(" ", 1)[0]
        first = cut + "…"
    return first


def _views(block: str) -> int | None:
    match = _VIEWS.search(block)
    if not match:
        return None
    value = match.group(1).strip().upper().replace(",", ".")
    mult = 1
    if value.endswith("K"):
        mult, value = 1_000, value[:-1]
    elif value.endswith("M"):
        mult, value = 1_000_000, value[:-1]
    try:
        return int(float(value) * mult)
    except ValueError:
        return None


def parse_preview(payload: bytes | str, source: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """Turn a t.me/s page into raw items in the collector's schema."""
    page = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    blocks = _MESSAGE_SPLIT.split(page)[1:]
    items: list[dict[str, Any]] = []
    for block in blocks:
        post = _POST_ID.search(block)
        if not post:
            continue
        username, msg_id = post.group(1), post.group(2)
        text = _message_text(block)
        if len(text) < 20:  # bare photo / sticker / service message — nothing to retell
            continue
        date = _DATE.search(block)
        photo = _PHOTO.search(block)
        items.append({
            "source_id": source["id"],
            "title": _headline(text),
            "summary": text[:1200],
            "url": f"https://{PREVIEW_HOST}/{username}/{msg_id}",
            "published_at": html.unescape(date.group(1)) if date else None,
            "image_url": html.unescape(photo.group(1)) if photo else None,
            "raw_text": text[:4000],
            "views": _views(block),
        })
    # The page lists oldest first; the pipeline expects newest first.
    items.reverse()
    return items[:limit]


def has_preview(payload: bytes | str) -> bool:
    """False for private channels and channels that hide the web preview."""
    page = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    return bool(_POST_ID.search(page))
