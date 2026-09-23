"""Owner inbox: understand what the owner forwards to the bot, draft a post.

``pipeline/approve.py::_store_owner_material`` keeps everything the owner
sends the bot in private — screenshots of Instagram posts and stories, reels,
developer PDFs (price lists, floor plans, payment plans), forwarded WhatsApp
and Telegram posts, her own notes — in ``state/inbox.json``. This module is
what turns that pile into work:

1. **understand** — download the file through Bot API and extract what it
   says: a photo goes to Claude vision (text transcribed verbatim + a short
   description + a kind), a PDF through ``pypdf`` (scans: the embedded page
   images go to vision), a video through ffmpeg + OpenAI Whisper when an
   ``OPENAI_API_KEY`` is set. The result is written back to the inbox entry
   as ``extracted`` and the entry gets ``processed_at``.
2. **draft** — every understood entry becomes a pseudo NormalizedItem
   (source ``owner_inbox``) and goes through the ordinary
   ``generate.generate_for_rubric`` with the ``from_owner`` prompt, so the
   fact-check gate and the tone rules apply exactly as for collected news.
   The draft is queued, the entry gets ``drafted_at`` and ``post_id``; a
   material the model declines gets ``skipped_reason`` and is never retried.

Both steps are idempotent: processed entries are not re-read, drafted or
skipped entries are not re-generated, so the stage returns immediately when
there is nothing new — it runs after every approve poll.

Files land in ``assets/cache/inbox/`` (git-ignored). The owner's own photo is
reused as the post image by its Telegram ``file_id``, so the approve and
publish runs do not depend on the local file.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import illustrate, postqueue, state
from .config import ASSETS_DIR, Settings
from .generate import generate_for_rubric
from .llm import LLMError, extract_json, get_provider
from .models import NormalizedItem, PostDraft
from .telegram import TelegramClient, TelegramError
from .textutil import sha1

log = logging.getLogger("pipeline.inbox")

RUBRIC = "from_owner"
SOURCE_ID = "owner_inbox"
STATE_FILE = "inbox.json"
CACHE_DIR = ASSETS_DIR / "cache" / "inbox"

# Bot API getFile refuses anything larger; the owner has to send a smaller file.
MAX_FILE_BYTES = 20 * 1024 * 1024
# state/inbox.json is committed on every run: keep the extracted text bounded.
MAX_TEXT_CHARS = 6000
MAX_DESCRIPTION_CHARS = 1000
# Below this much text a PDF is treated as a scan and its page images are read.
PDF_MIN_TEXT_CHARS = 200
PDF_TEXT_PAGES = 12
PDF_SCAN_PAGES = 3
# Anthropic accepts images up to 5 MB; larger ones are re-encoded with Pillow.
IMAGE_MAX_BYTES = 4_500_000
IMAGE_MAX_SIDE = 2000
# Whisper accepts uploads up to 25 MB; mono 48 kbps mp3 keeps an hour under that.
AUDIO_MAX_BYTES = 25 * 1024 * 1024
WHISPER_MODEL = "whisper-1"
# A transient LLM failure must not throw the material away; a second run
# (the next approve poll) gets one more try before the entry is parked.
MAX_DRAFT_ATTEMPTS = 2

VISION_KINDS = ("screenshot", "floorplan", "pricelist", "photo", "other")
# Which of the owner's pictures may be the post image. A screenshot of
# somebody else's Instagram post is not ours to republish (docs/LEGAL.md):
# such posts get the branded card; a floor plan or a photo she took is fine.
OWNER_PHOTO_KINDS = {"floorplan", "pricelist", "photo", "other"}

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}
VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".webm", ".avi", ".mkv", ".3gp"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".aac", ".opus"}

VISION_INSTRUCTION = (
    "Перед тобой изображение, которое владелица Telegram-канала о недвижимости "
    "и жизни в Дубае переслала своему боту.\n\n"
    "1. Перепиши ВЕСЬ текст с изображения дословно, в том порядке, в каком он "
    "читается: заголовки, подписи, цифры, цены, площади, даты, названия проектов "
    "и застройщиков, условия оплаты. Ничего не додумывай и не исправляй. "
    "Нечитаемые места помечай словом [неразборчиво].\n"
    "2. Кратко, в 1–3 предложениях, опиши, что на изображении: объект "
    "недвижимости, планировка, таблица цен или план оплаты, сторис с текстом, "
    "скриншот поста или переписки, фотография города.\n"
    "3. Определи тип: screenshot (скриншот чужого поста, сторис, рилса или "
    "переписки), floorplan (планировка), pricelist (прайс, таблица цен, план "
    "оплаты), photo (фотография объекта или города), other.\n\n"
    "Ответь строго одним JSON-объектом без markdown-обёртки и без текста вокруг:\n"
    '{"text": "весь текст с изображения", "description": "что изображено", '
    '"kind": "screenshot|floorplan|pricelist|photo|other"}'
)


class FileTooLarge(RuntimeError):
    """Bot API cannot hand out files over 20 MB."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim(text: Any, limit: int) -> str:
    return str(text or "").strip()[:limit]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

