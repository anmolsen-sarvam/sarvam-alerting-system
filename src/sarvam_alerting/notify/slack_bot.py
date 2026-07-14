"""Slack bot-token notifier (chat.postMessage -> any channel).

Supports per-org and per-campaign channel routing for alerts, and a dedicated
report channel (the "automation alerts" channel) for digests.
"""

from __future__ import annotations

import os

import httpx

from ..models import Finding, Report, Severity
from ..owners import OwnerResolver
from .base import Notifier, group_by_campaign
from .slack_format import alert_action_blocks, build_campaign_blocks, build_report_blocks

_API = "https://slack.com/api/chat.postMessage"


class SlackBotNotifier(Notifier):
    def __init__(
        self,
        min_severity: Severity,
        options: dict,
        streams: tuple[str, ...] = ("alerts",),
        links: dict | None = None,
        owners: OwnerResolver | None = None,
    ):
        super().__init__(min_severity, streams, links, owners)
        token_env = options.get("token_env", "SLACK_BOT_TOKEN")
        self._token = os.environ.get(token_env, "").strip()
        if not self._token:
            raise ValueError(
                f"slack_bot notifier: env {token_env!r} is not set. See .env.example."
            )
        self._default_channel = options.get("default_channel")
        if not self._default_channel:
            raise ValueError("slack_bot notifier: 'default_channel' is required.")
        self._report_channel = options.get("report_channel", self._default_channel)
        self._channels: dict[str, str] = dict(options.get("channels", {}))      # per campaign
        self._org_channels: dict[str, str] = dict(options.get("org_channels", {}))  # per org

    def _post(
        self, channel: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None
    ) -> str | None:
        payload: dict = {"channel": channel, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        resp = httpx.post(
            _API,
            headers={"Authorization": f"Bearer {self._token}"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Slack API error: {body.get('error')}")
        return body.get("ts")

    def _channel_for(self, campaign_id: str, org_id: str) -> str:
        return (
            self._channels.get(campaign_id)
            or self._org_channels.get(org_id)
            or self._default_channel
        )

    def _emit(self, findings: list[Finding], meta: dict) -> None:
        if not findings:
            return
        # One tidy message per campaign: heading · findings · evidence · owner · buttons.
        for campaign_id, items in group_by_campaign(findings).items():
            org_id = items[0].org_id if items else ""
            channel = self._channel_for(campaign_id, org_id)
            fallback, blocks = build_campaign_blocks(campaign_id, items, self.links, self.owners)
            blocks.append(alert_action_blocks(campaign_id, org_id, self.links))
            self._post(channel, fallback, blocks)

    def _emit_report(self, report: Report) -> None:
        self._post(self._report_channel, *build_report_blocks(report))

    def _emit_recovery(self, recoveries: list[dict]) -> None:
        lines = [f":white_check_mark: `{r['campaign_id']}` — {r['title']} has cleared."
                 for r in recoveries]
        text = "*Recovered*\n" + "\n".join(lines)
        self._post(self._default_channel, text,
                   [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])

    def _emit_escalation(self, items: list[dict]) -> None:
        by_campaign: dict[str, list[dict]] = {}
        for it in items:
            by_campaign.setdefault(it["campaign_id"], []).append(it)
        for campaign_id, its in by_campaign.items():
            org_id = its[0].get("org_id", "")
            channel = self._channel_for(campaign_id, org_id)
            titles = "\n".join(f"• *{i['title']}*" for i in its)
            text = (
                f":arrow_up: :rotating_light: *Still unresolved* on `{campaign_id}`"
                + (f" · `{org_id}`" if org_id else "")
                + f"\n{titles}\n_No one has acknowledged this — please take a look._"
            )
            # Re-ping owners regardless of the min-severity gate (this is already critical).
            if self.owners is not None:
                mentions = self.owners.mentions_for(org_id, campaign_id, Severity.CRITICAL.rank)
                if mentions:
                    text += "\n" + " ".join(mentions)
            self._post(channel, text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
