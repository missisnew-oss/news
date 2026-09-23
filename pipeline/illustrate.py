"""Stage 5 — ILLUSTRATE.

The post image is a *photo card*: a photograph covering the whole frame, a
soft dark gradient for legibility, the headline in bold capitals top-left,
the wordmark (or ``assets/logo.png``) top-right and one or two "pills" at the
bottom-left with the place and the rubric tag. The look follows what the owner
pointed at (Whitewill); ``style="editorial"`` gives the Ruble-like variant with
a centred serif headline.

Where the photograph comes from, in order of preference:

1. ``press``      — the source's own picture, only when the source is a
                    developer / official press channel (``license.reuse:
                    press_release``, not a ``role: signal`` broker channel);
2. ``generated``  — OpenAI ``gpt-image-1`` when ``OPENAI_API_KEY`` is set
                    (costs money; skipped for rubrics about a concrete
                    project, see docs/LEGAL.md);
3. ``unsplash`` / ``pexels`` — licensed stock by the model's ``stock_query``;
4. ``own_card``   — a branded gradient backdrop with the same layout.

In DRY_RUN nothing touches the network: the card is drawn over a synthetic
"sky, sea and towers" backdrop so the owner still sees a meaningful preview.
Author, licence and source URL are recorded for every image (docs/LEGAL.md).
"""

from __future__ import annotations

import base64
import logging
import random
from pathlib import Path
from typing import Any

from .config import ASSETS_DIR, ROOT, Settings
from .textutil import sha1, truncate

log = logging.getLogger("pipeline.illustrate")

# Legacy landscape size; the default card size now comes from brand.yml.
CARD_SIZE = (1200, 675)
CARD_SIZES = {"4:5": (1080, 1350), "16:9": (1200, 675)}
GENERATED_DIR = ASSETS_DIR / "generated"
CACHE_DIR = ASSETS_DIR / "cache"
FONTS_DIR = ASSETS_DIR / "fonts"
BRAND_FILE = ASSETS_DIR / "brand.yml"

PRESS_CATEGORIES = {"developers", "official_data", "city_gov"}
PRESS_MAX_BYTES = 5 * 1024 * 1024
PRESS_MIN_SIDE = 600
PRESS_TIMEOUT = 20

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
FONT_CANDIDATES_SERIF = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/Library/Fonts/Times New Roman.ttf",
    "C:/Windows/Fonts/times.ttf",
)

DEFAULT_BRAND: dict[str, Any] = {
    "name": "REALNEW MARY",
    "tagline": "REAL ESTATE · DUBAI",
    "logo_path": "assets/logo.png",
    "bg_from": "#0A1F35",
    "bg_to": "#05080F",
    "accent": "#F2B84B",
    "text": "#FFFFFF",
    "muted": "#D9E2EA",
    "pill_bg": "#0B1622",
    "pill_alpha": 150,
    "card": {
        "size": "4:5", "style": "photo", "margin": 64, "pill_radius": 30,
        "max_lines": 4, "shade_top": 150, "shade_bottom": 200,
    },
    "fonts": {"bold": "", "regular": "", "serif": ""},
    "tags": {},
    "tag_default": "#НОВОСТЬДНЯ",
    "places": {},
    "place_default": "ДУБАЙ",
    "ai_image": {
        "model": "gpt-image-1",
        "quality": "medium",
        "skip_rubrics": ["new_launch", "case_story"],
        "style_suffix": "bright photorealistic Dubai, golden hour, clean composition, "
                        "no text, no people close-up",
    },
}


# --------------------------------------------------------------------------
# Brand, fonts, text helpers
# --------------------------------------------------------------------------

