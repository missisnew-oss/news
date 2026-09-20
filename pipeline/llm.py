"""LLM provider abstraction.

Anthropic is the primary provider. OpenAI plugs in through the same
``LLMProvider`` interface, so switching is a config change
(``LLM_PROVIDER=openai``) rather than a rewrite. In DRY_RUN a deterministic
offline provider is used and no network call is made at all.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

from .config import Settings

log = logging.getLogger("pipeline.llm")

DEFAULT_MAX_TOKENS = 2000
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY не задан")
        self.model = model
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise LLMError("Не установлен пакет anthropic (pip install -r requirements.txt)") from exc
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return "".join(
                    block.text for block in response.content if getattr(block, "type", "") == "text"
                )
            except Exception as exc:
                last_error = exc
                delay = 2 ** attempt
                log.warning("Anthropic: попытка %d не удалась (%s), пауза %ds", attempt + 1, exc, delay)
                time.sleep(delay)
        raise LLMError(f"Anthropic недоступен: {last_error}")


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMError("OPENAI_API_KEY не задан")
        self.model = model
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError("Не установлен пакет openai (раскомментируйте его в requirements.txt)") from exc
        self._client = OpenAI(api_key=api_key)

    def complete(self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                last_error = exc
                delay = 2 ** attempt
                log.warning("OpenAI: попытка %d не удалась (%s), пауза %ds", attempt + 1, exc, delay)
                time.sleep(delay)
        raise LLMError(f"OpenAI недоступен: {last_error}")


class OfflineProvider:
    """Deterministic stand-in used by DRY_RUN and by tests. Never touches the network."""

    name = "offline"

    def __init__(self, model: str = "offline") -> None:
        self.model = model

    def complete(self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        from .offline_draft import build_offline_draft

        return json.dumps(build_offline_draft(user), ensure_ascii=False)


def get_provider(settings: Settings) -> LLMProvider:
    if settings.dry_run:
        log.info("DRY_RUN: используется офлайн-провайдер LLM, сетевых вызовов не будет")
        return OfflineProvider(settings.llm_model)
    if settings.llm_provider == "openai":
        return OpenAIProvider(settings.openai_api_key, settings.openai_model)
    return AnthropicProvider(settings.anthropic_api_key, settings.llm_model)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

REQUIRED_KEYS = (
    "rubric", "title", "body", "hashtags", "cta", "image",
    "sources", "facts", "self_check", "length_chars", "needs_separate_text",
)
REQUIRED_SELF_CHECK = (
    "all_numbers_have_source", "no_guaranteed_returns", "language_is_russian",
    "no_verbatim_copy", "fits_telegram_limit",
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Tolerates the three failure modes seen in practice: a ```json fence, chatty
    text around the object, and trailing prose after the closing brace.
    """
    if not text or not text.strip():
        raise LLMError("Пустой ответ модели")
    candidate = text.strip()

    fence = _FENCE_RE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    if start == -1:
        raise LLMError("В ответе модели нет JSON-объекта")
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(candidate)):
        char = candidate[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = candidate[start: idx + 1]
                try:
                    return json.loads(block)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Не удалось разобрать JSON: {exc}") from exc
    raise LLMError("JSON-объект в ответе модели не закрыт")


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Check the LLM response against the agreed schema and coerce types."""
    if not isinstance(payload, dict):
        raise LLMError("Ответ модели не является объектом")
    missing = [key for key in REQUIRED_KEYS if key not in payload]
    if missing:
        raise LLMError("В ответе модели нет обязательных полей: " + ", ".join(missing))

    if not isinstance(payload.get("title"), str) or not payload["title"].strip():
        raise LLMError("Поле title пустое или не строка")
    if not isinstance(payload.get("body"), str) or not payload["body"].strip():
        raise LLMError("Поле body пустое или не строка")

    payload["hashtags"] = [str(h) for h in (payload.get("hashtags") or []) if str(h).strip()]
    payload["cta"] = str(payload.get("cta") or "")

    image = payload.get("image")
    if not isinstance(image, dict):
        image = {}
    image.setdefault("mode", "card")
    image.setdefault("headline", payload["title"][:60])
    image.setdefault("accent", "")
    image.setdefault("stock_query", "dubai skyline real estate")
    payload["image"] = image

    payload["sources"] = [s for s in (payload.get("sources") or []) if isinstance(s, dict) and s.get("url")]
    payload["facts"] = [f for f in (payload.get("facts") or []) if isinstance(f, dict)]

    self_check = payload.get("self_check")
    if not isinstance(self_check, dict):
        raise LLMError("Поле self_check отсутствует или неверного типа")
    for flag in REQUIRED_SELF_CHECK:
        if flag not in self_check:
            raise LLMError(f"В self_check нет флага {flag}")
        self_check[flag] = bool(self_check[flag])
    payload["self_check"] = self_check

    try:
        payload["length_chars"] = int(payload.get("length_chars") or 0)
    except (TypeError, ValueError):
        payload["length_chars"] = 0
    payload["needs_separate_text"] = bool(payload.get("needs_separate_text"))
    return payload


def parse_response(text: str) -> dict[str, Any]:
    return validate_payload(extract_json(text))
