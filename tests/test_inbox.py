"""The owner's inbox: understanding forwarded materials and drafting posts.

No network anywhere: the Telegram client is a fake that serves bytes from
memory, the LLM provider is a fake, and ``socket.socket`` is replaced by a
function that raises (same guard as tests/test_dry_run_no_network.py).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pipeline import inbox, postqueue, state
from pipeline.offline_draft import build_offline_draft
from pipeline.telegram import TelegramClient


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("тест копилки попытался выйти в сеть")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(inbox, "CACHE_DIR", tmp_path / "cache")
    # No ffmpeg on this machine as far as the tests are concerned.
    monkeypatch.setattr(inbox.shutil, "which", lambda name: None)


class FakeTelegram(TelegramClient):
    """Serves files from memory; everything else is the DRY_RUN recorder."""

    def __init__(self, files: dict[str, tuple[bytes, str]] | None = None) -> None:
        super().__init__("test-token", dry_run=True)
        self.files = files or {}

    def get_file(self, file_id: str) -> dict:
        self.calls.append({"method": "getFile", "payload": {"file_id": file_id}})
        data, suffix = self.files[file_id]
        return {"file_id": file_id, "file_path": f"files/{file_id}{suffix}", "file_size": len(data)}

    def download_file(self, file_path: str, dest, timeout: int = 120) -> Path:
        self.calls.append({"method": "downloadFile", "payload": {"file_path": file_path}})
        file_id = Path(file_path).stem
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[file_id][0])
        return dest


class FakeProvider:
    name = "fake"

    def __init__(self, *, skip: bool = False, kind: str = "floorplan") -> None:
        self.skip = skip
        self.kind = kind
        self.prompts: list[str] = []
        self.images: list[tuple[str, int]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 0) -> str:
        self.prompts.append(user)
        if self.skip:
            return json.dumps({"skip": True, "skip_reason": "личная заметка без темы"})
        return json.dumps(build_offline_draft(user), ensure_ascii=False)

    def describe_image(self, image_bytes: bytes, media_type: str, instruction: str,
                       *, max_tokens: int = 0) -> dict:
        self.images.append((media_type, len(image_bytes)))
        return {
            "text": "Планировка 2 спальни, 1 250 кв. футов, от 1,9 млн AED",
            "description": "Планировка квартиры с двумя спальнями",
            "kind": self.kind,
        }


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer (Helvetica, ASCII only)."""
    lines = text.split("\n")
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{obj}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def _entry(message_id: int, text: str = "", *, photo: str | None = None,
           document: dict | None = None, origin: dict | None = None) -> dict:
    return {
        "message_id": message_id,
        "received_at": "2026-09-22T10:00:00+00:00",
        "kind": "forward" if origin else "note",
        "origin": origin or {"type": None, "chat_username": None, "chat_title": None, "message_id": None},
        "text": text,
        "photo_file_id": photo,
        "document": document,
    }


def _save_inbox(*entries: dict) -> None:
    state.save("inbox.json", {"version": 1, "items": list(entries)})


def _inbox_items() -> list[dict]:
    return state.load("inbox.json")["items"]


# ---------------------------------------------------------------------------

def test_photo_goes_through_vision_and_becomes_a_post_with_the_owners_image(settings):
    _save_inbox(_entry(1, "Планировка от застройщика, 2 спальни", photo="PHOTO1"))
    client = FakeTelegram({"PHOTO1": (b"\xff\xd8jpegbytes", ".jpg")})
    provider = FakeProvider(kind="floorplan")

    drafts = inbox.drafts_from_inbox(settings, provider=provider, client=client)

    assert provider.images == [("image/jpeg", len(b"\xff\xd8jpegbytes"))]
    entry = _inbox_items()[0]
    assert entry["processed_at"] and entry["drafted_at"]
    assert entry["extracted"]["kind"] == "floorplan"
    assert entry["extracted"]["method"] == "vision"
    assert "1 250 кв. футов" in entry["extracted"]["text"]
    assert "Планировка квартиры" in entry["extracted"]["description"]

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.rubric == "from_owner"
    assert entry["post_id"] == draft.post_id
    assert draft.image_meta["provider"] == "owner"
    assert Path(draft.image_path).read_bytes() == b"\xff\xd8jpegbytes"
    # The model saw the extracted text, the caption, and the owner-inbox url.
    assert "1 250 кв. футов" in provider.prompts[0]
    assert "Планировка от застройщика" in provider.prompts[0]
    assert "https://t.me/c/inbox/1" in provider.prompts[0]

    posts = postqueue.load_queue()["posts"]
    assert [p["post_id"] for p in posts] == [draft.post_id]
    assert posts[0]["status"] == "queued"
    assert posts[0]["telegram_file_id"] == "PHOTO1", "фото владельца уходит по file_id"
    assert posts[0]["sources"][0]["source_id"] == "owner_inbox"