def load_brand() -> dict[str, Any]:
    """brand.yml merged over the defaults (one level deep for the sub-dicts)."""
    brand: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_BRAND.items()}
    if not BRAND_FILE.exists():
        return brand
    try:
        import yaml

        data = yaml.safe_load(BRAND_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("brand.yml не прочитан (%s), используются цвета по умолчанию", exc)
        return brand
    if not isinstance(data, dict):
        return brand
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(brand.get(key), dict):
            brand[key].update(value)
        elif isinstance(value, (str, int, float, dict, list)):
            brand[key] = value
    return brand


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = (str(value) or "").lstrip("#")
    if len(value) != 6:
        return (11, 43, 64)
    return tuple(int(value[i: i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font_candidates(kind: str, brand: dict[str, Any] | None) -> list[str]:
    """brand.yml path first, then anything in assets/fonts/, then system fonts."""
    out: list[str] = []
    configured = ((brand or {}).get("fonts") or {}).get(kind) or ""
    if configured:
        out.append(str(ROOT / configured) if not Path(configured).is_absolute() else configured)
    if FONTS_DIR.exists():
        pattern = {"bold": "bold|black|heavy|extrabold", "regular": "regular|medium|book", "serif": "serif|display"}[kind]
        import re

        for path in sorted(FONTS_DIR.glob("*.[to]tf")):
            if re.search(pattern, path.stem, re.IGNORECASE):
                out.append(str(path))
    out.extend({"bold": FONT_CANDIDATES_BOLD, "regular": FONT_CANDIDATES_REGULAR, "serif": FONT_CANDIDATES_SERIF}[kind])
    return out


def _load_font(size: int, bold: bool = False, *, kind: str | None = None, brand: dict[str, Any] | None = None):
    from PIL import ImageFont

    kind = kind or ("bold" if bold else "regular")
    for path in _font_candidates(kind, brand):
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


def _split_hyphenated(word: str) -> list[str]:
    """«северо-восток» → ["северо-", "восток"]; a plain word stays whole."""
    if "-" not in word.strip("-"):
        return [word]
    parts = [p for p in word.split("-") if p]
    return [p + "-" for p in parts[:-1]] + parts[-1:]


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in (text or "").split():
        if draw.textlength(word, font=font) > max_width:
            # Flush what we have, then split the oversized word itself:
            # «экспериментально-технологический» breaks at its own hyphen
            # first, and only then letter by letter.
            if current:
                lines.append(current)
                current = ""
            pieces: list[str] = []
            for part in _split_hyphenated(word):
                pieces.extend(_break_long_word(draw, part, font, max_width))
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


def _fit_headline(draw, text: str, box_width: int, sizes: tuple[int, ...], max_lines: int,
                  *, kind: str = "bold", brand: dict[str, Any] | None = None):
    """Largest size at which the text fits in ``max_lines`` lines of ``box_width``.

    Returns ``(font, lines)``. When even the smallest size does not fit, the
    text is cut to ``max_lines`` lines with an ellipsis rather than overflow.
    """
    font, lines = None, []
    for size in sizes:
        font = _load_font(size, kind=kind, brand=brand)
        lines = _wrap(draw, text, font, box_width)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= max_lines and widest <= box_width:
            return font, lines
    lines = lines[:max_lines]
    if lines:
        last = lines[-1].rstrip("-")
        while last and draw.textlength(last + "…", font=font) > box_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return font, lines


def _tracked_width(draw, text: str, font, tracking: float) -> float:
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def _draw_tracked(draw, xy: tuple[float, float], text: str, font, fill, tracking: float) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


# --------------------------------------------------------------------------
# Backgrounds
# --------------------------------------------------------------------------

def _cover(image, size: tuple[int, int]):
    """Scale and centre-crop ``image`` so it fills ``size`` (CSS object-fit: cover)."""
    from PIL import ImageOps

    return ImageOps.fit(image.convert("RGB"), size, method=3, centering=(0.5, 0.45))


def _vertical_shade(base, *, top: int, bottom: int, top_span: float = 0.5, bottom_span: float = 0.42):
    """Darken the top and the bottom of an RGBA image with soft gradients."""
    from PIL import Image

    width, height = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ramp = Image.linear_gradient("L")  # 256×256, black at the top → white at the bottom
    if top > 0:
        span = max(1, int(height * top_span))
        mask = ramp.transpose(Image.FLIP_TOP_BOTTOM).point(lambda v: v * top // 255).resize((width, span))
        overlay.paste((0, 0, 0, 255), (0, 0, width, span), mask)
    if bottom > 0:
        span = max(1, int(height * bottom_span))
        mask = ramp.point(lambda v: v * bottom // 255).resize((width, span))
        overlay.paste((0, 0, 0, 255), (0, height - span, width, height), mask)
    return Image.alpha_composite(base, overlay)


def render_synthetic_photo(size: tuple[int, int] = CARD_SIZES["4:5"], seed: str = ""):
    """A drawn "sky – sea – towers" backdrop for DRY_RUN previews and tests.

    No network, no licence questions: pure Pillow gradients and shapes that
    read as a golden-hour Dubai skyline from a distance.
    """
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    rng = random.Random(seed or "dubai")
    horizon = int(height * 0.60)

    # Sky: deep blue → warm gold at the horizon; sea: teal → deep navy.
    column = Image.new("RGB", (1, height))
    stops = [
        (0, (14, 42, 78)), (int(height * 0.30), (52, 96, 150)), (int(height * 0.50), (200, 150, 110)),
        (horizon, (247, 205, 140)), (horizon + 1, (60, 140, 165)), (height - 1, (8, 34, 58)),
    ]
    for (y0, c0), (y1, c1) in zip(stops, stops[1:]):
        for y in range(y0, y1 + 1):
            t = (y - y0) / max(1, y1 - y0)
            column.putpixel((0, min(y, height - 1)), tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)))
    base = column.resize(size).convert("RGBA")

    # Sun and its reflection.
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    sx, sy, r = int(width * 0.66), int(horizon - height * 0.04), int(width * 0.11)
    gdraw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 232, 170, 230))
    gdraw.rectangle([sx - r // 2, horizon, sx + r // 2, height], fill=(255, 220, 150, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(width * 0.05))
    base = Image.alpha_composite(base, glow)

    # Skyline silhouettes with a few lit windows, mirrored faintly in the sea.
    towers = Image.new("RGBA", size, (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(towers)
    x = -rng.randint(10, 40)
    while x < width:
        w = rng.randint(int(width * 0.03), int(width * 0.09))
        h = rng.randint(int(height * 0.06), int(height * 0.28))
        shade = rng.randint(10, 26)
        tdraw.rectangle([x, horizon - h, x + w, horizon], fill=(shade, shade + 14, shade + 30, 255))
        for _ in range(rng.randint(2, 14)):
            wx, wy = rng.randint(x + 4, max(x + 4, x + w - 8)), rng.randint(horizon - h + 6, horizon - 8)
            tdraw.rectangle([wx, wy, wx + 3, wy + 5], fill=(255, 225, 160, rng.randint(120, 220)))
        x += w + rng.randint(4, 24)
    spire_x, spire_h = int(width * 0.42), int(height * 0.42)
    tdraw.polygon([(spire_x - 22, horizon), (spire_x + 22, horizon), (spire_x + 4, horizon - spire_h),
                   (spire_x - 4, horizon - spire_h)], fill=(12, 24, 40, 255))
    base = Image.alpha_composite(base, towers)
    reflection = towers.crop((0, horizon - int(height * 0.3), width, horizon)).transpose(Image.FLIP_TOP_BOTTOM)
    reflection = reflection.filter(ImageFilter.GaussianBlur(6))
    reflection.putalpha(reflection.getchannel("A").point(lambda v: v * 70 // 255))
    base.alpha_composite(reflection, (0, horizon))
    return base.convert("RGB")


def _brand_backdrop(size: tuple[int, int], brand: dict[str, Any]):
    """Gradient backdrop for the no-photo fallback: navy → black with a warm glow."""
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    top, bottom = _hex_to_rgb(brand["bg_from"]), _hex_to_rgb(brand["bg_to"])
    column = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        column.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    base = column.resize(size).convert("RGBA")
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    accent = _hex_to_rgb(brand["accent"])
    r = int(width * 0.55)
    cx, cy = int(width * 0.95), int(height * 0.85)
    gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=accent + (70,))
    r2 = int(width * 0.35)
    gdraw.ellipse([-r2 // 2, -r2 // 2, r2, r2], fill=(90, 140, 190, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(width * 0.12))
    base = Image.alpha_composite(base, glow)
    # A thin diagonal accent line gives the flat gradient some structure.
    line = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(line).line([(0, int(height * 0.78)), (width, int(height * 0.55))], fill=accent + (90,), width=3)
    return Image.alpha_composite(base, line).convert("RGB")


# --------------------------------------------------------------------------
# Card rendering
# --------------------------------------------------------------------------

def _card_size(brand: dict[str, Any]) -> tuple[int, int]:
    return CARD_SIZES.get(str((brand.get("card") or {}).get("size", "4:5")), CARD_SIZES["4:5"])


def _rounded(draw, box, radius: int, fill) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _draw_logo(image, draw, brand: dict[str, Any], *, margin: int, scale: float, centered: bool) -> tuple[int, int]:
    """Logo PNG if present, else the wordmark. Returns (width, height) used."""
    from PIL import Image

    logo_rel = str(brand.get("logo_path") or "")
    logo_path = Path(logo_rel) if Path(logo_rel).is_absolute() else ROOT / logo_rel
    width = image.size[0]
    if logo_rel and logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            target_h = int(64 * scale)
            logo = logo.resize((max(1, int(logo.size[0] * target_h / logo.size[1])), target_h), Image.LANCZOS)
            x = (width - logo.size[0]) // 2 if centered else width - margin - logo.size[0]
            image.alpha_composite(logo, (x, margin))
            return logo.size
        except Exception as exc:
            log.warning("Логотип %s не прочитан (%s), рисуем надпись", logo_path, exc)
    name = str(brand.get("name") or "").upper()
    tagline = str(brand.get("tagline") or "").upper()
    kind = "serif" if centered else "bold"
    font = _load_font(int(30 * scale), kind=kind, brand=brand)
    tracking = 5 * scale
    name_w = _tracked_width(draw, name, font, tracking)
    x = (width - name_w) / 2 if centered else width - margin - name_w
    text_rgb = _hex_to_rgb(brand["text"])
    _draw_tracked(draw, (x, margin), name, font, text_rgb, tracking)
    used_h = int(34 * scale)
    if tagline:
        small = _load_font(int(13 * scale), kind="regular", brand=brand)
        small_tracking = 3 * scale
        small_w = _tracked_width(draw, tagline, small, small_tracking)
        sx = (width - small_w) / 2 if centered else width - margin - small_w
        _draw_tracked(draw, (sx, margin + int(36 * scale)), tagline, small, _hex_to_rgb(brand["muted"]), small_tracking)
        used_h = int(54 * scale)
    return int(name_w), used_h


def _draw_pills(draw, labels: list[str], *, x: int, y: int, brand: dict[str, Any], scale: float,
                centered_width: int | None = None, outline: bool = False, dots: bool = True) -> int:
    """Row of pills starting at (x, y); returns the row height."""
    labels = [label for label in labels if label]
    if not labels:
        return 0
    font = _load_font(int(24 * scale), kind="bold", brand=brand)
    pad_x, pill_h, gap = int(24 * scale), int(56 * scale), int(14 * scale)
    radius = int((brand.get("card") or {}).get("pill_radius", 30) * scale)
    fill = _hex_to_rgb(brand.get("pill_bg", "#0B1622")) + (int(brand.get("pill_alpha", 150)),)
    accent = _hex_to_rgb(brand["accent"])
    text_rgb = _hex_to_rgb(brand["text"])
    tracking = 1.5 * scale
    widths, dot_widths = [], []
    for i, label in enumerate(labels):
        dot = int(14 * scale) if dots and i == 0 and not label.startswith("#") else 0
        dot_widths.append(dot)
        widths.append(int(_tracked_width(draw, label, font, tracking)) + pad_x * 2 + dot)
    if centered_width is not None:
        x = (centered_width - (sum(widths) + gap * (len(labels) - 1))) // 2
    for label, w, dot in zip(labels, widths, dot_widths):
        if outline:
            draw.rounded_rectangle([x, y, x + w, y + pill_h], radius=radius, outline=text_rgb, width=max(1, int(2 * scale)))
        else:
            _rounded(draw, [x, y, x + w, y + pill_h], radius, fill)
        tx = x + pad_x
        if dot:
            r = int(5 * scale)
            cy = y + pill_h // 2
            draw.ellipse([tx, cy - r, tx + 2 * r, cy + r], fill=accent)
            tx += dot
        ty = y + (pill_h - font.size) // 2 - int(3 * scale)
        _draw_tracked(draw, (tx, ty), label, font, text_rgb, tracking)
        x += w + gap
    return pill_h


def _open_photo(photo):
    from PIL import Image

    if isinstance(photo, Image.Image):
        return photo
    return Image.open(photo)


def render_photo_card(
    photo,
    headline: str,
    *,
    tag_left: str = "",
    tag_right: str = "",
    subtitle: str = "",
    accent: str = "",
    brand: dict[str, Any] | None = None,
    size: tuple[int, int] | None = None,
    style: str | None = None,
    out_path: Path | None = None,
) -> Path:
    """Compose the photo card and return the PNG path.

    ``photo`` is a path or a PIL image; it is cover-cropped to ``size``.
    ``style`` is ``"photo"`` (bold grotesque top-left, Whitewill-like) or
    ``"editorial"`` (centred serif at the bottom, Ruble-like).
    """
    from PIL import Image, ImageDraw, ImageEnhance

    brand = brand or load_brand()
    card_cfg = brand.get("card") or {}
    size = size or _card_size(brand)
    style = (style or card_cfg.get("style") or "photo").lower()
    width, height = size
    scale = width / 1080
    margin = int(int(card_cfg.get("margin", 64)) * scale)
    max_lines = int(card_cfg.get("max_lines", 4))

    base = _cover(_open_photo(photo), size)
    base = ImageEnhance.Color(base).enhance(1.12)
    base = ImageEnhance.Contrast(base).enhance(1.05)
    base = base.convert("RGBA")

    text_rgb = _hex_to_rgb(brand["text"])
    accent_rgb = _hex_to_rgb(brand["accent"])
    muted_rgb = _hex_to_rgb(brand["muted"])
    headline = " ".join((headline or "").split()).upper()

    if style == "editorial":
        base = _vertical_shade(base, top=110, bottom=235, top_span=0.35, bottom_span=0.6)
        draw = ImageDraw.Draw(base)
        _draw_logo(base, draw, brand, margin=margin, scale=scale, centered=True)
        box_width = width - margin * 2
        sizes = tuple(int(s * scale) for s in (66, 60, 54, 48, 44, 40, 36))
        font, lines = _fit_headline(draw, headline, box_width, sizes, max_lines, kind="serif", brand=brand)
        line_h = int(font.size * 1.18)
        bottom_cursor = height - margin
        labels = brand.get("tag_labels") or {}
        pills = [str(labels.get(tag_right) or tag_right.lstrip("#") or tag_left)]
        pill_h = int(56 * scale) if pills[0] else 0
        subtitle_font = _load_font(int(22 * scale), kind="regular", brand=brand)
        sub_lines = _wrap(draw, subtitle.upper(), subtitle_font, box_width)[:2] if subtitle else []
        sub_h = len(sub_lines) * int(subtitle_font.size * 1.4)
        block_h = len(lines) * line_h + (int(20 * scale) + sub_h if sub_lines else 0) + (int(26 * scale) + pill_h if pill_h else 0)
        y = bottom_cursor - block_h
        for line in lines:
            w = _tracked_width(draw, line, font, 2 * scale)
            _draw_tracked(draw, ((width - w) / 2 + 2, y + 3), line, font, (0, 0, 0, 110), 2 * scale)
            _draw_tracked(draw, ((width - w) / 2, y), line, font, text_rgb, 2 * scale)
            y += line_h
        if sub_lines:
            y += int(20 * scale)
            for line in sub_lines:
                w = _tracked_width(draw, line, subtitle_font, 2 * scale)
                _draw_tracked(draw, ((width - w) / 2, y), line, subtitle_font, muted_rgb, 2 * scale)
                y += int(subtitle_font.size * 1.4)
        if pill_h:
            y += int(26 * scale)
            _draw_pills(draw, pills, x=0, y=y, brand=brand, scale=scale, centered_width=width, outline=True, dots=False)
    else:
        base = _vertical_shade(
            base, top=int(card_cfg.get("shade_top", 150)), bottom=int(card_cfg.get("shade_bottom", 200)),
        )
        draw = ImageDraw.Draw(base)
        logo_w, logo_h = _draw_logo(base, draw, brand, margin=margin, scale=scale, centered=False)
        portrait = height > width
        if portrait:
            box_width = width - margin * 2
            y = margin + logo_h + int(28 * scale)
        else:
            box_width = width - margin * 2 - logo_w - int(40 * scale)
            y = margin - int(4 * scale)
        sizes = tuple(int(s * scale) for s in (84, 76, 68, 62, 56, 50, 44, 40))
        font, lines = _fit_headline(draw, headline, box_width, sizes, max_lines, kind="bold", brand=brand)
        line_h = int(font.size * 1.14)
        shadow = (0, 0, 0, 120)
        for line in lines:
            draw.text((margin + 2, y + 3), line, font=font, fill=shadow)
            draw.text((margin, y), line, font=font, fill=text_rgb)
            y += line_h
        if accent:
            y += int(14 * scale)
            accent_font = _load_font(int(font.size * 1.5), kind="bold", brand=brand)
            draw.text((margin + 2, y + 3), accent[:18], font=accent_font, fill=shadow)
            draw.text((margin, y), accent[:18], font=accent_font, fill=accent_rgb)

        pill_y = height - margin - int(56 * scale)
        _draw_pills(draw, [tag_left, tag_right], x=margin, y=pill_y, brand=brand, scale=scale)
        if subtitle:
            sub_font = _load_font(int(26 * scale), kind="regular", brand=brand)
            sub_lines = _wrap(draw, subtitle, sub_font, width - margin * 2)[:2]
            sub_lh = int(sub_font.size * 1.35)
            sy = pill_y - int(22 * scale) - sub_lh * len(sub_lines)
            for line in sub_lines:
                draw.text((margin + 1, sy + 2), line, font=sub_font, fill=(0, 0, 0, 110))
                draw.text((margin, sy), line, font=sub_font, fill=muted_rgb)
                sy += sub_lh

    out_path = out_path or GENERATED_DIR / f"photo-{sha1(headline + accent + tag_left + tag_right + style)[:12]}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path


def render_card(headline: str, accent: str = "", *, subtitle: str = "", out_path: Path | None = None,
                tag_left: str = "", tag_right: str = "") -> Path:
    """Fallback card without a photograph: the same layout over a brand gradient."""
    brand = load_brand()
    backdrop = _brand_backdrop(_card_size(brand), brand)
    out_path = out_path or GENERATED_DIR / f"card-{sha1(headline + accent)[:12]}.png"
    return render_photo_card(
        backdrop, headline, accent=accent, subtitle=subtitle, brand=brand, style="photo",
        tag_left=tag_left or brand.get("place_default", ""), tag_right=tag_right, out_path=out_path,
    )


# --------------------------------------------------------------------------
# Photo providers
# --------------------------------------------------------------------------

def fetch_stock(settings: Settings, query: str, *, orientation: str = "landscape") -> dict[str, Any] | None:
    """Download a licensed stock photo. Returns image metadata or None."""
    import requests

    query = (query or "dubai skyline").strip()
    if settings.unsplash_access_key:
        try:
            response = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1, "orientation": orientation},
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
                params={"query": query, "per_page": 1, "orientation": orientation},
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


def _valid_photo(path: Path, min_side: int = PRESS_MIN_SIDE) -> bool:
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            return min(img.size) >= min_side
    except Exception:
        return False


def download_press_photo(url: str) -> Path | None:
    """Fetch a source's own picture into assets/cache/ with size and format checks."""
    import requests

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"press-{sha1(url)[:12]}.img"
    if path.exists() and _valid_photo(path):
        return path
    try:
        with requests.get(url, timeout=PRESS_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > PRESS_MAX_BYTES:
                log.info("Пресс-фото %s больше 5 МБ, пропускаем", url)
                return None
            chunks, total = [], 0
            for chunk in response.iter_content(64 * 1024):
                total += len(chunk)
                if total > PRESS_MAX_BYTES:
                    log.info("Пресс-фото %s больше 5 МБ, пропускаем", url)
                    return None
                chunks.append(chunk)
    except Exception as exc:
        log.warning("Пресс-фото не скачалось (%s): %s", url, exc)
        return None
    path.write_bytes(b"".join(chunks))
    if not _valid_photo(path):
        log.info("Пресс-фото %s не картинка или меньше %d px, пропускаем", url, PRESS_MIN_SIDE)
        path.unlink(missing_ok=True)
        return None
    return path


_SOURCES_CACHE: dict[str, dict[str, Any]] | None = None


def _source_registry(sources_doc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    global _SOURCES_CACHE
    if sources_doc is not None:
        return {s["id"]: s for s in (sources_doc.get("sources") or []) if s.get("id")}
    if _SOURCES_CACHE is None:
        try:
            from .config import load_sources

            _SOURCES_CACHE = {s["id"]: s for s in (load_sources().get("sources") or []) if s.get("id")}
        except Exception as exc:
            log.warning("Реестр источников не прочитан (%s) — пресс-фото не используются", exc)
            _SOURCES_CACHE = {}
    return _SOURCES_CACHE


def press_photo_allowed(source: dict[str, Any] | None) -> bool:
    """Only a developer's / official press channel may lend us its picture.

    Broker Telegram channels (``role: signal``) and news media
    (``reuse: summary_with_link``) are somebody else's content — never.
    """
    if not source:
        return False
    if str(source.get("role") or "news") == "signal":
        return False
    if (source.get("license") or {}).get("reuse") != "press_release":
        return False
    return source.get("category") in PRESS_CATEGORIES


def find_press_photo(draft, sources_doc: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The first eligible source picture among the draft's items, downloaded."""
    registry = _source_registry(sources_doc)
    cited = {s.get("source_id") for s in (getattr(draft, "sources", None) or [])}
    items = list(getattr(draft, "source_items", None) or [])
    items.sort(key=lambda row: 0 if row.get("source_id") in cited else 1)
    for row in items:
        url = row.get("image_url")
        source = registry.get(str(row.get("source_id") or ""))
        if not url or not press_photo_allowed(source):
            continue
        path = download_press_photo(str(url))
        if not path:
            continue
        return {
            "provider": "press",
            "path": str(path),
            "license": "пресс-материал источника (license.reuse: press_release)",
            "author": source.get("title", row.get("source_id", "")),
            "author_url": source.get("homepage") or source.get("url") or "",
            "source_url": str(url),
            "remote_url": str(url),
            "article_url": row.get("url") or "",
            "source_id": row.get("source_id"),
        }
    return None


def build_image_prompt(stock_query: str, brand: dict[str, Any] | None = None) -> str:
    brand = brand or load_brand()
    suffix = str((brand.get("ai_image") or {}).get("style_suffix") or DEFAULT_BRAND["ai_image"]["style_suffix"])
    query = " ".join((stock_query or "Dubai skyline waterfront").split())
    return f"{query}, {suffix}"


def generate_image(settings: Settings, prompt: str, *, size: str = "1024x1536") -> bytes | None:
    """OpenAI Images (gpt-image-1) → PNG bytes, or None on any failure.

    Kept as one small function so tests and future providers can replace it.
    """
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("Пакет openai не установлен — генерация картинок недоступна (requirements.txt)")
        return None
    ai_cfg = load_brand().get("ai_image") or {}
    try:
        client = OpenAI(api_key=settings.openai_api_key, timeout=120, max_retries=1)
        result = client.images.generate(
            model=str(ai_cfg.get("model") or "gpt-image-1"),
            prompt=prompt,
            size=size,
            n=1,
            quality=str(ai_cfg.get("quality") or "medium"),
        )
        payload = result.data[0].b64_json if result.data else None
        return base64.b64decode(payload) if payload else None
    except Exception as exc:
        log.warning("Генерация картинки не удалась: %s", exc)
        return None


# --------------------------------------------------------------------------
# Stage entry points
# --------------------------------------------------------------------------

def card_tags(draft, brand: dict[str, Any] | None = None) -> tuple[str, str]:
    """(place pill, rubric tag pill) for a draft, from brand.yml."""
    brand = brand or load_brand()
    haystack = " ".join(str(getattr(draft, key, "") or "") for key in ("title", "body")).lower()
    place = str(brand.get("place_default") or "")
    for name, keywords in (brand.get("places") or {}).items():
        if any(str(kw).lower() in haystack for kw in (keywords or [])):
            place = str(name)
            break
    tags = brand.get("tags") or {}
    tag = tags.get(getattr(draft, "rubric", ""), brand.get("tag_default", ""))
    return place, str(tag or "")


def _own_meta(headline: str, accent: str, place: str, tag: str, *, background: str, note: str = "") -> dict[str, Any]:
    meta = {
        "provider": "own_card",
        "license": "собственная графика канала",
        "author": "канал",
        "source_url": "",
        "headline": headline,
        "accent": accent,
        "tag_left": place,
        "tag_right": tag,
        "background": background,
    }
    if note:
        meta["note"] = note
    return meta


def _photo_for_draft(settings: Settings, draft, brand: dict[str, Any], sources_doc: dict[str, Any] | None):
    """Walk the provider chain. Returns (photo path or None, meta)."""
    spec = getattr(draft, "image", None) or {}
    size = _card_size(brand)
    portrait = size[1] > size[0]

    if settings.dry_run:
        log.info("DRY_RUN: фото не запрашиваем, фон рисуем синтетический")
        return None, {}

    press = find_press_photo(draft, sources_doc)
    if press:
        log.info("Картинка: пресс-фото источника %s", press.get("source_id"))
        return press.pop("path"), press

    ai_cfg = brand.get("ai_image") or {}
    if settings.openai_api_key:
        if getattr(draft, "rubric", "") in set(ai_cfg.get("skip_rubrics") or []):
            log.info("Картинка: рубрика %s про конкретный объект — ИИ-генерацию пропускаем", draft.rubric)
        else:
            prompt = build_image_prompt(spec.get("stock_query", ""), brand)
            data = generate_image(settings, prompt, size="1024x1536" if portrait else "1536x1024")
            if data:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path = CACHE_DIR / f"gen-{sha1(prompt)[:12]}.png"
                path.write_bytes(data)
                if _valid_photo(path, min_side=256):
                    log.info("Картинка: сгенерирована (%s)", ai_cfg.get("model", "gpt-image-1"))
                    return str(path), {
                        "provider": "generated",
                        "license": "ИИ-иллюстрация (OpenAI gpt-image-1), не фото реального объекта",
                        "author": "канал / OpenAI gpt-image-1",
                        "source_url": "",
                        "prompt": prompt,
                        "note": "иллюстрация",
                    }

    stock = fetch_stock(settings, spec.get("stock_query", ""), orientation="portrait" if portrait else "landscape")
    if stock:
        log.info("Картинка: сток %s (%s)", stock.get("provider"), stock.get("author"))
        return stock.pop("path"), stock
    return None, {}


def illustrate(settings: Settings, draft, sources_doc: dict[str, Any] | None = None) -> tuple[str | None, dict[str, Any]]:
    """Produce the post image: photo card over the best available photograph."""
    brand = load_brand()
    spec = getattr(draft, "image", None) or {}
    headline = truncate((spec.get("headline") or draft.title or "").strip(), 64, "…")
    accent = (spec.get("accent") or "").strip()
    subtitle = (draft.sources[0].get("title", "") if draft.sources else "")[:80]
    place, tag = card_tags(draft, brand)

    try:
        photo, meta = _photo_for_draft(settings, draft, brand, sources_doc)
    except Exception as exc:  # a provider bug must never kill the run
        log.warning("Поиск фото упал (%s), рисуем карточку без фото", exc)
        photo, meta = None, {}

    if photo is None and settings.dry_run:
        photo = render_synthetic_photo(_card_size(brand), seed=headline)
        meta = _own_meta(headline, accent, place, tag, background="synthetic")

    if photo is not None:
        try:
            path = render_photo_card(photo, headline, accent=accent, subtitle=subtitle,
                                     tag_left=place, tag_right=tag, brand=brand)
            meta.update({"headline": headline, "accent": accent, "tag_left": place, "tag_right": tag,
                         "card": "photo", "card_path": str(path)})
            if isinstance(photo, (str, Path)):
                meta["background_path"] = str(photo)
            return str(path), meta
        except Exception as exc:
            log.warning("Фото-карточка не отрисовалась (%s), рисуем градиентную", exc)

    try:
        path = render_card(headline, accent, subtitle=subtitle, tag_left=place, tag_right=tag)
        log.info("Картинка: собственная градиентная карточка")
        return str(path), _own_meta(headline, accent, place, tag, background="gradient")
    except Exception as exc:
        log.error("Изображение не создано: %s", exc)
        return None, {"provider": "none", "error": str(exc)}


def ensure_image(settings: Settings, post: dict[str, Any]) -> str | None:
    """Path to the post's image, re-creating it when the file is gone.

    Every Actions run starts from a clean checkout and assets/generated/ is
    not committed, so a card drawn in the generate run does not exist in the
    approve or publish run. Stock and press photos are downloaded again by
    their recorded URL; a generated picture is *not* generated again (it
    costs money) — the post falls back to the gradient card, with a note.
    """
    path = post.get("image_path")
    if path and Path(path).exists():
        return str(path)
    meta = post.get("image_meta") or {}
    if not path and not meta.get("headline"):
        return None  # the post was deliberately text-only; keep it that way
    headline = truncate((meta.get("headline") or post.get("title") or "").strip(), 64, "…")
    if not headline:
        return None
    accent = (meta.get("accent") or "").strip()
    brand = load_brand()
    place = meta.get("tag_left") or brand.get("place_default", "")
    tag = meta.get("tag_right") or (brand.get("tags") or {}).get(post.get("rubric", ""), brand.get("tag_default", ""))
    provider = meta.get("provider")

    photo = None
    if provider in {"press", "unsplash", "pexels"} and not settings.dry_run:
        url = meta.get("remote_url") or meta.get("source_url")
        if url:
            photo = download_press_photo(url) if provider == "press" else (
                (_download(url, {}) or {}).get("path")
            )
    elif provider == "own_card" and meta.get("background") == "synthetic":
        photo = render_synthetic_photo(_card_size(brand), seed=headline)

    new_path = None
    if photo is not None:
        try:
            new_path = render_photo_card(photo, headline, accent=accent, tag_left=place, tag_right=tag, brand=brand)
        except Exception as exc:
            log.warning("Фото-карточку для %s перерисовать не удалось: %s", post.get("post_id"), exc)
    if new_path is None:
        try:
            new_path = render_card(headline, accent, tag_left=place, tag_right=tag)
        except Exception as exc:
            log.warning("Картинку для %s перерисовать не удалось: %s", post.get("post_id"), exc)
            return None
        if provider != "own_card":
            note = (
                "сгенерированная картинка утеряна между запусками; повторно не генерируем (платно), заменена карточкой"
                if provider == "generated"
                else f"исходная картинка ({provider}) утеряна между запусками, заменена карточкой"
            )
            post["image_meta"] = _own_meta(headline, accent, place, tag, background="gradient", note=note)
    post["image_path"] = str(new_path)
    log.info("Картинка для %s перерисована: %s", post.get("post_id"), new_path)
    return str(new_path)
