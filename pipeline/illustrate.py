"""Stage 5 — ILLUSTRATE.

Primary path: a branded card rendered with Pillow (gradient background, logo
placeholder, wrapped headline, optional accent figure). It costs nothing, is
always licence-clean and keeps the channel visually consistent.

Fallback: a stock photo from Unsplash or Pexels, chosen by the keywords the
model produced. Author, licence and source URL are recorded for every image
(docs/LEGAL.md).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import ASSETS_DIR, Settings
from .textutil import sha1, truncate

log = logging.getLogger("pipeline.illustrate")

# Telegram renders 1200x675 (16:9) cleanly in both feed and preview.
CARD_SIZE = (1200, 675)
GENERATED_DIR = ASSETS_DIR / "generated"
BRAND_FILE = ASSETS_DIR / "brand.yml"

FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
FONT_CANDIDATES_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)

DEFAULT_BRAND = {
    "name": "DUBAI PROPERTY",
    "bg_from": "#0B2B40",
    "bg_to": "#155E75",
    "accent": "#F2B84B",
    "text": "#FFFFFF",
    "muted": "#BFD4DE",
}


def load_brand() -> dict[str, Any]:
    if not BRAND_FILE.exists():
        return dict(DEFAULT_BRAND)
    try:
        import yaml

        data = yaml.safe_load(BRAND_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return dict(DEFAULT_BRAND)
    brand = dict(DEFAULT_BRAND)
    if isinstance(data, dict):
        brand.update({k: v for k, v in data.items() if isinstance(v, str)})
    return brand


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = (value or "").lstrip("#")
    if len(value) != 6:
        return (11, 43, 64)
    return tuple(int(value[i: i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    log.warning("Не найден TrueType-шрифт, используется встроенный растровый")
    return ImageFont.load_default()


def _break_long_word(draw, word: str, font, max_width: int) -> list[str]:
    """Hyphenate a word that cannot fit on a line by itself.

    Russian produces words like «Достопримечательности» that are wider than
    the whole text box at the largest heading size. Without this they used to
    be emitted as a single line and ran off the right edge of the card.
    """
    parts: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if current and draw.textlength(candidate + "-", font=font) > max_width:
            parts.append(current + "-")
            current = char
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or [word]


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in (text or "").split():
        if draw.textlength(word, font=font) > max_width:
            # Flush what we have, then split the oversized word itself.
            if current:
                lines.append(current)
                current = ""
            pieces = _break_long_word(draw, word, font, max_width)
            lines.extend(pieces[:-1])
            current = pieces[-1]
            continue
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_card(headline: str, accent: str = "", *, subtitle: str = "", out_path: Path | None = None) -> Path:
    """Render the branded card and return its path."""
    from PIL import Image, ImageDraw

    brand = load_brand()
    width, height = CARD_SIZE
    top = _hex_to_rgb(brand["bg_from"])
    bottom = _hex_to_rgb(brand["bg_to"])

    image = Image.new("RGB", CARD_SIZE, top)
    draw = ImageDraw.Draw(image)
    for y in range(height):  # vertical gradient
        ratio = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)),
        )

    accent_rgb = _hex_to_rgb(brand["accent"])
    text_rgb = _hex_to_rgb(brand["text"])
    muted_rgb = _hex_to_rgb(brand["muted"])

    margin = 72
    # Logo placeholder: accent square + brand name.
    draw.rectangle([margin, margin, margin + 56, margin + 56], fill=accent_rgb)
    draw.text((margin + 76, margin + 12), brand["name"], font=_load_font(30, bold=True), fill=text_rgb)

    # Headline, wrapped, shrinking until it fits the available box.
    box_width = width - margin * 2
    # Shrink until the headline fits in four lines AND no line is wider than
    # the box (a single long word can overflow at any size).
    for size in (74, 66, 58, 50, 44, 38):
        font = _load_font(size, bold=True)
        lines = _wrap(draw, headline, font, box_width)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= 4 and widest <= box_width:
            break
    line_height = int(size * 1.22)
    block_height = line_height * len(lines)
    accent_height = 96 if accent else 0
    y = max(margin + 110, (height - block_height - accent_height) // 2)
    for line in lines:
        draw.text((margin, y), line, font=font, fill=text_rgb)
        y += line_height

    if accent:
        y += 18
        accent_font = _load_font(84, bold=True)
        draw.text((margin, y), accent[:18], font=accent_font, fill=accent_rgb)

    if subtitle:
        draw.text((margin, height - margin - 30), subtitle[:90], font=_load_font(26), fill=muted_rgb)

    draw.rectangle([0, height - 10, width, height], fill=accent_rgb)

    out_path = out_path or GENERATED_DIR / f"card-{sha1(headline + accent)[:12]}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, "PNG", optimize=True)
    return out_path


def fetch_stock(settings: Settings, query: str) -> dict[str, Any] | None:
    """Download a licensed stock photo. Returns image metadata or None."""
    import requests

    query = (query or "dubai skyline").strip()
    if settings.unsplash_access_key:
        try:
            response = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {settings.unsplash_access_key}"},
                timeout=20,
            )
            response.raise_for_status()
            results = response.json().get("results") or []
            if results:
                photo = results[0]
                return _download(
                    photo["urls"]["regular"],
                    {
                        "provider": "unsplash",
                        "author": (photo.get("user") or {}).get("name", ""),
                        "author_url": ((photo.get("user") or {}).get("links") or {}).get("html", ""),
                        "source_url": (photo.get("links") or {}).get("html", ""),
                        "license": "Unsplash License",
                        "query": query,
                    },
                )
        except Exception as exc:
            log.warning("Unsplash недоступен: %s", exc)

    if settings.pexels_api_key:
        try:
            response = requests.get(
                "https://api.pexels.com/v1/search",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": settings.pexels_api_key},
                timeout=20,
            )
            response.raise_for_status()
            photos = response.json().get("photos") or []
            if photos:
                photo = photos[0]
                return _download(
                    photo["src"]["large"],
                    {
                        "provider": "pexels",
                        "author": photo.get("photographer", ""),
                        "author_url": photo.get("photographer_url", ""),
                        "source_url": photo.get("url", ""),
                        "license": "Pexels License",
                        "query": query,
                    },
                )
        except Exception as exc:
            log.warning("Pexels недоступен: %s", exc)
    return None


def _download(url: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    import requests

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        log.warning("Не удалось скачать стоковое фото: %s", exc)
        return None
    path = GENERATED_DIR / f"stock-{sha1(url)[:12]}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    meta["path"] = str(path)
    meta["remote_url"] = url
    return meta


def illustrate(settings: Settings, draft) -> tuple[str | None, dict[str, Any]]:
    """Produce an image for a draft. Card first, stock only as a fallback."""
    image_spec = draft.image or {}
    headline = truncate((image_spec.get("headline") or draft.title or "").strip(), 64, "…")
    accent = (image_spec.get("accent") or "").strip()
    subtitle = (draft.sources[0].get("title", "") if draft.sources else "")[:80]

    if image_spec.get("mode", "card") == "card":
        try:
            path = render_card(headline, accent, subtitle=subtitle)
            return str(path), {
                "provider": "own_card",
                "license": "собственная графика канала",
                "author": "канал",
                "source_url": "",
                "headline": headline,
                "accent": accent,
            }
        except Exception as exc:
            log.warning("Не удалось отрисовать карточку (%s), пробуем сток", exc)

    if settings.dry_run:
        log.info("DRY_RUN: стоки не запрашиваются")
    else:
        stock = fetch_stock(settings, image_spec.get("stock_query", ""))
        if stock:
            return stock.pop("path"), stock

    try:
        path = render_card(headline, accent, subtitle=subtitle)
        return str(path), {
            "provider": "own_card",
            "license": "собственная графика канала",
            "author": "канал",
            "source_url": "",
            "headline": headline,
            "accent": accent,
        }
    except Exception as exc:
        log.error("Изображение не создано: %s", exc)
        return None, {"provider": "none", "error": str(exc)}
