"""Shared Slack message formatting (Block Kit)."""

from __future__ import annotations

from ..deeplinks import finding_links
from ..models import Finding, Report
from ..owners import OwnerResolver
from .base import group_by_campaign


def _evidence_line(f: Finding, links: dict) -> str:
    """A subtle one-liner of deep links / example call ids (rendered in a context block)."""
    parts: list[str] = []
    for label, url in finding_links(f, links):
        parts.append(f"<{url}|{label}>")
    if f.interaction_ids and not any("call " in p for p in parts):
        parts.append("calls " + ", ".join(f"`{i[:8]}`" for i in f.interaction_ids[:3]))
    return (":mag: " + "  ·  ".join(parts)) if parts else ""


def campaign_blocks(
    campaign_id: str,
    items: list[Finding],
    links: dict | None = None,
    owners: OwnerResolver | None = None,
) -> list[dict]:
    """Blocks for a single campaign's findings: heading · findings · evidence · owners."""
    links = links or {}
    worst = max(items, key=lambda f: f.severity.rank)
    org = items[0].org_id if items else ""
    n = len(items)

    heading = f"{worst.severity.emoji}  *{campaign_id}*"
    if org:
        heading += f"   ·   `{org}`"
    heading += f"\n_{n} issue{'s' if n != 1 else ''} detected · last 6h_"
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}]

    for f in items:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{f.severity.emoji} *{f.title}*\n{f.detail}"},
        })
        ev = _evidence_line(f, links)
        if ev:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ev}]})

    if owners is not None:
        owner_line = owners.line_for(org, campaign_id, worst.severity.rank)
        if owner_line:  # a real section (not context) so the mention actually pings
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": owner_line}})
    return blocks


def build_blocks(
    findings: list[Finding],
    meta: dict,
    links: dict | None = None,
    owners: OwnerResolver | None = None,
) -> tuple[str, list[dict]]:
    """Multi-campaign message (used by the webhook notifier)."""
    links = links or {}
    grouped = group_by_campaign(findings)
    n, m = len(findings), len(grouped)
    header = f":rotating_light: *{n} alert{'s' if n != 1 else ''}* across {m} campaign{'s' if m != 1 else ''}"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]
    for campaign_id, items in grouped.items():
        blocks.extend(campaign_blocks(campaign_id, items, links, owners))
        blocks.append({"type": "divider"})

    scanned = meta.get("campaigns_scanned", 0)
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"Scanned {scanned} campaign(s)  ·  sarvam-alerting"}],
    })
    fallback = f"{n} campaign alert(s): " + "; ".join(f.title for f in findings[:5])
    return fallback, blocks


def build_campaign_blocks(
    campaign_id: str,
    items: list[Finding],
    links: dict | None = None,
    owners: OwnerResolver | None = None,
) -> tuple[str, list[dict]]:
    """Single-campaign message (used by the bot notifier; buttons appended by the caller)."""
    blocks = campaign_blocks(campaign_id, items, links, owners)
    fallback = f"{len(items)} alert(s) on {campaign_id}: " + "; ".join(f.title for f in items[:5])
    return fallback, blocks


def alert_action_blocks(campaign_id: str, org_id: str = "", links: dict | None = None) -> dict:
    """An actions block of buttons for one campaign's alert (bot-token messages only)."""
    links = links or {}
    elements: list[dict] = [
        {"type": "button", "text": {"type": "plain_text", "text": "Ack"},
         "action_id": "alert_ack", "value": campaign_id},
        {"type": "button", "text": {"type": "plain_text", "text": "Snooze 4h"},
         "action_id": "alert_snooze", "value": campaign_id},
        {"type": "button", "text": {"type": "plain_text", "text": "Mute"},
         "style": "danger", "action_id": "alert_mute", "value": campaign_id},
    ]
    tmpl = links.get("campaign_dashboard_url_template")
    if tmpl:
        try:
            elements.append({
                "type": "button", "text": {"type": "plain_text", "text": "Open in Metabase"},
                "url": tmpl.format(campaign_id=campaign_id, org_id=org_id),
                "action_id": "open_metabase",
            })
        except Exception:  # noqa: BLE001 - a bad template must not break alerting
            pass
    return {"type": "actions", "elements": elements}


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
