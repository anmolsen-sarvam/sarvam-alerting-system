"""Orchestration: discover active campaigns, run detectors, collect findings."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime

from .clients.metabase import MetabaseClient, sql_str
from .config import Config
from .detectors import Detector, DetectorContext, build_detectors
from .models import CampaignInfo, Finding, Report
from .reports import build_cycle_report, build_run_summary

log = logging.getLogger("sarvam_alerting.engine")


def discover_campaigns(metabase: MetabaseClient, config: Config) -> list[CampaignInfo]:
    """Discover campaigns to check, via the configured source.

    "scheduling" (default) reads active campaigns from the small scheduling DB -- cheap,
    and picks up brand-new campaigns immediately (the launch-check case). "facts" scans
    EngagementFacts for campaigns above a call threshold (heavier; can hit gateway 504s).
    """
    if config.discovery.source == "scheduling":
        return _discover_from_scheduling(metabase, config)
    return _discover_from_facts(metabase, config)


def _discover_from_scheduling(metabase: MetabaseClient, config: Config) -> list[CampaignInfo]:
    """Hybrid: cheap active-campaign candidates from the scheduling DB, then a single
    ``campaign_id IN (...)`` count query on facts (bounded -> no full-table 504) to get
    real call counts and drop campaigns below ``min_calls``."""
    disc = config.discovery
    status_list = ", ".join(sql_str(s) for s in disc.statuses)
    candidates_sql = f"""
    SELECT DISTINCT campaign_id, org_id
    FROM {metabase.scheduling_table('campaigns')}
    WHERE status IN ({status_list})
      AND campaign_id IS NOT NULL AND campaign_id <> ''
    """
    rows = metabase.query(candidates_sql, database_id=metabase.scheduling_db)

    candidates: dict[str, str] = {}
    for r in rows:
        cid = str(r["campaign_id"])
        org = str(r.get("org_id") or "")
        if not disc.accepts(cid, org):
            continue
        candidates[cid] = org
    if not candidates:
        return []

    # Bounded count query on facts (filtered by the candidate ids, so it scans only those).
    id_list = ", ".join(sql_str(c) for c in candidates)
    counts_sql = f"""
    SELECT campaign_id, count() AS calls, max(created_at_timestamp) AS last_call
    FROM {metabase.table}
    WHERE campaign_id IN ({id_list})
      AND created_at_timestamp >= now() - INTERVAL {config.windows.current_hours} HOUR
      AND is_deleted = 0
      AND is_debug_call = 0
    GROUP BY campaign_id
    HAVING calls >= {disc.min_calls}
    ORDER BY calls DESC
    """
    counted = metabase.query(counts_sql)

    def parse_dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    campaigns: list[CampaignInfo] = []
    for r in counted:
        cid = str(r["campaign_id"])
        campaigns.append(
            CampaignInfo(
                campaign_id=cid,
                calls=int(r["calls"]),
                last_call=parse_dt(r.get("last_call")),
                org_id=candidates.get(cid, ""),
            )
        )
    return campaigns


def _discover_from_facts(metabase: MetabaseClient, config: Config) -> list[CampaignInfo]:
    """Find campaigns with enough recent activity by scanning EngagementFacts."""
    disc = config.discovery
    sql = f"""
    SELECT
        campaign_id,
        any(org_id)                AS org_id,
        count()                    AS calls,
        min(created_at_timestamp)  AS first_call,
        max(created_at_timestamp)  AS last_call,
        uniqExact(app_id)          AS apps
    FROM {metabase.table}
    WHERE created_at_timestamp >= now() - INTERVAL {config.windows.current_hours} HOUR
      AND is_deleted = 0
      AND is_debug_call = 0
    GROUP BY campaign_id
    HAVING calls >= {disc.min_calls}
    ORDER BY calls DESC
    """
    rows = metabase.query(sql)

    def parse_dt(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    campaigns: list[CampaignInfo] = []
    for r in rows:
        cid = str(r["campaign_id"])
        org = str(r.get("org_id") or "")
        if not disc.accepts(cid, org):
            continue
        campaigns.append(
            CampaignInfo(
                campaign_id=cid,
                calls=int(r["calls"]),
                first_call=parse_dt(r.get("first_call")),
                last_call=parse_dt(r.get("last_call")),
                apps=int(r.get("apps", 0)),
                org_id=org,
            )
        )
    return campaigns


def run_campaign(
    metabase: MetabaseClient,
    campaign: CampaignInfo,
    config: Config,
    detectors: list[Detector],
) -> list[Finding]:
    ctx = DetectorContext(metabase=metabase, campaign=campaign, config=config)
    findings: list[Finding] = []
    for detector in detectors:
        try:
            findings.extend(detector.run(ctx))
        except Exception:  # one bad detector shouldn't sink the whole run
            log.exception(
                "detector %s failed on campaign %s", detector.name, campaign.campaign_id
            )
    # Stamp the owning org so notifiers can route per-org.
    return [dataclasses.replace(f, org_id=f.org_id or campaign.org_id) for f in findings]


def run_scan(
    metabase: MetabaseClient,
    config: Config,
    only_campaign: str | None = None,
) -> tuple[list[Finding], list[CampaignInfo]]:
    """Full scan: returns (all findings, campaigns scanned)."""
    detectors = build_detectors(config.detectors)

    if only_campaign:
        campaigns = [CampaignInfo(campaign_id=only_campaign, calls=0)]
    else:
        campaigns = discover_campaigns(metabase, config)

    all_findings: list[Finding] = []
    for campaign in campaigns:
        log.info("scanning campaign %s (%s calls)", campaign.campaign_id, campaign.calls)
        all_findings.extend(run_campaign(metabase, campaign, config, detectors))
    return all_findings, campaigns


def build_reports(
    metabase: MetabaseClient,
    config: Config,
    campaigns: list[CampaignInfo],
) -> list[Report]:
    """Build the enabled digest reports (run summary, cycle report)."""
    reports: list[Report] = []

    if config.reports.get("run_summary", {}).get("enabled", False):
        try:
            summary = build_run_summary(metabase, config)
            if summary:
                reports.append(summary)
        except Exception:
            log.exception("run_summary report failed")

    if config.reports.get("cycle_report", {}).get("enabled", False):
        try:
            cycle = build_cycle_report(
                metabase, config, [c.campaign_id for c in campaigns]
            )
            if cycle:
                reports.append(cycle)
        except Exception:
            log.exception("cycle_report failed")

    return reports
