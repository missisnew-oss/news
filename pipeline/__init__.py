"""Content pipeline for a Russian-language Telegram channel about Dubai real estate.

Stages (see docs/ARCHITECTURE.md):
    collect -> normalize/dedupe -> score -> generate -> illustrate
            -> queue -> approve -> publish -> analytics
"""

__version__ = "1.0.0"
