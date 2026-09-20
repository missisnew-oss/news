"""Text helpers: Telegram limits, HTML sanitising, hashing, truncation."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT

# Telegram accepts only this subset of HTML in messages.
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "a", "code", "pre", "blockquote"}

_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)(\s[^>]*)?>")
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "igshid", "ref", "ref_src", "amp",
}


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """Strip tracking params, fragment and trailing slash; lowercase the host."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        urlencode(query),
        "",
    ))


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for fuzzy dedupe."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_hash(title: str, url: str) -> str:
    """Stable key for 'we have already seen this story'.

    Built from the canonical URL plus the normalised title, so the same story
    republished under a tracking-tagged URL collapses onto one key.
    """
    return sha1(f"{canonical_url(url)}|{normalize_title(title)}")


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_telegram_html(text: str) -> str:
    """Remove tags Telegram does not accept, keeping their inner text.

    This is also a prompt-injection guard: content coming out of the LLM (which
    itself saw untrusted RSS text) cannot smuggle arbitrary markup into a post.
    """
    def repl(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        return match.group(0) if tag in ALLOWED_TAGS else ""

    return _TAG_RE.sub(repl, text or "")


def visible_length(text: str) -> int:
    """Length as Telegram counts it: markup tags do not count toward the limit."""
    return len(strip_html(text))


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Cut on a word boundary so a post never ends mid-word."""
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - len(ellipsis))]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:—-") + ellipsis


def split_for_telegram(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Split a long text into Telegram-sized chunks on paragraph boundaries.

    Falls back to line, then to hard character boundaries, so the function
    always terminates and never returns a chunk longer than ``limit``.
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remainder = text
    while len(remainder) > limit:
        window = remainder[:limit]
        for separator in ("\n\n", "\n", " "):
            idx = window.rfind(separator)
            if idx > limit // 3:
                chunks.append(remainder[:idx].rstrip())
                remainder = remainder[idx + len(separator):].lstrip()
                break
        else:
            chunks.append(window)
            remainder = remainder[limit:]
    if remainder:
        chunks.append(remainder)
    return chunks


def fits_caption(text: str) -> bool:
    return len(text) <= TG_CAPTION_LIMIT


def plan_delivery(text: str, has_photo: bool) -> dict[str, object]:
    """Decide how a post is physically sent.

    * photo + text <= 1024      -> one photo message with a caption
    * photo + text  > 1024      -> photo with a short caption, then the rest
                                   as separate text messages
    * no photo                  -> text messages split at 4096
    """
    text = text or ""
    if not has_photo:
        parts = split_for_telegram(text, TG_MESSAGE_LIMIT)
        return {"mode": "text", "caption": None, "texts": parts}
    if len(text) <= TG_CAPTION_LIMIT:
        return {"mode": "photo", "caption": text, "texts": []}
    head = truncate(text, TG_CAPTION_LIMIT)
    tail = text[len(head.rstrip("…").rstrip()):].lstrip()
    return {
        "mode": "photo_plus_text",
        "caption": head,
        "texts": split_for_telegram(tail, TG_MESSAGE_LIMIT),
    }
