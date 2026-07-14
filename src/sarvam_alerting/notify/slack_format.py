"""Shared Slack message formatting (Block Kit)."""

from __future__ import annotations

from ..deeplinks import finding_links
from ..models import Finding, Report
from .base import group_by_campaign


def _evidence_line(f: Finding, links: dict) -> str:
    parts: list[str] = []
    for label, url in finding_links(f, links):
        parts.append(f"<{url}|{label}>")
    if f.interaction_ids and not any("call " in p for p in parts):
        parts.append("calls: " + ", ".join(f"`{i[:8]}`" for i in f.interaction_ids[:3]))
    return ("   " + " · ".join(parts)) if parts else ""


def build_blocks(findings: list[Finding], meta: dict, links: dict | None = None) -> tuple[str, list[dict]]:
    """Return (fallback_text, blocks) for a Slack message."""
    links = links or {}
    n = len(findings)
    header = f"{n} campaign alert{'s' if n != 1 else ''} :rotating_light:"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
    ]

    for campaign_id, items in group_by_campaign(findings).items():
        lines = [f"*Campaign* `{campaign_id}`"]
        for f in items:
            block = f"{f.severity.emoji} *{f.title}*\n{f.detail}"
            ev = _evidence_line(f, links)
            if ev:
                block += f"\n{ev}"
            lines.append(block)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(lines)}}
        )
        blocks.append({"type": "divider"})

    scanned = meta.get("campaigns_scanned", 0)
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Scanned {scanned} campaign(s) · sarvam-alerting",
                }
            ],
        }
    )
    fallback = f"{n} campaign alert(s): " + "; ".join(f.title for f in findings[:5])
    return fallback, blocks


def build_report_blocks(report: Report) -> tuple[str, list[dict]]:
    """Return (fallback_text, blocks) for a report digest."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": report.title}},
    ]
    for section in report.sections:
        # Slack section text caps around 3000 chars; keep each section safe.
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": section[:2900]}}
        )
    return report.title, blocks