def test_screenshot_of_someone_elses_post_gets_a_card_not_the_screenshot(settings):
    _save_inbox(_entry(2, "Скрин из инстаграма про новые правила", photo="SHOT"))
    client = FakeTelegram({"SHOT": (b"pngbytes", ".png")})
    drafts = inbox.drafts_from_inbox(settings, provider=FakeProvider(kind="screenshot"), client=client)

    assert len(drafts) == 1
    assert drafts[0].image_meta["provider"] == "own_card"
    assert "telegram_file_id" not in postqueue.load_queue()["posts"][0]


def test_pdf_with_a_text_layer_is_read_by_pypdf_without_vision(settings):
    text = "\n".join(["Payment plan 60/40. Studio from AED 850000. Handover Q4 2027."] * 5)
    pdf = _minimal_pdf(text)
    _save_inbox(_entry(3, "Прайс от застройщика",
                       document={"file_id": "DOC1", "name": "prices.pdf", "mime": "application/pdf"}))
    client = FakeTelegram({"DOC1": (pdf, ".pdf")})
    provider = FakeProvider()

    drafts = inbox.drafts_from_inbox(settings, provider=provider, client=client)

    entry = _inbox_items()[0]
    assert entry["extracted"]["method"] == "pypdf"
    assert entry["extracted"]["kind"] == "pdf"
    assert "AED 850000" in entry["extracted"]["text"]
    assert not entry["extracted"].get("error")
    assert provider.images == [], "у PDF есть текстовый слой — vision не нужен"
    assert len(drafts) == 1 and "Текст из PDF" in provider.prompts[0]


def test_video_without_openai_key_is_drafted_from_its_caption(settings):
    settings.openai_api_key = ""
    _save_inbox(_entry(4, "Рилс про новый парк в Dubai Hills, открытие 12 октября",
                       document={"file_id": "VID1", "name": "reel.mp4", "mime": "video/mp4"}))
    client = FakeTelegram({"VID1": (b"mp4", ".mp4")})
    provider = FakeProvider()

    drafts = inbox.drafts_from_inbox(settings, provider=provider, client=client)

    entry = _inbox_items()[0]
    assert entry["extracted"]["error"] == "needs_openai_key"
    assert entry["extracted"]["transcript"] == ""
    assert not any(c["method"] in ("getFile", "downloadFile") for c in client.calls), \
        "без ключа видео даже не скачивается"
    assert len(drafts) == 1
    assert "Рилс про новый парк" in provider.prompts[0]
    assert "OPENAI_API_KEY" in provider.prompts[0], "модели сказано, что расшифровки нет"
    assert drafts[0].image_meta["provider"] == "own_card"


def test_second_run_does_nothing(settings):
    _save_inbox(_entry(5, "Планировка", photo="P5"),
                _entry(6, "Заметка: сравнить цены в Marina и JVC"))
    client = FakeTelegram({"P5": (b"jpg", ".jpg")})
    provider = FakeProvider()

    first = inbox.drafts_from_inbox(settings, provider=provider, client=client)
    assert len(first) == 2
    prompts, images, calls = len(provider.prompts), len(provider.images), len(client.calls)
    queue_before = json.dumps(postqueue.load_queue(), sort_keys=True)

    second = inbox.drafts_from_inbox(settings, provider=provider, client=client)

    assert second == []
    assert (len(provider.prompts), len(provider.images), len(client.calls)) == (prompts, images, calls)
    assert json.dumps(postqueue.load_queue(), sort_keys=True) == queue_before


