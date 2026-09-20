"""Configuration: paths, environment, rubric registry, source registry loading.

Secrets are read from the process environment only. A local ``.env`` file is
loaded as a convenience for developers, but it is git-ignored and never
required: in CI every value comes from GitHub Secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
STATE_DIR = ROOT / "state"
ASSETS_DIR = ROOT / "assets"
OUT_DIR = ROOT / "out"

SOURCES_FILE = CONFIG_DIR / "sources.yml"
SETTINGS_FILE = CONFIG_DIR / "settings.yml"

TIMEZONE = "Asia/Dubai"

# Fixed rubric registry. These ids are a contract shared by:
#   ``per_4weeks`` is the planned number of posts per 4-week cycle. The plan is
#   49 posts / 28 days (~1.75 per day, within the brief's 1-2) of which 12 are
#   selling (new_launch + investor_math + case_story) = 24.5%, under the 25% cap.
#   docs/RUBRICS.md, prompts/<rubric_id>.md and this module.
# tests/test_prompts_rubrics.py enforces that the three stay in sync.
RUBRICS: dict[str, dict[str, Any]] = {
    "market_pulse": {
        "title": "Пульс рынка",
        "categories": ["official_data", "realty_news"],
        "selling": False,
        "max_chars": 900,
        "per_4weeks": 4,
    },
    "new_launch": {
        "title": "Новый лонч",
        "categories": ["developers", "realty_news"],
        "selling": True,
        "max_chars": 900,
        "per_4weeks": 6,
    },
    "investor_math": {
        "title": "Считаем деньги",
        "categories": ["official_data", "realty_news", "developers"],
        "selling": True,
        "max_chars": 1000,
        "per_4weeks": 4,
    },
    "rules_and_laws": {
        "title": "Правила игры",
        "categories": ["city_gov", "official_data"],
        "selling": False,
        "max_chars": 950,
        "per_4weeks": 4,
    },
    "area_guide": {
        "title": "Район под лупой",
        "categories": ["realty_news", "lifestyle", "official_data"],
        "selling": False,
        "max_chars": 1000,
        "per_4weeks": 4,
    },
    "dubai_life": {
        "title": "Жизнь в Дубае",
        "categories": ["lifestyle", "city_gov"],
        "selling": False,
        "max_chars": 900,
        "per_4weeks": 6,
    },
    "events_afisha": {
        "title": "Афиша",
        "categories": ["events", "lifestyle"],
        "selling": False,
        "max_chars": 900,
        "per_4weeks": 5,
    },
    "faq_answer": {
        "title": "Вопрос — ответ",
        "categories": ["realty_news", "city_gov", "official_data"],
        "selling": False,
        "max_chars": 900,
        "per_4weeks": 6,
    },
    "case_story": {
        "title": "История клиента",
        "categories": ["realty_news", "developers"],
        "selling": True,
        "max_chars": 1000,
        "per_4weeks": 2,
    },
    "personal": {
        "title": "Личное",
        "categories": [],
        "selling": False,
        "max_chars": 1000,
        "per_4weeks": 8,
        "manual": True,  # owner writes the text herself from a generated brief
    },
}

SELLING_RUBRICS = {rid for rid, meta in RUBRICS.items() if meta["selling"]}

# Telegram hard limits (Bot API).
TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096


def _load_dotenv() -> None:
    """Load ROOT/.env into os.environ without overriding real env vars."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Runtime settings assembled from the environment."""

    dry_run: bool = True
    log_level: str = "INFO"

    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_owner_id: str = ""

    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    unsplash_access_key: str = ""
    pexels_api_key: str = ""

    tuning: dict[str, Any] = field(default_factory=dict)

    @property
    def secret_values(self) -> list[str]:
        """Every secret-like value, used by the log redactor."""
        values = [
            self.telegram_bot_token,
            self.anthropic_api_key,
            self.openai_api_key,
            self.unsplash_access_key,
            self.pexels_api_key,
        ]
        return [v for v in values if v and len(v) >= 8]

    def require_telegram(self) -> None:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("TELEGRAM_CHANNEL_ID", self.telegram_channel_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Не заданы переменные окружения: " + ", ".join(missing)
            )


