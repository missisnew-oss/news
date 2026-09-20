"""Text helpers: Telegram limits, HTML sanitising, hashing, truncation."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .config import TG_CAPTION_LIMIT, TG_MESSAGE_LIMIT

# Telegram accepts only this subset of HTML in messages.
# See https://core.telegram.org/bots/api#html-style — anything else makes
# sendMessage fail with "can't parse entities", which loses the post.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "blockquote", "tg-spoiler",
}

# Tag names may contain a hyphen (tg-spoiler), so the old [a-zA-Z0-9]+ was
# too narrow. Attributes must not contain angle brackets.
_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)/?>")
_HREF_RE = re.compile(r"""\bhref\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'<>]+))""", re.I)
_ENTITY_RE = re.compile(r"&(?!(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});)")
_SAFE_SCHEMES = ("http://", "https://", "tg://", "mailto:")
# Room reserved in every chunk for the tags reopened/closed by the splitter.
_TAG_RESERVE = 96
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


def escape_text(text: str) -> str:
    """Escape ``&``, ``<`` and ``>`` in a run of plain text.

    Telegram parses the message as HTML, so a bare ``&`` or ``<`` in ordinary
    prose ("Emaar & Nakheel", "цена < 1 млн") makes sendMessage fail with
    "can't parse entities" and the post is lost. Existing entities such as
    ``&amp;`` or ``&#1055;`` are left alone so nothing is double-escaped.
    """
    text = _ENTITY_RE.sub("&amp;", text or "")
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _clean_href(attrs: str) -> str:
    """Keep only a safe href on an <a> tag; drop every other attribute."""
    match = _HREF_RE.search(attrs or "")
    if not match:
        return ""
    href = (match.group(2) or match.group(3) or match.group(4) or "").strip()
    if not href.lower().startswith(_SAFE_SCHEMES):
        return ""
    return f' href="{escape_text(href)}"'


def _tokenize(text: str):
    """Yield ('text', raw) and ('tag', name, attrs, is_close) tokens."""
    pos = 0
    for match in _TAG_RE.finditer(text or ""):
        if match.start() > pos:
            yield ("text", text[pos:match.start()])
        name = match.group(1).lower()
        yield ("tag", name, match.group(2) or "", match.group(0).startswith("</"))
        pos = match.end()
    if pos < len(text or ""):
        yield ("text", text[pos:])


def _render_open(name: str, attrs: str) -> str:
    return f"<a{_clean_href(attrs)}>" if name == "a" else f"<{name}>"


def sanitize_telegram_html(text: str) -> str:
    """Make text safe to send with ``parse_mode=HTML``.

    Three jobs in one pass:
      * tags Telegram does not accept are dropped, their inner text kept;
      * ``&``, ``<`` and ``>`` in ordinary prose are escaped;
      * tags are balanced — a stray ``</b>`` is dropped and anything still
        open at the end is closed.

    This is also a prompt-injection guard: content coming out of the LLM
    (which itself saw untrusted RSS text) cannot smuggle markup into a post.
    """
    out: list[str] = []
    stack: list[str] = []
    dropped_links: list[int] = []
    for token in _tokenize(text or ""):
        if token[0] == "text":
            out.append(escape_text(token[1]))
            continue
        _, name, attrs, is_close = token
        if name not in ALLOWED_TAGS:
            continue  # drop the tag, keep whatever is inside it
        if name == "a" and not is_close and not _clean_href(attrs):
            # Telegram rejects <a> without a usable href, and a javascript:
            # or data: link has no business in a post. Keep the text only.
            dropped_links.append(len(out))
            continue
        if name == "a" and is_close and dropped_links:
            dropped_links.pop()
            continue
        if not is_close:
            stack.append(name)
            out.append(_render_open(name, attrs))
        elif name in stack:
            # Close everything opened after it, so nesting stays valid.
            while stack:
                open_name = stack.pop()
                out.append(f"</{open_name}>")
                if open_name == name:
                    break
        # a closer with no opener is simply dropped
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)


