"""Prompt loading and rendering.

Prompts live in ``prompts/`` as Markdown files named after the rubric id and
are never hard-coded here: adding a rubric means adding a file, not editing
Python. Available placeholders are documented in prompts/README.md.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .config import PROMPTS_DIR, RUBRICS
from .models import NormalizedItem

log = logging.getLogger("pipeline.prompts")

SYSTEM_PROMPT_FILE = "system_tone.md"

FALLBACK_SYSTEM = (
    "Ты — редактор русскоязычного Telegram-канала о недвижимости и жизни в Дубае. "
    "Пиши по-русски, без воды. Любая цифра допускается только если она есть во "
    "входных данных, и на неё обязана быть ссылка. Не обещай гарантированную "
    "доходность. Отвечай СТРОГО одним валидным JSON-объектом без markdown-обёртки."
)

FALLBACK_TASK = (
    "RUBRIC_ID: {{rubric}}\n"
    "Сегодня: {{today}}\n"
    "Максимальная длина поста: {{max_chars}} символов.\n\n"
    "Напиши пост рубрики «{{rubric_title}}» по входным данным ниже. "
    "Каждую цифру подтверждай ссылкой из этих же данных.\n\n"
    "<INPUT_ITEMS>\n{{items}}\n</INPUT_ITEMS>\n"
)


def load_system_prompt() -> str:
    path = PROMPTS_DIR / SYSTEM_PROMPT_FILE
    if path.exists():
        return path.read_text(encoding="utf-8")
    log.warning("Не найден %s, используется встроенный системный промпт", path)
    return FALLBACK_SYSTEM


def load_rubric_prompt(rubric: str) -> str:
    path = PROMPTS_DIR / f"{rubric}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    log.warning("Не найден промпт %s, используется встроенный шаблон", path)
    return FALLBACK_TASK


def items_block(items: list[NormalizedItem]) -> str:
    """Serialise input items as JSON so the model can cite them precisely."""
    payload = [
        {
            "item_id": item.item_id,
            "source_id": item.source_id,
            "category": item.category,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "published_at": item.published_at,
            "lang": item.lang,
        }
        for item in items
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render(template: str, context: dict[str, Any]) -> str:
    out = template
    for key, value in context.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def build_prompt(rubric: str, items: list[NormalizedItem], *, today: str | None = None) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for a rubric."""
    meta = RUBRICS.get(rubric, {})
    context = {
        "rubric": rubric,
        "rubric_title": meta.get("title", rubric),
        "max_chars": meta.get("max_chars", 900),
        "today": today or datetime.now().strftime("%Y-%m-%d"),
        "items": items_block(items),
    }
    task = render(load_rubric_prompt(rubric), context)
    # The rubric id and the items block are appended unconditionally so that
    # a hand-edited prompt file that forgot a placeholder still works.
    if "RUBRIC_ID:" not in task:
        task = f"RUBRIC_ID: {rubric}\n\n" + task
    if "<INPUT_ITEMS>" not in task:
        task += f"\n\n<INPUT_ITEMS>\n{context['items']}\n</INPUT_ITEMS>\n"
    return load_system_prompt(), task
