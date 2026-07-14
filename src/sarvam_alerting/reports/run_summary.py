"""Run-summary report: what the automation is doing right now, per org.

Posts after each run: active cohorts, rows uploaded vs filtered, campaign IDs,
and retry windows -- pulled from the scheduling DB.
"""

from __future__ import annotations

from ..clients.metabase import MetabaseClient
from ..clients.scheduling import active_campaign_runs
from ..config import Config
from ..models import Report


def build_run_summary(mb: MetabaseClient, config: Config) -> Report | None:
    opts = config.reports.get("run_summary", {})
    lookback = int(opts.get("lookback_hours", 24))
    statuses = tuple(opts.get("statuses", ["active", "scheduled", "running"]))
    max_per_org = int(opts.get("max_campaigns_per_org", 15))

    runs = active_campaign_runs(mb, lookback, statuses)
    runs = [r for r in runs if config.discovery.accepts(r.campaign_id, r.org_id)]
    if not runs:
        return None

    by_org: dict[str, list] = {}
    for r in runs:
        by_org.setdefault(r.org_id, []).append(r)

    total_valid = sum(r.valid_records for r in runs)
    total_rejected = sum(r.rejected_records for r in runs)
    total_cohorts = sum(r.cohort_count for r in runs)

    sections: list[str] = [
        f"*{len(runs)}* campaign(s) across *{len(by_org)}* org(s) · "
        f"*{total_cohorts}* cohort(s) · rows uploaded *{total_valid:,}* · "
        f"filtered *{total_rejected:,}*"
    ]

    for org, items in sorted(by_org.items()):
        lines = [f"*{org or 'unknown-org'}*"]
        for r in items[:max_per_org]:
            retry = f"{r.max_retries or '-'} retries @ {r.retry_windows or '-'} min"
            lines.append(
                f"• `{r.campaign_id}` [{r.status}] — "
                f"cohorts {r.cohort_count}, "
                f"uploaded {r.valid_records:,}/{r.total_records:,}, "
                f"filtered {r.rejected_records:,} · {retry}"
            )
        extra = len(items) - max_per_org
        if extra > 0:
            lines.append(f"…and {extra} more")
        sections.append("\n".join(lines))

    return Report(kind="run_summary", title="Automation run summary", sections=sections)
