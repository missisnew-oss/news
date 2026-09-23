"""Enrich the items a post is written from with the article behind the link.

Why: a Telegram channel repost is two sentences («Dubizzle запустил прямое
бронирование…»). Another channel wrote a rich post about the same news
because its author read the original article: scale (10 000 objects a
month), who operates them, what it means for owners. The model cannot
write what it was not given, and the fact-check gate rightly blocks any
number that is not in the input. So, right before generation, every item
of the pool gets the text of the article it links to (or of the item's own
page when the feed only carried a teaser), and the model writes from the
primary source instead of from a retelling.

Network only at generation time, only for the handful of items in a pool,
never in DRY_RUN.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Callable
from urllib.parse import urlparse

from .models import NormalizedItem
from .textutil import strip_html

log = logging.getLogger("pipeline.enrich")

# Hosts whose links carry no article: social networks, messengers, app stores.
SKIP_HOSTS = (
    "t.me", "telegram.me", "telegram.org", "max.ru", "instagram.com", "youtube.com", "youtu.be",
    "tiktok.com", "facebook.com", "fb.com", "wa.me", "whatsapp.com", "twitter.com", "x.com",
    "vk.com", "ok.ru", "apps.apple.com", "play.google.com", "linkedin.com", "threads.net",
    "snapchat.com", "pinterest.com", "goo.gl", "forms.gle", "docs.google.com",
)
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)
_BLOCK_RE = re.compile(r"<(script|style|noscript|svg|iframe|nav|footer|header|form)\b.*?</\1>", re.I | re.S)
_ARTICLE_RE = re.compile(r"<article\b.*?</article>", re.I | re.S)
_PARA_RE = re.compile(r"<(p|h1|h2|h3|li)\b[^>]*>(.*?)</\1>", re.I | re.S)
_META_DESC_RE = re.compile(r'<meta[^>]+(?:name|property)="(?:description|og:description)"[^>]+content="([^"]*)"', re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

MAX_ARTICLE_CHARS = 3500
MIN_PARAGRAPH_CHARS = 40
MAX_FETCHES = 6
MAX_BYTES = 1_500_000
TEASER_CHARS = 500          # an item this short is a teaser: fetch its own page
LABEL = "Статья по ссылке"

_CACHE: dict[str, str] = {}


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def linked_url(item: NormalizedItem) -> str | None:
    """The first external article link inside a channel post, if any."""
    text = f"{item.raw_text or ''}\n{item.summary or ''}"
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?»)")
        host = _host(url)
        if not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            continue
        return url
    return None


def own_page_url(item: NormalizedItem) -> str | None:
    """The item's own page when it is an article the feed only teased."""
    url = item.url or ""
    host = _host(url)
    if not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return None
    if len(item.summary or "") >= TEASER_CHARS:
        return None
    return url


def extract_article(payload: bytes | str) -> str:
    """Readable text of a news page: paragraphs and headings, scripts dropped."""
    page = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    page = _BLOCK_RE.sub(" ", page)
    scope = _ARTICLE_RE.search(page)
    body = scope.group(0) if scope else page
    paragraphs: list[str] = []
    seen: set[str] = set()
    for match in _PARA_RE.finditer(body):
        text = " ".join(html.unescape(strip_html(match.group(2))).split())
        if len(text) < MIN_PARAGRAPH_CHARS or text in seen:
            continue
        seen.add(text)
        paragraphs.append(text)
    if not paragraphs:
        meta = _META_DESC_RE.search(page)
        if meta:
            paragraphs.append(" ".join(html.unescape(meta.group(1)).split()))
    text = "\n".join(paragraphs).strip()
    return text[:MAX_ARTICLE_CHARS]


def page_title(payload: bytes | str) -> str:
    page = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    match = _TITLE_RE.search(page)
    return " ".join(html.unescape(strip_html(match.group(1))).split())[:160] if match else ""


def _fetch(url: str, http_get: Callable[..., tuple[int, bytes, str]], *, timeout: int, user_agent: str) -> str:
    if url in _CACHE:
        return _CACHE[url]
    text = ""
    try:
        status, payload, content_type = http_get(url, timeout=timeout, user_agent=user_agent)
        if status == 200 and payload and len(payload) <= MAX_BYTES and "html" in (content_type or "").lower():
            text = extract_article(payload)
    except Exception as exc:  # a dead link must never cost the post
        log.info("Статья по ссылке %s не прочитана: %s", url, exc)
    _CACHE[url] = text
    return text


def enrich_items(items: list[NormalizedItem], settings: Any, *, http_get: Callable[..., Any] | None = None,
                 max_fetches: int = MAX_FETCHES) -> int:
    """Append the linked (or own) article text to each item's summary in place.

    Returns the number of items enriched. No network in DRY_RUN.
    """
    if getattr(settings, "dry_run", False):
        return 0
    if http_get is None:
        from .collect import _http_get as http_get  # noqa: N813
    timeout = int(getattr(settings, "enrich_timeout_sec", 12) or 12)
    user_agent = "DubaiNewsBot/1.0 (+https://github.com/missisnew-oss/news)"
    done = 0
    fetches = 0
    for item in items:
        if LABEL in (item.summary or ""):
            continue  # already enriched (a rewrite of a snapshotted post)
        url = linked_url(item) or own_page_url(item)
        if not url or fetches >= max_fetches:
            continue
        fetches += 1
        text = _fetch(url, http_get, timeout=timeout, user_agent=user_agent)
        if len(text) < 200:
            continue
        item.summary = f"{(item.summary or '').rstrip()}\n\n{LABEL} ({url}):\n{text}"
        item.raw_text = (item.raw_text or "")[:4000]
        done += 1
        log.info("Материал %s дополнен статьёй по ссылке (%d симв.): %s", item.item_id, len(text), url)
    return done
