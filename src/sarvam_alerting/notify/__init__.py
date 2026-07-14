"""Notifiers: pluggable alert delivery behind a single interface."""

from __future__ import annotations

from ..config import NotifierConfig
from ..models import Severity
from ..owners import OwnerResolver
from .base import Notifier
from .console import ConsoleNotifier
from .slack_bot import SlackBotNotifier
from .slack_webhook import SlackWebhookNotifier


def build_notifiers(
    configs: tuple[NotifierConfig, ...],
    links: dict | None = None,
    owners: dict | None = None,
) -> list[Notifier]:
    resolver = OwnerResolver(owners)
    notifiers: list[Notifier] = []
    for cfg in configs:
        min_sev = Severity(cfg.min_severity)
        if cfg.type == "console":
            notifiers.append(ConsoleNotifier(min_sev, cfg.streams, links, resolver))
        elif cfg.type == "slack_webhook":
            notifiers.append(SlackWebhookNotifier(min_sev, cfg.options, cfg.streams, links, resolver))
        elif cfg.type == "slack_bot":
            notifiers.append(SlackBotNotifier(min_sev, cfg.options, cfg.streams, links, resolver))
        elif cfg.type == "gsheet":
            # Imported lazily: gspread is an optional dependency, only needed for this sink.
            from .gsheet import GSheetNotifier

            notifiers.append(GSheetNotifier(min_sev, cfg.options, cfg.streams, links, resolver))
        else:
            raise ValueError(f"Unknown notifier type: {cfg.type!r}")
    return notifiers


__all__ = [
    "Notifier",
    "ConsoleNotifier",
    "SlackWebhookNotifier",
    "SlackBotNotifier",
    "build_notifiers",
]
