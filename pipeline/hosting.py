"""Public URL for a card image, so a long post can be ONE Telegram message.

Telegram caps a photo caption at 1024 characters; anything longer went out
as a photo plus a second text message, which the owner called «коряво».
A text message, on the other hand, may carry 4096 characters and show a
large link preview *above* the text — that is one message with the card on
top and the whole post under it. The preview needs a public image URL.

The repository is public, so the card is committed to ``cards/`` and served
from raw.githubusercontent.com at the commit's SHA (immutable, cacheable).
Works only inside GitHub Actions (git credentials are there); anywhere else
``host_card`` returns None and the caller falls back to photo + text.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .config import ROOT
import hashlib

log = logging.getLogger("pipeline.hosting")

CARDS_DIR = ROOT / "cards"
RAW_BASE = "https://raw.githubusercontent.com"
DEFAULT_REPO = "missisnew-oss/news"


def repo_slug() -> str:
    """``owner/repo`` from Actions' GITHUB_REPOSITORY or the git remote."""
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if slug:
        return slug
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
    except Exception:
        return DEFAULT_REPO
    url = url.removesuffix(".git")
    if "github.com" in url:
        tail = url.split("github.com", 1)[1].lstrip(":/")
        if tail.count("/") == 1:
            return tail
    return DEFAULT_REPO


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _commit_and_push(rel_path: str, message: str) -> str | None:
    """Commit one file and push; returns the commit SHA or None."""
    _git("config", "user.name", "dubai-channel-bot")
    _git("config", "user.email", "bot@users.noreply.github.com")
    if _git("add", rel_path).returncode != 0:
        return None
    if _git("diff", "--cached", "--quiet").returncode == 0:
        # Already committed earlier (same content): the current HEAD serves it.
        return _git("rev-parse", "HEAD").stdout.strip() or None
    if _git("commit", "-m", f"{message} [skip ci]").returncode != 0:
        return None
    for attempt in range(1, 4):
        if _git("push").returncode == 0:
            return _git("rev-parse", "HEAD").stdout.strip() or None
        log.warning("push карточки не прошёл (попытка %d), подтягиваем ветку", attempt)
        pull = _git("pull", "--rebase", "--autostash")
        if pull.returncode != 0:
            _git("rebase", "--abort")
            return None
        time.sleep(attempt * 2)
    return None


def _reachable(url: str, *, attempts: int = 6, pause: float = 3.0) -> bool:
    import requests

    for _ in range(attempts):
        try:
            response = requests.get(url, timeout=15, stream=True)
            if response.status_code == 200:
                response.close()
                return True
        except Exception as exc:
            log.info("Карточка по ссылке пока недоступна: %s", exc)
        time.sleep(pause)
    return False


def host_card(image_path: str | Path | None, post_id: str, settings: Any,
              *, commit: Callable[[str, str], str | None] | None = None,
              reachable: Callable[[str], bool] | None = None) -> str | None:
    """Commit the card to ``cards/`` and return its raw.githubusercontent URL.

    None when there is nothing to host, in DRY_RUN, outside a pushable
    checkout, or when the URL does not answer within ~20 seconds.
    """
    if not image_path or getattr(settings, "dry_run", False):
        return None
    src = Path(image_path)
    if not src.exists() or src.stat().st_size == 0:
        return None
    if src.stat().st_size > 5_000_000:
        log.warning("Карточка %s слишком велика для превью (%d байт)", src, src.stat().st_size)
        return None
    digest = hashlib.sha1(src.read_bytes()).hexdigest()[:10]
    name = f"{post_id}-{digest}{src.suffix.lower() or '.png'}"
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = CARDS_DIR / name
    if not dest.exists():
        shutil.copyfile(src, dest)
    rel = str(dest.relative_to(ROOT))
    sha = (commit or _commit_and_push)(rel, f"cards: {post_id}")
    if not sha:
        log.warning("Карточка %s не закоммичена — пост уйдёт фото плюс текст", rel)
        return None
    url = f"{RAW_BASE}/{repo_slug()}/{sha}/{rel}"
    if not (reachable or _reachable)(url):
        log.warning("Карточка %s не отдаётся по ссылке — пост уйдёт фото плюс текст", url)
        return None
    return url


def invisible_link(url: str) -> str:
    """The zero-width anchor that makes Telegram show ``url`` as the preview."""
    return f'<a href="{url}">&#8205;</a>'
