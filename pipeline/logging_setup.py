"""Logging with secret redaction.

Any value listed in Settings.secret_values is replaced with ``***`` before a
record reaches a handler, so a stack trace or a debug dump can never leak a
token into GitHub Actions logs.
"""

from __future__ import annotations

import logging
import sys


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
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
