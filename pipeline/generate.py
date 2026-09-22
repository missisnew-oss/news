"""Stage 4 — GENERATE: turn scored items into post drafts via the LLM."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import factcheck, prompts
from .config import RUBRICS, SELLING_RUBRICS, Settings
from .llm import extract_json, LLMError, get_provider, parse_response
from .models import NormalizedItem, PostDraft
from .score import select_for_rubric
from .textutil import sanitize_telegram_html, sha1, truncate

log = logging.getLogger("pipeline.generate")

MAX_ATTEMPTS = 2


def _post_id(rubric: str, items: list[NormalizedItem]) -> str:
    """Stable id: same rubric over the same items yields the same post_id.

    This is what makes a re-run of the generate workflow idempotent at the
    queue level — a repeated run updates the existing draft instead of
    appending a twin.
    """
    seed = rubric + "|" + "|".join(sorted(i.item_id for i in items))
    return f"{rubric}-{sha1(seed)[:12]}"


# docs/LEGAL.md requires this line on every selling post. It is appended by
# code, not left to the model, so it can never be forgotten or reworded.
SELLING_DISCLAIMER = (
    "<i>Не инвестиционная рекомендация. Условия застройщика могут измениться, "
    "доходность не гарантирована.</i>"
)


def compose_text(payload: dict[str, Any]) -> str:
    """Body + CTA + hashtags + legal disclaimer, sanitised for Telegram HTML."""
    parts = [sanitize_telegram_html(payload.get("body", "")).strip()]
    cta = sanitize_telegram_html(payload.get("cta", "")).strip()
    if cta and cta not in parts[0]:
        parts.append(cta)
    tags = " ".join(payload.get("hashtags") or [])
    if tags:
        parts.append(tags)
    if payload.get("rubric") in SELLING_RUBRICS and "инвестиционная рекомендация" not in parts[0]:
        parts.append(SELLING_DISCLAIMER)
    return "\n\n".join(p for p in parts if p)


# Fields kept when a draft snapshots the items it was written from.
# raw_text is dropped on purpose: it is up to 4000 characters per item and
# state/queue.json is committed to git on every run.
_SNAPSHOT_FIELDS = (
    "item_id", "source_id", "category", "title", "summary", "url",
    "canonical_url", "published_at", "collected_at", "lang",
)


def snapshot_items(items: list[NormalizedItem]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        data = item.to_dict()
        row = {key: data.get(key) for key in _SNAPSHOT_FIELDS}
        row["summary"] = (row.get("summary") or "")[:600]
        out.append(row)
    return out


def generate_for_rubric(
    settings: Settings,
    rubric: str,
    items: list[NormalizedItem],
    provider: Any = None,
    *,
    extra_instruction: str = "",
) -> PostDraft | None:
    if not items:
        log.info("Рубрика %s: нет подходящих материалов", rubric)
        return None

    meta = RUBRICS.get(rubric, {})
    max_chars = int(meta.get("max_chars", 900))
    provider = provider or get_provider(settings)
    system, user = prompts.build_prompt(rubric, items)
    if extra_instruction:
        user = f"{user}\n\n{extra_instruction}"

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = user if attempt == 1 else (
            user + "\n\nПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА ФАКТ-ЧЕК-ГЕЙТОМ:\n"
            + last_error
            + "\nИсправь и верни снова строго валидный JSON."
        )
        try:
            raw = provider.complete(system, prompt)
            probe = extract_json(raw)
            if probe.get("skip"):
                # Better no post than a stretched one: the model is allowed to
                # say the inputs do not carry a real story for this rubric.
                log.info("Рубрика %s: модель пропустила выпуск — %s",
                         rubric, str(probe.get("skip_reason") or "")[:200])
                return None
            payload = parse_response(raw)
        except LLMError as exc:
            last_error = str(exc)
            log.warning("Рубрика %s: попытка %d — %s", rubric, attempt, exc)
            continue

        payload["rubric"] = rubric
        payload["body"] = sanitize_telegram_html(payload["body"])
        verdict = factcheck.check(payload, items, max_chars=max_chars)
        if not verdict["passed"]:
            last_error = "; ".join(verdict["errors"])
            log.warning("Рубрика %s: факт-чек не пройден — %s", rubric, last_error)
            if attempt < MAX_ATTEMPTS:
                continue

        draft = PostDraft(
            post_id=_post_id(rubric, items),
            rubric=rubric,
            title=truncate(payload["title"], 90, ""),
            body=payload["body"],
            hashtags=payload["hashtags"],
            cta=payload["cta"],
            image=payload["image"],
            sources=payload["sources"],
            facts=payload["facts"],
            self_check=payload["self_check"],
            length_chars=len(payload["body"]),
            needs_separate_text=bool(payload["needs_separate_text"]),
            status="draft" if verdict["passed"] else "failed",
            created_at=datetime.now(timezone.utc).isoformat(),
            item_ids=[i.item_id for i in items],
            source_items=snapshot_items(items),
            gate=verdict,
        )
        if not verdict["passed"]:
            log.error("Рубрика %s: пост отправлен в брак", rubric)
        return draft

    log.error("Рубрика %s: не удалось получить валидный ответ модели (%s)", rubric, last_error)
    return None


def generate(
    settings: Settings,
    items: list[NormalizedItem],
    rubric_plan: list[str],
    provider: Any = None,
) -> list[PostDraft]:
    provider = provider or get_provider(settings)
    drafts: list[PostDraft] = []
    for rubric in rubric_plan:
        pool = select_for_rubric(items, rubric, limit=4)
        draft = generate_for_rubric(settings, rubric, pool, provider=provider)
        if draft and draft.status != "failed":
            drafts.append(draft)
        elif draft:
            log.info("Пост %s в браке, в очередь не попадёт", draft.post_id)
    return drafts