# The cache file name is a hash of the file_id plus an extension; the
# extension comes from Telegram's ``file_path`` / the document name and is
# kept only when it looks like one — nothing from the outside may add a
# path component.
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


def cache_path(file_id: str, suffix: str = "") -> Path:
    suffix = suffix.lower()
    if not _SUFFIX_RE.match(suffix):
        suffix = ""
    return CACHE_DIR / f"{sha1(file_id)[:16]}{suffix}"


def download_file(client: TelegramClient, file_id: str, *, name: str = "") -> Path:
    """``getFile`` + download into ``assets/cache/inbox/``. Cached by file_id."""
    try:
        info = client.get_file(file_id)
    except TelegramError as exc:
        if "too big" in str(exc).lower() or "too large" in str(exc).lower():
            raise FileTooLarge(str(exc)) from exc
        raise
    size = int(info.get("file_size") or 0)
    if size > MAX_FILE_BYTES:
        raise FileTooLarge(f"{size} байт > {MAX_FILE_BYTES}")
    file_path = str(info.get("file_path") or "")
    suffix = Path(file_path).suffix or Path(name).suffix
    dest = cache_path(file_id, suffix)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        # The client streams with its own 20 MB cap (TelegramClient.MAX_DOWNLOAD_BYTES).
        client.download_file(file_path, dest)
    except TelegramError as exc:
        if "лимита" in str(exc):
            raise FileTooLarge(str(exc)) from exc
        raise
    log.info("Скачан файл из копилки: %s (%d байт)", dest.name, dest.stat().st_size)
    return dest


def _classify(entry: dict[str, Any]) -> tuple[str, str | None, str, str]:
    """(kind, file_id, name, media_type) for an inbox entry."""
    if entry.get("photo_file_id"):
        return "image", entry["photo_file_id"], "photo.jpg", "image/jpeg"
    document = entry.get("document") or {}
    file_id = document.get("file_id")
    if not file_id:
        return "text", None, "", ""
    name = str(document.get("name") or "")
    mime = str(document.get("mime") or "").lower()
    suffix = Path(name).suffix.lower()
    if mime.startswith("image/") or suffix in IMAGE_MEDIA_TYPES:
        return "image", file_id, name, IMAGE_MEDIA_TYPES.get(suffix, mime or "image/jpeg")
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf", file_id, name, "application/pdf"
    if mime.startswith(("video/", "audio/")) or suffix in VIDEO_SUFFIXES | AUDIO_SUFFIXES:
        return "video", file_id, name, mime
    return "unsupported", file_id, name, mime


# ---------------------------------------------------------------------------
# Understanding: image / pdf / video
# ---------------------------------------------------------------------------

def sniff_media_type(data: bytes) -> str:
    """Image media type from the magic bytes, '' when it is not an image we
    know. The extension Telegram reports is not trusted: a PNG saved as
    «.jpg» sent with ``image/jpeg`` is rejected by the vision API."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _prepare_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Bytes and media type the vision API will accept.

    The type comes from the content, not the extension. The bytes are kept
    as they are when they are a known format, under the size cap and no
    longer than ``IMAGE_MAX_SIDE`` on the long edge (the API rejects images
    over 8000 px and downsizes to ~1600 anyway); otherwise the image is
    re-encoded to a bounded JPEG.
    """
    import io

    sniffed = sniff_media_type(data)
    media_type = sniffed or media_type
    needs_reencode = len(data) > IMAGE_MAX_BYTES or not sniffed
    if not needs_reencode:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                needs_reencode = max(image.size) > IMAGE_MAX_SIDE
        except Exception:
            needs_reencode = True
    if not needs_reencode:
        return data, media_type
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=85, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        log.warning("Картинку не удалось перекодировать (%s), отправляю как есть", exc)
        return data, media_type or "image/jpeg"


