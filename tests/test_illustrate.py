"""ILLUSTRATE: photo card rendering and the photo provider chain."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pipeline import illustrate
from pipeline.config import Settings
from pipeline.models import PostDraft

SIZE = (1080, 1350)
LONG_HEADLINE = (
    "Достопримечательности Джумейры: экспериментально-технологический "
    "комплекс откроют к 2027 году"
)  # 93 characters, one word wider than the whole text box


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(illustrate, "GENERATED_DIR", tmp_path / "generated")
    monkeypatch.setattr(illustrate, "CACHE_DIR", tmp_path / "cache")
    illustrate._SOURCES_CACHE = None
    yield
    illustrate._SOURCES_CACHE = None


@pytest.fixture
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("ILLUSTRATE попытался выйти в сеть")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _draft(rubric: str = "market_pulse", *, source_id: str = "demo_official", image_url: str | None = None,
           title: str = "Сделки в Дубае выросли на 12% за квартал") -> PostDraft:
    return PostDraft(
        post_id="p1", rubric=rubric, title=title, body="Текст поста про рынок.",
        image={"mode": "card", "headline": title, "accent": "+12%", "stock_query": "dubai skyline"},
        sources=[{"source_id": source_id, "title": "Demo source", "url": "https://example.com/a"}],
        source_items=[{
            "item_id": "i1", "source_id": source_id, "category": "official_data", "title": title,
            "summary": "", "url": "https://example.com/a", "image_url": image_url,
        }],
    )


def _sources_doc(role: str | None = None, reuse: str = "press_release", category: str = "official_data") -> dict:
    src = {
        "id": "demo_official", "title": "Demo Press Office", "url": "https://example.com", "type": "html",
        "category": category, "lang": "en", "license": {"reuse": reuse, "note": ""},
    }
    if role:
        src["role"] = role
    return {"sources": [src]}


def _fake_photo(path: Path, size=(1200, 1600), color=(30, 90, 140)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_photo_card_renders_from_synthetic_photo(tmp_path):
    photo = illustrate.render_synthetic_photo(SIZE, seed="test")
    out = illustrate.render_photo_card(
        photo, LONG_HEADLINE, tag_left="ДУБАЙ", tag_right="#НОВОСТЬДНЯ",
        subtitle="Dubai Media Office", accent="+12%", out_path=tmp_path / "card.png",
    )
    assert out.exists()
    with Image.open(out) as img:
        assert img.size == SIZE
        assert img.mode == "RGB"


@pytest.mark.parametrize("size", [(1080, 1350), (1200, 675)])
def test_headline_stays_inside_the_frame(tmp_path, size):
    """Text drawn on a black photo: no bright pixel may land in the side margins."""
    black = Image.new("RGB", size, (0, 0, 0))
    brand = illustrate.load_brand()
    brand["logo_path"] = ""
    brand["tagline"] = ""
    out = illustrate.render_photo_card(
        black, LONG_HEADLINE, tag_left="ДУБАЙ", tag_right="#НОВОСТЬДНЯ", accent="+12%",
        brand=brand, size=size, out_path=tmp_path / "bbox.png",
    )
    with Image.open(out) as img:
        width, height = img.size
        margin = int(64 * width / 1080)
        gray = img.convert("L")
        # Right edge strip beyond the margin: nothing bright (headline lines end before it).
        strip = gray.crop((width - margin + 6, 0, width, height))
        assert strip.getextrema()[1] < 60, "заголовок или пилюли вылезли за правое поле"
        left = gray.crop((0, 0, margin - 6, height))
        assert left.getextrema()[1] < 60, "что-то нарисовано в левом поле"
        # The headline actually got drawn.
        assert gray.crop((margin, margin, width - margin, height // 2)).getextrema()[1] > 200


def test_fit_headline_keeps_lines_within_box_and_limit():
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for headline in (LONG_HEADLINE, "Частнопредпринимательский", "Короткий заголовок"):
        font, lines = illustrate._fit_headline(
            draw, headline.upper(), 952, (84, 76, 68, 62, 56, 50, 44, 40), 4,
        )
        assert 1 <= len(lines) <= 4
        assert max(draw.textlength(line, font=font) for line in lines) <= 952


def test_hyphenated_word_breaks_at_its_own_hyphen():
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = illustrate._load_font(60, bold=True)
    lines = illustrate._wrap(draw, "ЭКСПЕРИМЕНТАЛЬНО-ТЕХНОЛОГИЧЕСКИЙ КОМПЛЕКС", font, 900)
    assert lines[0] == "ЭКСПЕРИМЕНТАЛЬНО-"


def test_editorial_style_and_logo_png(tmp_path, monkeypatch):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (400, 120), (255, 255, 255, 255)).save(logo)
    brand = illustrate.load_brand()
    brand["logo_path"] = str(logo)
    photo = illustrate.render_synthetic_photo(SIZE, seed="ed")
    out = illustrate.render_photo_card(
        photo, "Официальное открытие музея в Абу-Даби", tag_right="#НОВОСТЬДНЯ",
        subtitle="Билеты уже в продаже", brand=brand, style="editorial", out_path=tmp_path / "ed.png",
    )
    with Image.open(out) as img:
        # The white logo block is composited centred at the top.
        assert img.getpixel((SIZE[0] // 2, 64 + 30))[:3] == (255, 255, 255)


def test_card_tags_pick_place_and_rubric_tag():
    draft = _draft(rubric="new_launch", title="Новый проект на острове Саадият в Абу-Даби")
    place, tag = illustrate.card_tags(draft)
    assert place == "АБУ-ДАБИ"
    assert tag == "#ЛОТНЕДЕЛИ"
    place, tag = illustrate.card_tags(_draft(rubric="events_afisha"))
    assert (place, tag) == ("ДУБАЙ", "#СОБЫТИЯ")


def test_render_card_fallback_still_works(tmp_path):
    out = illustrate.render_card("Заголовок", "12%", subtitle="Источник", out_path=tmp_path / "c.png")
    assert out.exists()


# --------------------------------------------------------------------------
# Provider chain
# --------------------------------------------------------------------------

def _live_settings(**kw) -> Settings:
    return Settings(dry_run=False, **kw)


def test_press_photo_is_preferred(monkeypatch, tmp_path):
    photo = _fake_photo(tmp_path / "press.jpg")
    calls = []
    monkeypatch.setattr(illustrate, "download_press_photo", lambda url: calls.append(url) or photo)
    monkeypatch.setattr(illustrate, "generate_image", lambda *a, **k: pytest.fail("генерация не нужна"))
    monkeypatch.setattr(illustrate, "fetch_stock", lambda *a, **k: pytest.fail("сток не нужен"))
    draft = _draft(image_url="https://example.com/press.jpg")
    path, meta = illustrate.illustrate(_live_settings(openai_api_key="sk-test"), draft, sources_doc=_sources_doc())
    assert meta["provider"] == "press"
    assert calls == ["https://example.com/press.jpg"]
    assert meta["source_url"] == "https://example.com/press.jpg"
    assert meta["remote_url"] == "https://example.com/press.jpg"
    assert meta["author"] == "Demo Press Office"
    assert Path(path).exists() and meta["card"] == "photo"
    assert meta["tag_left"] == "ДУБАЙ" and meta["tag_right"] == "#НОВОСТЬДНЯ"


@pytest.mark.parametrize("doc", [
    _sources_doc(role="signal"),                       # broker's Telegram channel
    _sources_doc(reuse="summary_with_link"),           # news media
    _sources_doc(category="lifestyle"),                # not a developer / official source
])
def test_foreign_channel_photos_are_never_used(monkeypatch, doc):
    monkeypatch.setattr(illustrate, "download_press_photo", lambda url: pytest.fail("чужое фото скачано"))
    monkeypatch.setattr(illustrate, "generate_image", lambda *a, **k: None)
    monkeypatch.setattr(illustrate, "fetch_stock", lambda *a, **k: None)
    draft = _draft(image_url="https://t.me/broker/photo.jpg")
    _, meta = illustrate.illustrate(_live_settings(), draft, sources_doc=doc)
    assert meta["provider"] == "own_card"


def test_generated_when_no_press_photo(monkeypatch):
    png = Image.new("RGB", (1024, 1536), (200, 160, 90))
    import io

    buf = io.BytesIO()
    png.save(buf, "PNG")
    prompts = []

    def fake_generate(settings, prompt, *, size="1024x1536"):
        prompts.append((prompt, size))
        return buf.getvalue()

    monkeypatch.setattr(illustrate, "generate_image", fake_generate)
    monkeypatch.setattr(illustrate, "fetch_stock", lambda *a, **k: pytest.fail("сток не нужен"))
    draft = _draft()  # no image_url
    path, meta = illustrate.illustrate(_live_settings(openai_api_key="sk-test"), draft, sources_doc=_sources_doc())
    assert meta["provider"] == "generated"
    assert prompts and prompts[0][1] == "1024x1536"
    assert "dubai skyline" in prompts[0][0] and "no text" in prompts[0][0]
    assert meta["note"] == "иллюстрация"
    assert Path(path).exists()


def test_generation_skipped_for_concrete_project_rubrics(monkeypatch):
    monkeypatch.setattr(illustrate, "generate_image", lambda *a, **k: pytest.fail("ИИ для лонча запрещён"))
    monkeypatch.setattr(illustrate, "fetch_stock", lambda *a, **k: None)
    _, meta = illustrate.illustrate(_live_settings(openai_api_key="sk-test"), _draft(rubric="new_launch"),
                                    sources_doc=_sources_doc())
    assert meta["provider"] == "own_card"


def test_stock_when_no_openai_key(monkeypatch, tmp_path):
    photo = _fake_photo(tmp_path / "stock.jpg")

    def fake_stock(settings, query, *, orientation="landscape"):
        assert orientation == "portrait"
        return {"provider": "unsplash", "author": "Ann", "license": "Unsplash License",
                "source_url": "https://unsplash.com/photos/x", "remote_url": "https://images.unsplash.com/x",
                "path": str(photo), "query": query}

    monkeypatch.setattr(illustrate, "generate_image", lambda *a, **k: pytest.fail("без ключа генерации нет"))
    monkeypatch.setattr(illustrate, "fetch_stock", fake_stock)
    path, meta = illustrate.illustrate(_live_settings(unsplash_access_key="u"), _draft(), sources_doc=_sources_doc())
    assert meta["provider"] == "unsplash"
    assert meta["author"] == "Ann" and meta["license"] == "Unsplash License"
    assert meta["background_path"] == str(photo)
    assert Path(path).exists() and path != str(photo)


def test_own_card_when_nothing_else(monkeypatch):
    monkeypatch.setattr(illustrate, "generate_image", lambda *a, **k: None)
    monkeypatch.setattr(illustrate, "fetch_stock", lambda *a, **k: None)
    path, meta = illustrate.illustrate(_live_settings(), _draft(), sources_doc=_sources_doc())
    assert meta["provider"] == "own_card" and meta["background"] == "gradient"
    assert Path(path).exists()


def test_press_download_rejects_small_and_big_files(monkeypatch, tmp_path):
    small = _fake_photo(tmp_path / "small.jpg", size=(400, 300))
    assert illustrate._valid_photo(small) is False
    big = _fake_photo(tmp_path / "big.jpg", size=(800, 800))
    assert illustrate._valid_photo(big) is True

    class _Resp:
        headers = {"Content-Length": str(6 * 1024 * 1024)}

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"x"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert illustrate.download_press_photo("https://example.com/huge.jpg") is None


# --------------------------------------------------------------------------
# ensure_image between runs
# --------------------------------------------------------------------------

def test_ensure_image_does_not_regenerate_ai_pictures(monkeypatch, tmp_path):
    monkeypatch.setattr(illustrate, "generate_image", lambda *a, **k: pytest.fail("повторная генерация — деньги"))
    post = {
        "post_id": "p1", "rubric": "market_pulse", "title": "Заголовок",
        "image_path": str(tmp_path / "gone" / "photo.png"),
        "image_meta": {"provider": "generated", "headline": "Заголовок", "accent": "+5%",
                       "tag_left": "ДУБАЙ", "tag_right": "#НОВОСТЬДНЯ", "prompt": "x"},
    }
    path = illustrate.ensure_image(_live_settings(openai_api_key="sk-test"), post)
    assert path and Path(path).exists()
    assert post["image_meta"]["provider"] == "own_card"
    assert "повторно не генерируем" in post["image_meta"]["note"]


def test_ensure_image_redownloads_stock_by_url(monkeypatch, tmp_path):
    photo = _fake_photo(tmp_path / "again.jpg")
    urls = []

    def fake_download(url, meta):
        urls.append(url)
        meta["path"] = str(photo)
        return meta

    monkeypatch.setattr(illustrate, "_download", fake_download)
    post = {
        "post_id": "p2", "title": "Заголовок",
        "image_path": str(tmp_path / "gone.png"),
        "image_meta": {"provider": "pexels", "headline": "Заголовок", "remote_url": "https://images.pexels.com/x",
                       "source_url": "https://www.pexels.com/photo/x", "tag_left": "ДУБАЙ", "tag_right": "#СОБЫТИЯ"},
    }
    path = illustrate.ensure_image(_live_settings(), post)
    assert urls == ["https://images.pexels.com/x"]
    assert path and Path(path).exists()
    assert post["image_meta"]["provider"] == "pexels"  # licence record kept


def test_ensure_image_redraws_synthetic_card_in_dry_run(no_network, tmp_path):
    post = {
        "post_id": "p3", "title": "Заголовок",
        "image_path": str(tmp_path / "gone.png"),
        "image_meta": {"provider": "own_card", "background": "synthetic", "headline": "Заголовок", "accent": ""},
    }
    path = illustrate.ensure_image(Settings(dry_run=True), post)
    assert path and Path(path).exists()


# --------------------------------------------------------------------------
# DRY_RUN: no sockets
# --------------------------------------------------------------------------

def test_dry_run_draws_photo_card_without_network(no_network, monkeypatch):
    monkeypatch.setattr(illustrate, "download_press_photo", lambda url: pytest.fail("сеть в DRY_RUN"))
    draft = _draft(image_url="https://example.com/press.jpg")
    settings = Settings(dry_run=True, openai_api_key="sk-test", unsplash_access_key="u", pexels_api_key="p")
    path, meta = illustrate.illustrate(settings, draft, sources_doc=_sources_doc())
    assert meta["provider"] == "own_card" and meta["background"] == "synthetic"
    assert meta["card"] == "photo"
    assert Path(path).exists()
    with Image.open(path) as img:
        assert img.size == SIZE


def test_place_pill_has_accent_dot(tmp_path):
    """The gold dot before the place label (regression: it vanished once)."""
    black = Image.new("RGB", SIZE, (0, 0, 0))
    brand = illustrate.load_brand()
    out = illustrate.render_photo_card(black, "Заголовок", tag_left="ДУБАЙ", tag_right="#СОБЫТИЯ",
                                       brand=brand, out_path=tmp_path / "dot.png")
    accent = illustrate._hex_to_rgb(brand["accent"])
    with Image.open(out) as img:
        margin, pill_h = 64, 56
        region = img.crop((margin, SIZE[1] - margin - pill_h, margin + 80, SIZE[1] - margin))
        assert accent in {px[:3] for px in region.getdata()}, "нет золотой точки в пилюле места"


# --------------------------------------------------------------------------
# QA round 2
# --------------------------------------------------------------------------

def test_oversized_pictures_are_refused_before_decoding(tmp_path, monkeypatch):
    """A downloaded picture is untrusted: a decompression bomb must be refused
    from the header, not decoded into gigabytes."""
    monkeypatch.setattr(illustrate, "PHOTO_MAX_PIXELS", 1_000_000)
    big = _fake_photo(tmp_path / "bomb.jpg", size=(1200, 1200))
    assert illustrate._valid_photo(big) is False
    ok = _fake_photo(tmp_path / "ok.jpg", size=(900, 900))
    assert illustrate._valid_photo(ok) is True


def test_stock_download_is_bounded_and_checked(monkeypatch, tmp_path):
    class _Resp:
        def __init__(self, chunks, declared=None):
            self.chunks = chunks
            self.headers = {"Content-Length": str(declared)} if declared else {}

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield from self.chunks

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    import requests

    monkeypatch.setattr(illustrate, "PRESS_MAX_BYTES", 4096)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([b"x" * 1024] * 8))
    assert illustrate._download("https://images.example.com/huge.jpg", {}) is None, "поток больше лимита"
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([b"x"], declared=10_000))
    assert illustrate._download("https://images.example.com/declared.jpg", {}) is None
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([b"<html>not an image</html>"]))
    assert illustrate._download("https://images.example.com/page.jpg", {}) is None, "HTML вместо картинки"
    assert not list((tmp_path / "generated").glob("*")), "мусор не остаётся на диске"

    import io

    buf = io.BytesIO()
    Image.new("RGB", (400, 400), (1, 2, 3)).save(buf, "JPEG")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([buf.getvalue()]))
    meta = illustrate._download("https://images.example.com/real.jpg", {})
    assert meta and Path(meta["path"]).exists() and meta["remote_url"].endswith("real.jpg")


def test_generated_picture_is_reused_for_the_same_prompt(monkeypatch):
    """A rewrite in the same run (or a re-run on the same runner) must not
    pay OpenAI for the same prompt twice."""
    import io

    buf = io.BytesIO()
    Image.new("RGB", (1024, 1536), (200, 160, 90)).save(buf, "PNG")
    calls = []

    def fake_generate(settings, prompt, *, size="1024x1536"):
        calls.append(prompt)
        return buf.getvalue()

    monkeypatch.setattr(illustrate, "generate_image", fake_generate)
    monkeypatch.setattr(illustrate, "fetch_stock", lambda *a, **k: pytest.fail("сток не нужен"))
    settings = _live_settings(openai_api_key="sk-test")
    _, meta1 = illustrate.illustrate(settings, _draft(), sources_doc=_sources_doc())
    _, meta2 = illustrate.illustrate(settings, _draft(title="Другой заголовок, тот же запрос"), sources_doc=_sources_doc())
    assert meta1["provider"] == meta2["provider"] == "generated"
    assert len(calls) == 1, "второй вызов gpt-image за тот же промпт"


def test_ensure_image_falls_back_to_the_card_when_the_photo_cannot_be_fetched(monkeypatch):
    """Clean runner, no network (or a 404): the post still gets an image."""
    monkeypatch.setattr(illustrate, "_fetch_bounded", lambda *a, **k: (_ for _ in ()).throw(OSError("нет сети")))
    for provider in ("press", "unsplash", "pexels"):
        post = {
            "post_id": "p9", "rubric": "market_pulse", "title": "Заголовок",
            "image_path": "/nonexistent/photo.png",
            "image_meta": {"provider": provider, "remote_url": "https://images.example.com/x.jpg",
                           "headline": "Заголовок"},
        }
        path = illustrate.ensure_image(_live_settings(), post)
        assert path and Path(path).exists()
        assert post["image_meta"]["provider"] == "own_card"
        assert provider in post["image_meta"]["note"]


def test_press_photo_lookup_survives_posts_snapshotted_before_image_url_existed():
    draft = _draft()
    draft.source_items = [{"item_id": "i1", "source_id": "demo_official", "url": "https://example.com/a"}]
    assert illustrate.find_press_photo(draft, _sources_doc()) is None
    draft.source_items = None
    assert illustrate.find_press_photo(draft, _sources_doc()) is None
    draft.sources = None
    assert illustrate.find_press_photo(draft, _sources_doc()) is None


def test_card_renders_yo_quotes_and_dash_without_tofu(tmp_path):
    """DejaVu/Liberation cover Cyrillic incl. «ё», guillemets and the em dash:
    the glyphs must differ from the .notdef box the fallback font would draw."""
    from PIL import ImageFont

    font = illustrate._load_font(48, kind="bold")
    assert not isinstance(font, ImageFont.ImageFont), "нужен TrueType-шрифт, не растровый"
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    widths = {ch: draw.textlength(ch, font=font) for ch in "ё«»—Ж"}
    assert all(w > 0 for w in widths.values())
    assert len({round(w) for w in widths.values()}) > 2, "все символы одной ширины — похоже на квадраты"
    black = Image.new("RGB", (1200, 675), (0, 0, 0))
    out = illustrate.render_photo_card(
        black, "Ёлка на набережной — «Дубай Крик» 90+ символов заголовка для проверки переноса слов",
        tag_left="ДУБАЙ", tag_right="#АФИША", size=(1200, 675), out_path=tmp_path / "yo.png",
    )
    with Image.open(out) as img:
        gray = img.convert("L")
        assert gray.crop((1200 - 50, 0, 1200, 675)).getextrema()[1] < 60
