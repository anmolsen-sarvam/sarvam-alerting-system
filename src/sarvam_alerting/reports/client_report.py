"""Weekly per-use-case (per-org) client report.

Generates a shareable markdown report per org combining the week's **performance funnel**
(from EngagementFacts) and **eval quality** (from the insights pipeline). This is the
foundation for the "2 evals reports per use-case per week" client deliverable -- one file
per org, ready to review/convert/share.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from ..clients.metabase import MetabaseClient, sql_str
from ..config import Config
from ..models import CampaignInfo

_PTP_TRUE = "('True', 'true', 'TRUE', 'Yes', 'yes', '1')"
_PTP_DATE_EMPTY = "('', 'null', 'None', 'NA')"
_BOOL_METRICS = ["hallucination", "loop_detection", "escalation_miss", "harassing_language"]
_SCORE_METRICS = ["call_effectiveness", "confidence_disp_accuracy_score"]


def _pct(num: int, den: int) -> str:
    return f"{(num / den * 100):.1f}%" if den else "-"


def build_client_reports(
    mb: MetabaseClient,
    config: Config,
    campaigns: list[CampaignInfo],
) -> list[str]:
    """Write one markdown report per org; return the file paths."""
    opts = config.reports.get("client_report", {})
    days = int(opts.get("days", 7))
    out_dir = Path(opts.get("output_dir", "client-reports")).expanduser()
    s3_prefix = str(opts.get("s3_prefix", "")).strip()  # e.g. s3://bucket/client-reports

    campaigns = [c for c in campaigns if c.campaign_id]
    if not campaigns:
        return []
    org_of = {c.campaign_id: (c.org_id or "unknown-org") for c in campaigns}
    id_list = ", ".join(sql_str(c.campaign_id) for c in campaigns)

    # Performance funnel, grouped by org (org_id is native on facts).
    perf_sql = f"""
    SELECT org_id,
        count()                                                  AS total,
        countIf(v2v_connectivity_status = 'connected')           AS connected,
        countIf(v2v_connectivity_status = 'connected' AND num_messages >= 2) AS engaged,
        countIf(completion_status = 'completed')                 AS completed,
        countIf(on_end_agent_variables['promised_to_pay'] IN {_PTP_TRUE}
                OR on_end_agent_variables['promised_to_pay_date'] NOT IN {_PTP_DATE_EMPTY}) AS ptp
    FROM {mb.table}
    WHERE campaign_id IN ({id_list})
      AND created_at_timestamp >= now() - INTERVAL {days} DAY
      AND is_deleted = 0
      AND is_debug_call = 0
    GROUP BY org_id
    """
    perf = {str(r["org_id"]): r for r in mb.query(perf_sql)}

    # Eval quality per campaign (mapped to org in Python).
    metric_list = ", ".join(sql_str(m) for m in _BOOL_METRICS + _SCORE_METRICS)
    qual_sql = f"""
    SELECT r.campaign_id AS campaign_id, r.metric_name AS metric_name,
           round(avg(r.boolean_value::int)::numeric, 3) AS brate,
           round(avg(r.numeric_value)::numeric, 2)      AS nval,
           count(*) AS n
    FROM {mb.evals_table('insights_result')} r
    JOIN {mb.evals_table('insights_run')} ir ON ir.id = r.run_id
    WHERE r.campaign_id IN ({id_list})
      AND ir.last_run_at >= now() - INTERVAL '{days} days'
      AND r.metric_name IN ({metric_list})
    GROUP BY r.campaign_id, r.metric_name
    """
    # org -> metric -> list of values
    qual: dict[str, dict[str, list[float]]] = {}
    for r in mb.query(qual_sql, database_id=mb.evals_db):
        org = org_of.get(str(r["campaign_id"]), "unknown-org")
        metric = str(r["metric_name"])
        val = r.get("brate") if metric in _BOOL_METRICS else r.get("nval")
        if val is None:
            continue
        qual.setdefault(org, {}).setdefault(metric, []).append(float(val))

    out_dir.mkdir(parents=True, exist_ok=True)
    week = _dt.date.today().isocalendar()
    stamp = f"{week[0]}W{week[1]:02d}"
    generated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # org -> list of campaign ids
    orgs: dict[str, list[str]] = {}
    for c in campaigns:
        orgs.setdefault(c.org_id or "unknown-org", []).append(c.campaign_id)

    paths: list[str] = []
    for org, cids in sorted(orgs.items()):
        p = perf.get(org)
        lines = [
            f"# Weekly QC / Evals Report — {org}",
            "",
            f"_Week {stamp} · last {days} days · generated {generated}_",
            "",
            "## Performance",
            "",
        ]
        if p:
            total = int(p["total"])
            connected = int(p["connected"])
            lines += [
                f"- **Calls dialed:** {total:,}",
                f"- **Connectivity:** {_pct(connected, total)} ({connected:,})",
                f"- **Engagement (of connected):** {_pct(int(p['engaged']), connected)}",
                f"- **Completion:** {_pct(int(p['completed']), total)}",
                f"- **PTP (of connected):** {_pct(int(p['ptp']), connected)}",
            ]
        else:
            lines.append("- No call activity in the window.")

        lines += ["", "## Conversation quality (insights)", ""]
        oq = qual.get(org, {})
        if oq:
            def avg(m):
                vals = oq.get(m, [])
                return sum(vals) / len(vals) if vals else None

            for m in _BOOL_METRICS:
                a = avg(m)
                if a is not None:
                    lines.append(f"- **{m}:** {a * 100:.1f}%")
            for m in _SCORE_METRICS:
                a = avg(m)
                if a is not None:
                    lines.append(f"- **{m}:** {a:.2f}")
        else:
            lines.append("- No eval data in the window.")

        lines += [
            "",
            "## Campaigns included",
            "",
            *[f"- `{cid}`" for cid in cids],
            "",
        ]

        safe_org = org.replace("/", "_").replace(" ", "_")
        filename = f"{safe_org}-{stamp}.md"
        path = out_dir / filename
        path.write_text("\n".join(lines))
        if s3_prefix:
            paths.append(_upload_s3(str(path), s3_prefix, f"{stamp}/{filename}"))
        else:
            paths.append(str(path))

    return paths


def _upload_s3(local_path: str, s3_prefix: str, key_suffix: str) -> str:
    """Upload a report to S3 (so it survives ephemeral Airflow pods). Returns the s3 URL."""
    import urllib.parse

    import boto3

    parsed = urllib.parse.urlparse(s3_prefix)
    bucket = parsed.netloc
    base_key = parsed.path.strip("/")
    key = f"{base_key}/{key_suffix}" if base_key else key_suffix
    boto3.client("s3").upload_file(local_path, bucket, key)
    return f"s3://{bucket}/{key}"