def test_empty_inbox_needs_no_provider_and_no_client(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("на пустой копилке провайдер и клиент не создаются")

    monkeypatch.setattr(inbox, "get_provider", boom)
    monkeypatch.setattr(inbox, "TelegramClient", boom)
    assert inbox.drafts_from_inbox(settings, provider=None, client=None) == []


def test_skip_marks_the_entry_and_tells_the_owner(settings):
    _save_inbox(_entry(7, "напомнить себе позвонить в DEWA"))
    client = FakeTelegram()
    provider = FakeProvider(skip=True)

    assert inbox.drafts_from_inbox(settings, provider=provider, client=client) == []
    entry = _inbox_items()[0]
    assert entry["skipped_reason"] == "личная заметка без темы"
    assert not entry.get("drafted_at")
    assert postqueue.load_queue()["posts"] == []
    notices = [c for c in client.calls if c["method"] == "sendMessage"]
    assert notices and "не сложился" in notices[0]["payload"]["text"]

    # Parked for good: the next run does not ask the model again.
    inbox.drafts_from_inbox(settings, provider=provider, client=client)
    assert len(provider.prompts) == 1


def test_forward_from_the_owners_own_channel_is_a_voice_sample_not_material(settings):
    settings.telegram_channel_id = "@my_channel"
    _save_inbox(_entry(8, "Мой старый пост", photo="P8",
                       origin={"type": "channel", "chat_username": "my_channel",
                               "chat_title": "Мой канал", "message_id": 42}))
    provider = FakeProvider()
    assert inbox.drafts_from_inbox(settings, provider=provider, client=FakeTelegram()) == []
    entry = _inbox_items()[0]
    assert entry["processed_at"] and "собственного канала" in entry["skipped_reason"]
    assert provider.prompts == [] and provider.images == []


def test_forwarded_channel_post_uses_its_public_url(settings):
    entry = _entry(9, "Текст", origin={"type": "channel", "chat_username": "dxb_news",
                                       "chat_title": "DXB", "message_id": 77})
    assert inbox.entry_url(entry) == "https://t.me/dxb_news/77"
    item = inbox.as_item(entry)
    assert item.source_id == "owner_inbox" and item.url == "https://t.me/dxb_news/77"
    assert "чужой текст" in item.summary


def test_too_large_file_is_recorded_and_the_caption_still_makes_a_post(settings):
    class Huge(FakeTelegram):
        def get_file(self, file_id):
            return {"file_id": file_id, "file_path": "files/x.pdf",
                    "file_size": inbox.MAX_FILE_BYTES + 1}

    _save_inbox(_entry(10, "Брошюра по проекту Creek Harbour",
                       document={"file_id": "BIG", "name": "brochure.pdf", "mime": "application/pdf"}))
    drafts = inbox.drafts_from_inbox(settings, provider=FakeProvider(), client=Huge())
    assert _inbox_items()[0]["extracted"]["error"] == "too_large"
    assert len(drafts) == 1


def test_extracted_text_is_bounded(settings):
    class Chatty(FakeProvider):
        def describe_image(self, *args, **kwargs):
            return {"text": "x" * 20000, "description": "d" * 5000, "kind": "pricelist"}

    _save_inbox(_entry(11, "Прайс", photo="P11"))
    inbox.drafts_from_inbox(settings, provider=Chatty(), client=FakeTelegram({"P11": (b"jpg", ".jpg")}))
    extracted = _inbox_items()[0]["extracted"]
    assert len(extracted["text"]) == inbox.MAX_TEXT_CHARS
    assert len(extracted["description"]) == inbox.MAX_DESCRIPTION_CHARS


def test_dry_run_client_stub_never_opens_a_socket():
    client = TelegramClient("token", dry_run=True)
    info = client.get_file("ABC")
    assert info["file_id"] == "ABC" and info["file_path"]
    dest = client.download_file(info["file_path"], Path(inbox.CACHE_DIR) / "stub.bin")
    assert dest.exists() and dest.stat().st_size == 0
    assert [c["method"] for c in client.calls] == ["getFile", "downloadFile"]
