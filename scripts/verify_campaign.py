"""Run every alerting functionality against one campaign and write a markdown report.

Usage:
    uv run python scripts/verify_campaign.py <campaign_id> [org_id] [output.md]

Exercises: discovery/scheduling info, all detectors (check), cycle report, conversationality
review (LLM), value-correctness (LLM), weekly-evals (insights), and the client report.
"""

from __future__ import annotations

import datetime as _dt
import sys

from sarvam_alerting.clients.llm import LLMClient
from sarvam_alerting.clients.metabase import MetabaseClient, sql_str
from sarvam_alerting.config import load_config
from sarvam_alerting.detectors import build_detectors
from sarvam_alerting.engine import run_campaign
from sarvam_alerting.models import CampaignInfo
from sarvam_alerting.reports import (
    build_client_reports,
    build_conversationality_review,
    build_cycle_report,
    build_value_correctness_review,
)


def main() -> None:
    cid = sys.argv[1]
    org = sys.argv[2] if len(sys.argv) > 2 else ""
    out = sys.argv[3] if len(sys.argv) > 3 else "verification/verification.md"

    cfg = load_config()
    md: list[str] = []
    w = md.append

    w(f"# Alerting verification — `{cid}`")
    w("")
    w(f"- **Org:** {org or '(unknown)'}")
    w(f"- **Generated:** {_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d %H:%M UTC}")
    w(f"- **Windows:** current {cfg.windows.current_hours}h vs baseline {cfg.windows.baseline_hours}h")
    w("")

    mb = MetabaseClient(cfg.metabase)
    try:
        # ---- 1. Discovery / call volume ----
        w("## 1. Discovery (call volume in current window)")
        w("")
        try:
            rows = mb.query(
                f"SELECT count() n, min(created_at_timestamp) first, max(created_at_timestamp) last, "
                f"uniqExact(app_id) apps FROM {mb.table} WHERE campaign_id={sql_str(cid)} "
                f"AND created_at_timestamp >= now()-INTERVAL {cfg.windows.current_hours} HOUR AND is_deleted=0"
            )
            r = rows[0] if rows else {}
            calls = int(r.get("n") or 0)
            w(f"- Calls (last {cfg.windows.current_hours}h): **{calls:,}**")
            w(f"- First / last: {r.get('first')} / {r.get('last')}")
            w(f"- Distinct apps: {r.get('apps')}")
        except Exception as e:
            calls = 0
            w(f"_error: {e}_")
        w("")

        # ---- 2. Run-summary equivalent (scheduling: cohorts / rows / retry) ----
        w("## 2. Run summary (scheduling: cohorts, rows uploaded/filtered, retry windows)")
        w("")
        try:
            rs = mb.query(
                f"""
                SELECT c.status, c.app_id, c.app_version, c.start_timestamp, c.end_timestamp,
                    COALESCE(c.app_config->'retry_config'->>'max_retries', c.retry_config->>'max_retries') AS max_retries,
                    COALESCE(c.app_config->'retry_config'->>'retry_interval_minutes', c.retry_config->>'retry_within_mins') AS retry_windows,
                    COALESCE(sum((co.result->>'total_records')::numeric),0) AS total_records,
                    COALESCE(sum((co.result->>'valid_records')::numeric),0) AS valid_records,
                    COALESCE(sum((co.result->>'rejected_records')::numeric),0) AS rejected_records,
                    count(co.internal_id) AS cohorts
                FROM {mb.scheduling_table('campaigns')} c
                LEFT JOIN {mb.scheduling_table('cohorts')} co ON co.campaign_internal_id = c.internal_id
                WHERE c.campaign_id = {sql_str(cid)}
                GROUP BY c.status, c.app_id, c.app_version, c.start_timestamp, c.end_timestamp, max_retries, retry_windows
                """,
                database_id=mb.scheduling_db,
            )
            if rs:
                r = rs[0]
                w(f"- **Status:** {r.get('status')}  ·  app_version {r.get('app_version')}")
                w(f"- **Window:** {r.get('start_timestamp')} → {r.get('end_timestamp')}")
                w(f"- **Cohorts:** {int(float(r.get('cohorts') or 0))}")
                w(f"- **Rows uploaded (valid/total):** {int(float(r.get('valid_records') or 0)):,} / {int(float(r.get('total_records') or 0)):,}")
                w(f"- **Rows filtered (rejected):** {int(float(r.get('rejected_records') or 0)):,}")
                w(f"- **Retry:** {r.get('max_retries')} retries @ {r.get('retry_windows')} min")
            else:
                w("_no scheduling row found_")
        except Exception as e:
            w(f"_error: {e}_")
        w("")

        # ---- 3. Detectors (check) ----
        w("## 3. Detectors (`check`) — findings")
        w("")
        try:
            campaign = CampaignInfo(campaign_id=cid, calls=calls, org_id=org)
            findings = run_campaign(mb, campaign, cfg, build_detectors(cfg.detectors))
            w(f"Enabled detectors: {', '.join(sorted(cfg.detectors))}")
            w("")
            if findings:
                for f in findings:
                    w(f"- **[{f.severity.value.upper()}] {f.title}** — {f.detail}")
            else:
                w("_No findings (all detectors clean for this campaign)._")
        except Exception as e:
            w(f"_error: {e}_")
        w("")

        # ---- 4. Cycle report ----
        w("## 4. Cycle report (connectivity / engagement / PTP / dispositions)")
        w("")
        try:
            rep = build_cycle_report(mb, cfg, [cid])
            w("\n\n".join(rep.sections) if rep else "_no data_")
        except Exception as e:
            w(f"_error: {e}_")
        w("")

        # ---- LLM-backed sections ----
        llm = None
        try:
            llm = LLMClient.from_config(cfg.llm)
        except Exception as e:
            w(f"_LLM unavailable: {e}_")

        # ---- 5. Conversationality review ----
        w("## 5. Conversationality review (LLM)")
        w("")
        if llm:
            try:
                rep, fnd = build_conversationality_review(mb, cfg, llm, [cid])
                w("\n\n".join(rep.sections) if rep else "_no scorable calls_")
                if fnd:
                    w("")
                    for f in fnd:
                        w(f"- **[{f.severity.value.upper()}] {f.title}** — {f.detail}")
            except Exception as e:
                w(f"_error: {e}_")
        else:
            w("_LLM not configured_")
        w("")

        # ---- 6. Value-correctness ----
        w("## 6. Value-correctness (LLM)")
        w("")
        if llm:
            try:
                rep, fnd = build_value_correctness_review(mb, cfg, llm, [cid])
                w("\n\n".join(rep.sections) if rep else "_no scorable calls / no target variables present_")
                if fnd:
                    w("")
                    for f in fnd:
                        w(f"- **[{f.severity.value.upper()}] {f.title}** — {f.detail}")
            except Exception as e:
                w(f"_error: {e}_")
        else:
            w("_LLM not configured_")
        w("")

        # ---- 7. Weekly evals (insights, this campaign) ----
        w("## 7. Weekly evals (insights metrics for this campaign)")
        w("")
        try:
            rows = mb.query(
                f"""
                SELECT r.metric_name AS metric_name, r.metric_category AS category,
                    round(avg(r.boolean_value::int)::numeric,3) AS brate,
                    round(avg(r.numeric_value)::numeric,2) AS nval, count(*) AS n
                FROM {mb.evals_table('insights_result')} r
                JOIN {mb.evals_table('insights_run')} ir ON ir.id = r.run_id
                WHERE r.campaign_id = {sql_str(cid)} AND ir.last_run_at >= now() - INTERVAL '7 days'
                GROUP BY r.metric_name, r.metric_category ORDER BY n DESC LIMIT 30
                """,
                database_id=mb.evals_db,
            )
            if rows:
                w("| metric | category | value | n |")
                w("|---|---|---|---|")
                for r in rows:
                    cat = str(r.get("category"))
                    val = f"{float(r['brate'])*100:.1f}%" if cat == "bool" and r.get("brate") is not None else r.get("nval")
                    w(f"| {r['metric_name']} | {cat} | {val} | {int(r['n'])} |")
            else:
                w("_no insights evals for this campaign in the last 7 days_")
        except Exception as e:
            w(f"_error: {e}_")
        w("")

        # ---- 8. Client report ----
        w("## 8. Client report (per-org markdown)")
        w("")
        try:
            paths = build_client_reports(mb, cfg, [CampaignInfo(campaign_id=cid, calls=calls, org_id=org)])
            if paths:
                w(f"Generated: `{paths[0]}`")
                w("")
                w("```markdown")
                w(open(paths[0]).read().strip())
                w("```")
            else:
                w("_no report generated_")
        except Exception as e:
            w(f"_error: {e}_")
        w("")
    finally:
        mb.close()

    import os
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        fh.write("\n".join(md))
    print(f"wrote {out} ({len(md)} lines)")


if __name__ == "__main__":
    main()
