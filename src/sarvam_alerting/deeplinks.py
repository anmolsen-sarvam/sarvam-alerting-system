"""Build deep links + evidence for a finding, from config templates.

Templates are formatted with ``campaign_id``, ``org_id`` and (for calls) ``interaction_id``.
Everything is optional -- if no templates are configured, only the raw evidence ids show.

Example config:
    [links]
    campaign_dashboard_url_template = "https://metabase.sarvam.ai/dashboard/42?campaign_id={campaign_id}"
    call_url_template = "https://agents.sarvam.ai/interactions/{interaction_id}"
"""

from __future__ import annotations

from .models import Finding


def finding_links(finding: Finding, links_cfg: dict) -> list[tuple[str, str]]:
    """Return a list of (label, url) deep links for a finding."""
    out: list[tuple[str, str]] = []
    dash = links_cfg.get("campaign_dashboard_url_template")
    if dash:
        try:
            out.append(("Open in Metabase", dash.format(campaign_id=finding.campaign_id, org_id=finding.org_id)))
        except (KeyError, IndexError):
            pass
    call_tmpl = links_cfg.get("call_url_template")
    if call_tmpl:
        for iid in finding.interaction_ids[:3]:
            try:
                out.append((f"call {iid[:8]}", call_tmpl.format(interaction_id=iid)))
            except (KeyError, IndexError):
                pass
    return out
