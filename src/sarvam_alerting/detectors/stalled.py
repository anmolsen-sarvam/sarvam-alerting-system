"""Stalled / zero-dial campaign detection.

Catches a *silent* failure the per-campaign detectors miss by construction: a campaign that
is active with an uploaded cohort but is barely dialing (or not at all). Those campaigns
never clear the discovery call-threshold, so they'd otherwise be invisible.

This runs at scan level (not as a per-campaign Detector): it reads active campaigns from the
scheduling DB, then checks how many calls each made in the recent window.
"""

from __future__ import annotations

import logging

from ..clients.metabase import MetabaseClient, sql_str
from ..clients.scheduling import active_campaign_runs
from ..config import Config
from ..models import Finding, Severity

log = logging.getLogger("sarvam_alerting.detectors.stalled")


def _parse_dt(value):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def find_stalled_campaigns(metabase: MetabaseClient, config: Config) -> list[Finding]:
    """Return findings for active campaigns that have a cohort but aren't dialing."""
    opts = config.detectors.get("stalled_campaign", {})
    if not opts.get("enabled", True):
        return []
    min_cohort = int(opts.get("min_cohort", 50))          # cohort must be at least this big
    min_calls = int(opts.get("min_calls", 5))             # flag if fewer than this many calls
    grace_hours = float(opts.get("grace_hours", 2.0))     # ignore just-started campaigns
    window_hours = int(opts.get("window_hours", config.windows.current_hours))
    lookback_hours = int(opts.get("lookback_hours", 24))

    try:
        runs = active_campaign_runs(metabase, lookback_hours, tuple(config.discovery.statuses))
    except Exception:
        log.exception("stalled: scheduling lookup failed")
        return []

    # Candidates: active, cohort uploaded, started long enough ago, not ended, in scope.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    candidates: dict[str, dict] = {}
    for r in runs:
        if not r.campaign_id or r.valid_records < min_cohort:
            continue
        if not config.discovery.accepts(r.campaign_id, r.org_id):
            continue
        start = _parse_dt(r.start_timestamp)
        if start is not None:
            age_h = (now - start).total_seconds() / 3600.0
            if age_h < grace_hours:
                continue  # too fresh to judge
        if r.end_timestamp and _parse_dt(r.end_timestamp) and _parse_dt(r.end_timestamp) < now:
            continue  # already ended
        candidates[r.campaign_id] = {"org_id": r.org_id, "valid": r.valid_records, "name": r.name}
    if not candidates:
        return []

    id_list = ", ".join(sql_str(c) for c in candidates)
    sql = f"""
    SELECT campaign_id, count() AS calls
    FROM {metabase.table}
    WHERE campaign_id IN ({id_list})
      AND created_at_timestamp >= now() - INTERVAL {window_hours} HOUR
      AND is_deleted = 0
      AND is_debug_call = 0
    GROUP BY campaign_id
    """
    try:
        counts = {str(r["campaign_id"]): int(r["calls"]) for r in metabase.query(sql)}
    except Exception:
        log.exception("stalled: call-count query failed")
        return []

    findings: list[Finding] = []
    for cid, info in candidates.items():
        calls = counts.get(cid, 0)
        if calls >= min_calls:
            continue
        findings.append(
            Finding(
                detector="stalled_campaign",
                severity=Severity.CRITICAL,
                campaign_id=cid,
                title="Campaign active but not dialing",
                detail=(
                    f"`{cid}` is active with a cohort of {info['valid']:,} valid records, "
                    f"but only *{calls}* call(s) in the last {window_hours}h. Dialing looks "
                    f"stalled — check the campaign is actually running (scheduler / provider / "
                    f"concurrency)."
                ),
                metrics={"calls": calls, "cohort_valid": info["valid"], "window_hours": window_hours},
                org_id=info["org_id"],
                dedupe_key=f"{cid}:stalled_campaign",
            )
        )
    return findings
