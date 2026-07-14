"""Slack incoming-webhook notifier (one channel per webhook URL)."""

from __future__ import annotations

import os

import httpx

from ..models import Finding, Report, Severity
from .base import Notifier
from .slack_format import build_blocks, build_report_blocks


class SlackWebhookNotifier(Notifier):
    def __init__(
        self,
        min_severity: Severity,
        options: dict,
        streams: tuple[str, ...] = ("alerts",),
        links: dict | None = None,
    ):
        super().__init__(min_severity, streams, links)
        url_env = options.get("url_env", "SLACK_WEBHOOK_URL")
        self._url = os.environ.get(url_env, "").strip()
        if not self._url:
            raise ValueError(
                f"slack_webhook notifier: env {url_env!r} is not set. See .env.example."
            )

    def _post(self, text: str, blocks: list[dict]) -> None:
        resp = httpx.post(self._url, json={"text": text, "blocks": blocks}, timeout=15)
        resp.raise_for_status()

    def _emit(self, findings: list[Finding], meta: dict) -> None:
        if not findings:
            return  # webhook stays quiet on clean runs
        self._post(*build_blocks(findings, meta, self.links))

    def _emit_report(self, report: Report) -> None:
        self._post(*build_report_blocks(report))