def _open_stack(chunk: str) -> list[tuple[str, str]]:
    """Tags left open at the end of an already-sanitised chunk."""
    stack: list[tuple[str, str]] = []
    for token in _tokenize(chunk):
        if token[0] != "tag":
            continue
        _, name, attrs, is_close = token
        if name not in ALLOWED_TAGS:
            continue
        if not is_close:
            stack.append((name, attrs))
        elif any(n == name for n, _ in stack):
            while stack:
                if stack.pop()[0] == name:
                    break
    return stack


def close_open_tags(chunk: str) -> tuple[str, str]:
    """Return (chunk with open tags closed, prefix that reopens them)."""
    stack = _open_stack(chunk)
    if not stack:
        return chunk, ""
    closing = "".join(f"</{name}>" for name, _ in reversed(stack))
    reopen = "".join(_render_open(name, attrs) for name, attrs in stack)
    return chunk + closing, reopen


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


def _tag_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _TAG_RE.finditer(text)]


def _inside_tag(spans: list[tuple[int, int]], idx: int) -> bool:
    return any(start < idx < end for start, end in spans)


def _safe_cut(text: str, limit: int) -> int:
    """Largest cut index <= limit that is not inside an HTML tag."""
    spans = _tag_spans(text)
    idx = min(limit, len(text))
    while idx > 0 and _inside_tag(spans, idx):
        idx = next(start for start, end in spans if start < idx < end)
    return idx


def split_for_telegram(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Split a long text into Telegram-sized chunks on paragraph boundaries.

    Falls back to line, then to hard character boundaries, so the function
    always terminates and never returns a chunk longer than ``limit``.

    Chunks are HTML-safe: a cut never lands inside a tag, and a tag left open
    by a cut is closed at the end of the chunk and reopened at the start of
    the next one. Without this Telegram rejects both halves of a split
    ``<b>…</b>`` with "can't parse entities".
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    budget = max(1, limit - _TAG_RESERVE)
    chunks: list[str] = []
    remainder = text
    carry = ""
    while len(remainder) > limit:
        hard = _safe_cut(remainder, budget)
        window = remainder[:hard]
        cut = 0
        for separator in ("\n\n", "\n", " "):
            idx = window.rfind(separator)
            if idx > budget // 3 and not _inside_tag(_tag_spans(remainder), idx):
                cut = idx
                skip = len(separator)
                break
        else:
            cut, skip = hard, 0
        if cut <= 0:
            cut, skip = hard, 0
        piece, reopen = close_open_tags(carry + remainder[:cut].rstrip())
        chunks.append(piece)
        carry = reopen
        remainder = remainder[cut + skip:].lstrip()
    if remainder or carry:
        chunks.append(carry + remainder)
    return [c for c in chunks if c]


def truncate_html(text: str, limit: int, ellipsis: str = "…") -> str:
    """Truncate without cutting inside a tag, closing whatever stays open."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = _safe_cut(text, max(0, limit - len(ellipsis) - _TAG_RESERVE))
    window = text[:cut]
    space = window.rfind(" ")
    if space > cut // 3 and not _inside_tag(_tag_spans(text), space):
        window = window[:space]
    window = window.rstrip(" ,.;:—-") + ellipsis
    return close_open_tags(window)[0]


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
    # Cut the caption at a tag-safe boundary and carry the remainder — and any
    # tag the cut left open — into the follow-up messages.
    cut = _safe_cut(text, TG_CAPTION_LIMIT - len("…") - _TAG_RESERVE)
    window = text[:cut]
    space = window.rfind(" ")
    if space > cut // 3 and not _inside_tag(_tag_spans(text), space):
        cut = space
    head, reopen = close_open_tags(text[:cut].rstrip(" ,.;:—-"))
    tail = (reopen + text[cut:].lstrip())
    return {
        "mode": "photo_plus_text",
        "caption": head + "…",
        "texts": split_for_telegram(tail, TG_MESSAGE_LIMIT),
    }
