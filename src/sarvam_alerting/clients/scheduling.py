"""Scheduling Service DB (Postgres) access: campaigns, cohorts, transformations.

Feeds the run-summary report (cohorts / rows / retry windows) and enriches the
default-variable detector with each app's ``required`` variable set.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metabase import MetabaseClient, sql_str


@dataclass(frozen=True)
class CampaignRun:
    org_id: str
    campaign_id: str
    name: str
    status: str
    app_id: str
    app_version: int | None
    start_timestamp: str | None
    end_timestamp: str | None
    max_retries: str | None
    retry_windows: str | None
    cohort_count: int
    total_records: int
    valid_records: int
    rejected_records: int


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def active_campaign_runs(
    mb: MetabaseClient,
    lookback_hours: int,
    statuses: tuple[str, ...] = ("active", "scheduled", "running"),
) -> list[CampaignRun]:
    """Campaigns that are active or were updated within the lookback window,
    with their cohort upload stats and retry configuration."""
    status_list = ", ".join(sql_str(s) for s in statuses)
    campaigns = mb.scheduling_table("campaigns")
    cohorts = mb.scheduling_table("cohorts")
    sql = f"""
    SELECT
        c.org_id, c.campaign_id, c.name, c.status, c.app_id, c.app_version,
        c.start_timestamp, c.end_timestamp,
        COALESCE(c.app_config->'retry_config'->>'max_retries',
                 c.retry_config->>'max_retries')               AS max_retries,
        COALESCE(c.app_config->'retry_config'->>'retry_interval_minutes',
                 c.retry_config->>'retry_within_mins')          AS retry_windows,
        COALESCE(agg.cohort_count, 0)                           AS cohort_count,
        COALESCE(agg.total_records, 0)                          AS total_records,
        COALESCE(agg.valid_records, 0)                          AS valid_records,
        COALESCE(agg.rejected_records, 0)                       AS rejected_records
    FROM {campaigns} c
    LEFT JOIN (
        SELECT campaign_internal_id,
               count(*)                                          AS cohort_count,
               sum((result->>'total_records')::numeric)          AS total_records,
               sum((result->>'valid_records')::numeric)          AS valid_records,
               sum((result->>'rejected_records')::numeric)       AS rejected_records
        FROM {cohorts}
        GROUP BY campaign_internal_id
    ) agg ON agg.campaign_internal_id = c.internal_id
    WHERE c.status IN ({status_list})
       OR c.updated_at >= now() - INTERVAL '{int(lookback_hours)} hours'
    ORDER BY c.org_id, c.start_timestamp DESC
    """
    rows = mb.query(sql, database_id=mb.scheduling_db)
    runs: list[CampaignRun] = []
    for r in rows:
        runs.append(
            CampaignRun(
                org_id=str(r.get("org_id") or ""),
                campaign_id=str(r.get("campaign_id") or ""),
                name=str(r.get("name") or ""),
                status=str(r.get("status") or ""),
                app_id=str(r.get("app_id") or ""),
                app_version=r.get("app_version"),
                start_timestamp=r.get("start_timestamp"),
                end_timestamp=r.get("end_timestamp"),
                max_retries=r.get("max_retries"),
                retry_windows=r.get("retry_windows"),
                cohort_count=_to_int(r.get("cohort_count")),
                total_records=_to_int(r.get("total_records")),
                valid_records=_to_int(r.get("valid_records")),
                rejected_records=_to_int(r.get("rejected_records")),
            )
        )
    return runs


def campaign_cohort_totals(mb: MetabaseClient, campaign_id: str) -> tuple[int, int, int]:
    """Return (total, valid, rejected) uploaded records for a campaign. Zeros if none."""
    campaigns = mb.scheduling_table("campaigns")
    cohorts = mb.scheduling_table("cohorts")
    sql = f"""
    SELECT
        COALESCE(sum((co.result->>'total_records')::numeric), 0)    AS total,
        COALESCE(sum((co.result->>'valid_records')::numeric), 0)    AS valid,
        COALESCE(sum((co.result->>'rejected_records')::numeric), 0) AS rejected
    FROM {campaigns} c
    JOIN {cohorts} co ON co.campaign_internal_id = c.internal_id
    WHERE c.campaign_id = {sql_str(campaign_id)}
    """
    rows = mb.query(sql, database_id=mb.scheduling_db)
    if not rows:
        return 0, 0, 0
    r = rows[0]
    return _to_int(r.get("total")), _to_int(r.get("valid")), _to_int(r.get("rejected"))


def required_variables(
    mb: MetabaseClient, app_id: str, app_version: int | None
) -> dict[str, dict]:
    """Return {variable: {"required": bool, "fallback": str|None}} for an app's
    most-recent cohort transformation. Empty dict if none found."""
    if not app_id:
        return {}
    ct = mb.scheduling_table("cohort_transformations")
    version_clause = (
        f"AND app_version = {int(app_version)}" if app_version is not None else ""
    )
    sql = f"""
    WITH latest AS (
        SELECT transformation_config
        FROM {ct}
        WHERE app_id = {sql_str(app_id)} {version_clause}
        ORDER BY created_at DESC
        LIMIT 1
    )
    SELECT kv.key                              AS variable,
           (kv.value->>'required')::boolean    AS required,
           kv.value->>'fallback_value'         AS fallback
    FROM latest,
         jsonb_each(latest.transformation_config::jsonb->'agent_variables') kv
    """
    try:
        rows = mb.query(sql, database_id=mb.scheduling_db)
    except Exception:
        return {}
    return {
        str(r["variable"]): {
            "required": bool(r.get("required")),
            "fallback": r.get("fallback"),
        }
        for r in rows
    }