def load_settings() -> Settings:
    _load_dotenv()
    settings = Settings(
        dry_run=_env_bool("DRY_RUN", True),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_channel_id=os.environ.get("TELEGRAM_CHANNEL_ID", "").strip(),
        telegram_owner_id=os.environ.get("TELEGRAM_OWNER_ID", "").strip(),
        llm_provider=os.environ.get("LLM_PROVIDER", "anthropic").strip().lower(),
        llm_model=os.environ.get("LLM_MODEL", "claude-sonnet-5").strip(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip(),
        unsplash_access_key=os.environ.get("UNSPLASH_ACCESS_KEY", "").strip(),
        pexels_api_key=os.environ.get("PEXELS_API_KEY", "").strip(),
    )
    settings.tuning = load_settings_file()
    return settings


def load_settings_file() -> dict[str, Any]:
    """Non-secret tuning knobs from config/settings.yml."""
    if not SETTINGS_FILE.exists():
        return {}
    import yaml

    data = yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def load_sources(path: Path | None = None) -> dict[str, Any]:
    """Load and validate config/sources.yml.

    Returns the full document; ``sources`` is filtered to entries that are
    structurally valid. Validation errors are raised, not silently ignored,
    so a broken registry fails the workflow loudly.
    """
    import yaml

    path = path or SOURCES_FILE
    if not path.exists():
        raise FileNotFoundError(f"Не найден реестр источников: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    validate_sources_doc(doc)
    return doc


ALLOWED_SOURCE_TYPES = {"rss", "atom", "json_api", "html", "ics", "sitemap"}
ALLOWED_VERIFICATION_STATUS = {"ok", "failed", "unverified"}
ALLOWED_REUSE = {
    "summary_with_link",
    "press_release",
    "open_data",
    "api_terms",
    "unknown",
}


def validate_sources_doc(doc: dict[str, Any]) -> None:
    if not isinstance(doc, dict):
        raise ValueError("sources.yml должен быть YAML-словарём")
    categories = doc.get("categories") or {}
    if not categories:
        raise ValueError("sources.yml: пустой блок categories")
    sources = doc.get("sources") or []
    if not isinstance(sources, list):
        raise ValueError("sources.yml: sources должен быть списком")

    seen: set[str] = set()
    for idx, src in enumerate(sources):
        where = f"sources[{idx}]"
        for key in ("id", "title", "url", "type", "category", "lang"):
            if not src.get(key):
                raise ValueError(f"{where}: отсутствует обязательное поле {key!r}")
        sid = src["id"]
        if sid in seen:
            raise ValueError(f"{where}: дублирующийся id {sid!r}")
        seen.add(sid)
        if src["type"] not in ALLOWED_SOURCE_TYPES:
            raise ValueError(f"{where}: недопустимый type {src['type']!r}")
        if src["category"] not in categories:
            raise ValueError(
                f"{where}: category {src['category']!r} отсутствует в блоке categories"
            )
        if not str(src["url"]).startswith(("http://", "https://")):
            raise ValueError(f"{where}: url должен быть абсолютным")
        verification = src.get("verification") or {}
        status = verification.get("status", "unverified")
        if status not in ALLOWED_VERIFICATION_STATUS:
            raise ValueError(f"{where}: недопустимый verification.status {status!r}")
        reuse = (src.get("license") or {}).get("reuse", "unknown")
        if reuse not in ALLOWED_REUSE:
            raise ValueError(f"{where}: недопустимый license.reuse {reuse!r}")


def enabled_sources(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Sources the collector may actually hit.

    A source is used only when it is explicitly enabled. Its verification
    status is surfaced to the caller but does not, by itself, disable it —
    ``pipeline.verify_sources`` is what flips ``enabled`` after a real check.
    """
    return [s for s in (doc.get("sources") or []) if s.get("enabled")]
