"""Logging with secret redaction.

Any value listed in Settings.secret_values is replaced with ``***`` before a
record reaches a handler, so a stack trace or a debug dump can never leak a
token into GitHub Actions logs.
"""

from __future__ import annotations

import logging
import re
import sys

# Credential shapes, redacted even when the exact value was never registered.
# The bot token is the important one: requests puts the full request URL —
# which contains the token — into its exception message, and that exception
# is logged on every network hiccup in pipeline/telegram.py.
SECRET_PATTERNS = (
    re.compile(r"\bbot\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}"),
)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, "***")
        for pattern in SECRET_PATTERNS:
            text = pattern.sub("***", text)
        return text

    def _scrub_any(self, value):
        """Scrub anything that will end up in the message via %s.

        Exceptions were previously passed through untouched, so a
        ``requests`` error carrying ``https://api.telegram.org/bot<TOKEN>/…``
        printed the live bot token into the GitHub Actions log.
        """
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, BaseException):
            return self._scrub(f"{type(value).__name__}: {value}")
        text = str(value)
        scrubbed = self._scrub(text)
        return scrubbed if scrubbed != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        elif record.msg is not None:
            record.msg = self._scrub_any(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub_any(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._scrub_any(a) for a in record.args)
        if record.exc_info and record.exc_info[1] is not None:
            # A traceback's exception message can carry the token too.
            record.exc_text = self._scrub(
                record.exc_text or f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            )
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")
    )
    handler.addFilter(RedactingFilter(secrets or []))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("pipeline")