def _normalize_vision(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise LLMError("vision: ответ модели не является объектом")
    kind = str(result.get("kind") or "other").strip().lower()
    return {
        "text": _trim(result.get("text"), MAX_TEXT_CHARS),
        "description": _trim(result.get("description"), MAX_DESCRIPTION_CHARS),
        "kind": kind if kind in VISION_KINDS else "other",
    }


def extract_image(provider: Any, path: Path, media_type: str) -> dict[str, Any]:
    data = path.read_bytes()
    if not data:
        return {"kind": "image", "method": "vision", "error": "empty_file"}
    payload, media_type = _prepare_image(data, media_type)
    result = _normalize_vision(provider.describe_image(payload, media_type, VISION_INSTRUCTION))
    result["method"] = "vision"
    return result


def extract_pdf(provider: Any, path: Path) -> dict[str, Any]:
    """Native text through pypdf; a scan falls back to vision on the page images
    (pypdf hands them out without poppler or any other native dependency)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = list(reader.pages)
    chunks = []
    for page in pages[:PDF_TEXT_PAGES]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception as exc:  # one broken page must not lose the document
            log.warning("pypdf: страница не прочитана: %s", exc)
    text = "\n".join(chunks).strip()
    description = f"PDF, страниц: {len(pages)}"
    if len(text) >= PDF_MIN_TEXT_CHARS:
        return {"text": text[:MAX_TEXT_CHARS], "description": description,
                "kind": "pdf", "method": "pypdf"}

    seen_text, seen_desc, kind = [text] if text else [], [], "pdf"
    for number, page in enumerate(pages[:PDF_SCAN_PAGES], 1):
        try:
            images = list(page.images)
        except Exception as exc:
            log.warning("pypdf: картинки страницы %d не прочитаны: %s", number, exc)
            continue
        if not images:
            continue
        biggest = max(images, key=lambda im: len(im.data or b""))
        media_type = IMAGE_MEDIA_TYPES.get(Path(str(biggest.name)).suffix.lower(), "image/png")
        payload, media_type = _prepare_image(biggest.data, media_type)
        vision = _normalize_vision(provider.describe_image(payload, media_type, VISION_INSTRUCTION))
        if vision["text"]:
            seen_text.append(f"[страница {number}]\n{vision['text']}")
        if vision["description"]:
            seen_desc.append(vision["description"])
        if vision["kind"] != "other":
            kind = vision["kind"]
    if len(seen_text) <= (1 if text else 0):
        return {"text": text[:MAX_TEXT_CHARS], "description": description,
                "kind": "pdf", "method": "pypdf", "error": "needs_ocr"}
    return {
        "text": "\n\n".join(seen_text)[:MAX_TEXT_CHARS],
        "description": _trim(description + ". " + " ".join(seen_desc), MAX_DESCRIPTION_CHARS),
        "kind": kind,
        "method": "pypdf+vision",
    }


def media_preflight(settings: Settings) -> str:
    """Why a video cannot be transcribed right now — or '' when it can.
    Checked before the download so nothing is fetched for nothing."""
    if not settings.openai_api_key:
        return "needs_openai_key"
    if not shutil.which("ffmpeg"):
        return "transcript_unavailable"
    return ""


def transcribe_media(settings: Settings, path: Path) -> dict[str, Any]:
    """ffmpeg → mono mp3 → OpenAI Whisper. Only called after media_preflight."""
    blocker = media_preflight(settings)
    if blocker:
        return {"kind": "video", "method": "whisper", "error": blocker}
    audio = path.with_suffix(".transcribe.mp3")
    subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)],
        check=True, capture_output=True, timeout=600,
    )
    if audio.stat().st_size > AUDIO_MAX_BYTES:
        return {"kind": "video", "method": "whisper", "error": "audio_too_large"}
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    with open(audio, "rb") as fh:
        result = client.audio.transcriptions.create(model=WHISPER_MODEL, file=fh)
    transcript = _trim(getattr(result, "text", "") or "", MAX_TEXT_CHARS)
    return {"kind": "video", "method": "whisper", "transcript": transcript,
            "error": "" if transcript else "empty_transcript"}


def understand(settings: Settings, entry: dict[str, Any], client: TelegramClient,
               provider: Any) -> dict[str, Any]:
    """Extract text/description/transcript from one inbox entry."""
    kind, file_id, name, media_type = _classify(entry)
    extracted: dict[str, Any] = {
        "text": "", "description": "", "kind": kind, "transcript": "",
        "method": "text", "error": "",
    }
    if kind == "text":
        return extracted
    if kind == "unsupported":
        extracted["error"] = "unsupported_document"
        extracted["description"] = _trim(f"файл {name} ({media_type})", MAX_DESCRIPTION_CHARS)
        return extracted
    if kind == "video":
        blocker = media_preflight(settings)
        if blocker:
            extracted.update(method="whisper", error=blocker)
            return extracted
    try:
        path = download_file(client, file_id or "", name=name)
    except FileTooLarge as exc:
        log.warning("Файл из копилки (сообщение %s) слишком большой: %s", entry.get("message_id"), exc)
        extracted["error"] = "too_large"
        return extracted
    except Exception as exc:
        log.warning("Файл из копилки (сообщение %s) не скачан: %s", entry.get("message_id"), exc)
        extracted["error"] = _trim(f"download_failed: {exc}", 300)
        return extracted
    extracted["file"] = str(path)
    extracted["method"] = {"image": "vision", "pdf": "pypdf", "video": "whisper"}[kind]
    try:
        if kind == "image":
            extracted.update(extract_image(provider, path, media_type))
        elif kind == "pdf":
            extracted.update(extract_pdf(provider, path))
        else:
            extracted.update(transcribe_media(settings, path))
    except Exception as exc:
        log.warning("Материал (сообщение %s, %s) не распознан: %s", entry.get("message_id"), kind, exc)
        extracted["error"] = _trim(f"{kind}_failed: {exc}", 300)
    extracted.setdefault("error", "")
    return extracted


# ---------------------------------------------------------------------------
# From an inbox entry to a NormalizedItem
# ---------------------------------------------------------------------------

ERROR_NOTES = {
    "needs_openai_key": "видео не расшифровано: не задан OPENAI_API_KEY, пост только по подписи",
    "transcript_unavailable": "видео не расшифровано: на сервере нет ffmpeg, пост только по подписи",
    "audio_too_large": "видео не расшифровано: аудиодорожка длиннее лимита Whisper",
    "too_large": "файл больше 20 МБ, Telegram не отдал его боту; пост только по подписи",
    "needs_ocr": "PDF — скан без текстового слоя, распознать не удалось",
    "unsupported_document": "формат файла не поддерживается, пост только по подписи",
    "empty_file": "файл пустой",
    "empty_transcript": "в видео не нашлось речи",
}


def entry_url(entry: dict[str, Any]) -> str:
    origin = entry.get("origin") or {}
    username, message_id = origin.get("chat_username"), origin.get("message_id")
    if username and message_id:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/inbox/{entry.get('message_id')}"


def _origin_line(entry: dict[str, Any]) -> str:
    origin = entry.get("origin") or {}
    if entry.get("kind") != "forward":
        return "Заметка владелицы, написанная боту напрямую."
    title = origin.get("chat_title") or origin.get("chat_username")
    if origin.get("type") == "channel" and title:
        return f"Пересланный пост из Telegram-канала «{title}» (чужой текст — пересказать своими словами)."
    return "Пересланное сообщение (чужой текст — пересказать своими словами)."


def compose_material(entry: dict[str, Any]) -> str:
    """The text the model sees: what the owner wrote plus what was extracted."""
    extracted = entry.get("extracted") or {}
    kind = extracted.get("kind") or "text"
    if kind in VISION_KINDS:
        kind = "image"  # vision refines "image" into screenshot/floorplan/...
    attachment = {
        "image": "Вложение: изображение.", "pdf": "Вложение: PDF.",
        "video": "Вложение: видео.", "unsupported": "Вложение: файл.",
    }.get(kind, "")
    sections = [" ".join(part for part in (_origin_line(entry), attachment) if part)]
    if entry.get("text"):
        sections.append("Сообщение владелицы (подпись):\n" + str(entry["text"]).strip())
    if extracted.get("text"):
        label = "Текст из PDF:" if extracted.get("method", "").startswith("pypdf") else "Текст с изображения:"
        sections.append(f"{label}\n{extracted['text']}")
    if extracted.get("description"):
        sections.append("Что на изображении:\n" + extracted["description"])
    if extracted.get("transcript"):
        sections.append("Расшифровка видео:\n" + extracted["transcript"])
    error = extracted.get("error") or ""
    if error:
        sections.append("Примечание робота: " + ERROR_NOTES.get(error, f"материал распознан не полностью ({error[:80]})"))
    return "\n\n".join(sections)


def as_item(entry: dict[str, Any]) -> NormalizedItem:
    extracted = entry.get("extracted") or {}
    material = compose_material(entry)
    first_line = next((line.strip() for line in str(entry.get("text") or "").splitlines()
                       if line.strip()), "")
    title = (first_line or extracted.get("description") or "Материал владельца")[:90]
    url = entry_url(entry)
    return NormalizedItem(
        item_id=f"owner-{entry.get('message_id')}",
        source_id=SOURCE_ID,
        category="lifestyle",
        title=title,
        summary=material,
        url=url,
        canonical_url=url,
        published_at=entry.get("received_at"),
        collected_at=_now(),
        lang="ru",
        # "note" (written or sent by the owner herself) vs "forward" (somebody
        # else's message), then what the attachment turned out to be. The
        # fact-check gate reads these: her own first-hand note is a confirmed
        # source, a forwarded or screenshotted post is a single one.
        tags=["owner_inbox", str(entry.get("kind") or "note"), str(extracted.get("kind") or "text")],
        raw_text=material,
    )


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

class _Recording:
    """Provider wrapper that remembers the last raw answer, so a ``skip``
    (the model declined) can be told apart from an LLM failure."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "?")
        self.last_raw = ""

    def complete(self, system: str, user: str, **kwargs: Any) -> str:
        self.last_raw = ""
        raw = self.inner.complete(system, user, **kwargs)
        self.last_raw = raw or ""
        return raw

    def describe_image(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.inner.describe_image(*args, **kwargs)

    def skip_reason(self) -> str | None:
        try:
            probe = extract_json(self.last_raw)
        except LLMError:
            return None
        if probe.get("skip"):
            return _trim(probe.get("skip_reason") or "модель пропустила материал", 300)
        return None


def _is_own_channel_forward(entry: dict[str, Any], settings: Settings) -> bool:
    """Forwards from the owner's own channel are voice samples, not material."""
    own = str(settings.telegram_channel_id or "").lstrip("@").strip().lower()
    origin = entry.get("origin") or {}
    username = str(origin.get("chat_username") or "").lstrip("@").lower()
    return bool(own and username and username == own and entry.get("kind") == "forward")


def _owner_photo(client: TelegramClient, entry: dict[str, Any]) -> Path | None:
    """Local copy of the owner's photo for the post image, re-downloaded when
    the cache from an earlier run is gone. None means: draw a card instead."""
    extracted = entry.get("extracted") or {}
    if extracted.get("kind") not in OWNER_PHOTO_KINDS or not entry.get("photo_file_id"):
        return None
    cached = extracted.get("file")
    path = Path(cached) if cached else None
    if path is None or not path.exists() or path.stat().st_size == 0:
        try:
            path = download_file(client, entry["photo_file_id"], name="photo.jpg")
        except Exception as exc:
            log.warning("Фото владельца не скачано, рисую карточку: %s", exc)
            return None
    return path if path.exists() and path.stat().st_size > 0 else None


def pending_entries(inbox: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = inbox.get("items") or []
    to_understand = [e for e in items if not e.get("processed_at")]
    to_draft = [e for e in items if e.get("processed_at") and not e.get("drafted_at")
                and not e.get("skipped_reason")]
    return to_understand, to_draft


def process_inbox(settings: Settings, inbox: dict[str, Any], client: TelegramClient,
                  provider: Any, *, persist: bool = True) -> int:
    """Step 1: understand every not-yet-processed entry. Returns the count."""
    to_understand, _ = pending_entries(inbox)
    done = 0
    for entry in to_understand:
        if _is_own_channel_forward(entry, settings):
            entry["processed_at"] = _now()
            entry["skipped_reason"] = "пересланный пост из собственного канала — образец голоса, не материал"
            done += 1
            continue
        entry["extracted"] = understand(settings, entry, client, provider)
        entry["processed_at"] = _now()
        done += 1
        log.info("Материал %s понят: kind=%s method=%s error=%s текст=%d симв.",
                 entry.get("message_id"), entry["extracted"].get("kind"),
                 entry["extracted"].get("method"), entry["extracted"].get("error") or "-",
                 len(entry["extracted"].get("text") or "") + len(entry["extracted"].get("transcript") or ""))
        if persist:
            # Save after each entry: a crash on the third file keeps the first two.
            state.save(STATE_FILE, inbox)
    return done


def _notify_skip(settings: Settings, client: TelegramClient, entry: dict[str, Any], reason: str) -> None:
    if not settings.telegram_owner_id:
        return
    try:
        client.send_message(
            settings.telegram_owner_id,
            f"Из материала (сообщение {entry.get('message_id')}) пост не сложился: {reason}",
        )
    except Exception as exc:
        log.warning("Не удалось сообщить владельцу о пропуске: %s", exc)


# Words that say nothing about the subject; without this list «если»,
# «только», «есть» made every post look related to every note.
_STOP_STEMS = {
    "если", "тольк", "есть", "могут", "может", "любой", "будет", "быть", "этот", "этого",
    "того", "чтобы", "когда", "после", "перед", "очень", "тоже", "также", "ещё", "еще",
    "уже", "сейча", "тепер", "напиш", "комме", "дубай", "дубае", "оаэ", "котор", "всего",
    "просто", "можно", "нужно", "стоит", "сразу", "минут", "прямо", "самый", "самое",
    "часто", "всегд", "почти", "между", "через", "более", "менее", "здесь", "новый",
}
MERGE_MIN_COMMON = 4
MERGE_MIN_RATIO = 0.10
MERGEABLE_STATUSES = {"queued", "rewrite", "postponed", "approved"}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z]{4,}", (text or "").lower())} - _STOP_STEMS


def related_post(queue: dict[str, Any], text: str) -> dict[str, Any] | None:
    """The queued post the owner's note is about, if any.

    The owner sent four tips on short-term rentals right after the Dubizzle
    booking post and expected them inside that post, not as a second one
    («они взаимосвязаны»). Subject overlap is measured on word stems with
    the noise words removed; the best candidate wins when it shares at least
    ``MERGE_MIN_COMMON`` stems and ``MERGE_MIN_RATIO`` of the shorter text.
    """
    note = _stems(text)
    if not note:
        return None
    best, best_score = None, (0, 0.0)
    for post in queue.get("posts") or []:
        if post.get("status") not in MERGEABLE_STATUSES:
            continue
        stems = _stems(f"{post.get('title', '')} {post.get('body', '')}")
        common = len(note & stems)
        ratio = common / max(1, min(len(note), len(stems)))
        if common >= MERGE_MIN_COMMON and ratio >= MERGE_MIN_RATIO and (common, ratio) > best_score:
            best, best_score = post, (common, ratio)
    return best


def merge_into_post(post: dict[str, Any], item: NormalizedItem, *, note: str) -> None:
    """Attach the owner's material to ``post`` and send it back to the model.

    The post keeps its id, slot and rubric; ``run.regenerate_rewrites``
    rebuilds it from ``source_items`` (now including the material) with the
    merge instruction, and a fresh preview goes to the owner.
    """
    from .generate import snapshot_items

    items = post.setdefault("source_items", [])
    if not any(row.get("item_id") == item.item_id for row in items):
        items.extend(snapshot_items([item]))
    ids = post.setdefault("item_ids", [])
    if item.item_id not in ids:
        ids.append(item.item_id)
    post["status"] = "rewrite"
    post["approval"] = {
        "sent_at": None, "action": "merge", "merge": True,
        "requested_at": _now(),
        "instruction": _trim("Владелица прислала дополнение к этому посту, его нужно вплести в текст, "
                             "а не пересказать отдельно:\n" + note, 900),
    }


def drafts_from_inbox(settings: Settings, provider: Any = None,
                      client: TelegramClient | None = None) -> list[PostDraft]:
    """Understand new materials, draft posts from them, queue the drafts.

    Material about a post that is already in the queue is merged into that
    post (``related_post``) instead of becoming a second one.
    """
    inbox = state.load(STATE_FILE)
    to_understand, to_draft = pending_entries(inbox)
    if not to_understand and not to_draft:
        log.info("Копилка владельца: новых материалов нет")
        return []

    # Created lazily: an empty inbox must not need any key at all.
    provider = _Recording(provider or get_provider(settings))
    client = client or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)

    understood = process_inbox(settings, inbox, client, provider)
    _, to_draft = pending_entries(inbox)
    log.info("Копилка владельца: понято %d, к черновикам %d", understood, len(to_draft))

    drafts: list[PostDraft] = []
    queue = postqueue.load_queue()
    merged = 0
    for entry in to_draft:
        item = as_item(entry)
        target = related_post(queue, str(entry.get("text") or "") + " " + (item.summary or ""))
        if target is not None:
            merge_into_post(target, item, note=str(entry.get("text") or item.summary))
            entry["drafted_at"] = _now()
            entry["post_id"] = target.get("post_id")
            entry["merged_into"] = target.get("post_id")
            merged += 1
            log.info("Материал %s вплетён в пост %s", entry.get("message_id"), target.get("post_id"))
            try:
                client.send_message(
                    str(settings.telegram_owner_id),
                    f"Дополнение вошло в пост «{_trim(target.get('title') or '', 60)}» — "
                    "перепишу его одним текстом и пришлю превью заново.")
            except Exception as exc:
                log.warning("Не удалось сообщить о слиянии: %s", exc)
            continue
        draft = generate_for_rubric(settings, RUBRIC, [item], provider=provider)
        attempts = int(entry.get("draft_attempts") or 0) + 1
        entry["draft_attempts"] = attempts

        if draft is None:
            reason = provider.skip_reason()
            if reason:
                entry["skipped_reason"] = reason
                log.info("Материал %s пропущен моделью: %s", entry.get("message_id"), reason)
                _notify_skip(settings, client, entry, reason)
            elif attempts >= MAX_DRAFT_ATTEMPTS:
                entry["skipped_reason"] = f"модель не вернула валидный пост за {attempts} попытки"
                log.warning("Материал %s: %s", entry.get("message_id"), entry["skipped_reason"])
                _notify_skip(settings, client, entry, entry["skipped_reason"])
            else:
                log.warning("Материал %s: ответа модели нет, повторим в следующем прогоне",
                            entry.get("message_id"))
            continue
        if draft.status == "failed":
            errors = "; ".join((draft.gate or {}).get("errors") or [])
            if attempts >= MAX_DRAFT_ATTEMPTS:
                entry["skipped_reason"] = _trim(f"факт-чек не пройден: {errors}", 300)
                log.warning("Материал %s: %s", entry.get("message_id"), entry["skipped_reason"])
                _notify_skip(settings, client, entry, entry["skipped_reason"])
            else:
                log.warning("Материал %s: факт-чек не пройден (%s), повторим", entry.get("message_id"), errors)
            continue

        photo = _owner_photo(client, entry)
        if photo:
            draft.image_path = str(photo)
            draft.image_meta = {
                "provider": "owner",
                "license": "материал владельца канала",
                "author": "владелец канала",
                "source_url": "",
                "telegram_file_id": entry["photo_file_id"],
                "headline": (draft.image or {}).get("headline") or draft.title,
                "accent": "",
            }
        else:
            draft.image_path, draft.image_meta = illustrate.illustrate(settings, draft)
        entry["drafted_at"] = _now()
        entry["post_id"] = draft.post_id
        drafts.append(draft)
        log.info("Материал %s → черновик %s", entry.get("message_id"), draft.post_id)

    if merged:
        # Saved before enqueue(), which loads its own copy of the queue.
        postqueue.save_queue(queue)
    if drafts:
        queue = postqueue.enqueue(drafts, persist=False)
        by_id = {p.get("post_id"): p for p in (queue.get("posts") or [])}
        for draft in drafts:
            file_id = (draft.image_meta or {}).get("telegram_file_id")
            post = by_id.get(draft.post_id)
            if file_id and post is not None:
                # Lets approve/publish send the owner's photo by file_id, with
                # no dependency on assets/cache/ surviving between runs.
                post["telegram_file_id"] = file_id
        postqueue.save_queue(queue)
    # The queue is saved first, the inbox state second. A crash in between
    # leaves the entry without ``drafted_at``, so the next run generates the
    # post again — and ``post_id`` is derived from the message id, so
    # ``enqueue`` updates the same queue entry instead of adding a twin. The
    # other order (inbox first) would mark the material drafted while no
    # post exists, and it would be lost for good.
    state.save(STATE_FILE, inbox)
    return drafts
