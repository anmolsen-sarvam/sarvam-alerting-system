"""Cycle report: performance summary per campaign for a completed cycle.

Connectivity, engagement, PTP, and disposition funnels from EngagementFacts.

Disposition lives in ``on_end_agent_variables['disposition']`` and is free-form /
org-specific, so we show the top-N observed values rather than a fixed taxonomy.
PTP (promise-to-pay) is derived from ``promised_to_pay`` / ``promised_to_pay_date``
and is only meaningful for collections use-cases, so we only surface it when present.
"""

from __future__ import annotations

from ..clients.metabase import MetabaseClient, sql_str
from ..config import Config
from ..models import Report

# Truthy encodings seen in on_end_agent_variables['promised_to_pay'].
_PTP_TRUE = "('True', 'true', 'TRUE', 'Yes', 'yes', '1')"
_PTP_DATE_EMPTY = "('', 'null', 'None', 'NA')"


def _pct(num: int, den: int) -> str:
    return f"{(num / den * 100):.0f}%" if den else "-"


def _dispositions_by_campaign(
    mb: MetabaseClient, id_list: str, hours: int
) -> dict[str, list[tuple[str, int]]]:
    sql = f"""
    SELECT campaign_id,
           on_end_agent_variables['disposition'] AS disposition,
           count() AS c
    FROM {mb.table}
    WHERE created_at_timestamp >= now() - INTERVAL {hours} HOUR
      AND is_deleted = 0
      AND is_debug_call = 0
      AND campaign_id IN ({id_list})
      AND on_end_agent_variables['disposition'] != ''
    GROUP BY campaign_id, disposition
    ORDER BY c DESC
    """
    out: dict[str, list[tuple[str, int]]] = {}
    for r in mb.query(sql):
        out.setdefault(str(r["campaign_id"]), []).append(
            (str(r["disposition"]), int(r["c"]))
        )
    return out


def build_cycle_report(
    mb: MetabaseClient,
    config: Config,
    campaign_ids: list[str],
) -> Report | None:
    opts = config.reports.get("cycle_report", {})
    hours = int(opts.get("lookback_hours", 24))
    engaged_min_messages = int(opts.get("engaged_min_messages", 2))
    max_campaigns = int(opts.get("max_campaigns", 25))
    max_dispositions = int(opts.get("max_dispositions", 6))

    ids = [c for c in campaign_ids if c][:max_campaigns]
    if not ids:
        return None
    id_list = ", ".join(sql_str(c) for c in ids)

    funnel_sql = f"""
    SELECT
        campaign_id,
        any(org_id)                                                    AS org_id,
        count()                                                        AS total,
        countIf(v2v_connectivity_status = 'connected')                 AS connected,
        countIf(v2v_connectivity_status = 'no_answer')                 AS no_answer,
        countIf(v2v_connectivity_status = 'busy')                      AS busy,
        countIf(v2v_connectivity_status = 'failed')                    AS failed_conn,
        countIf(v2v_connectivity_status = 'connected'
                AND num_messages >= {engaged_min_messages})            AS engaged,
        countIf(on_end_agent_variables['promised_to_pay'] IN {_PTP_TRUE}
                OR on_end_agent_variables['promised_to_pay_date'] NOT IN {_PTP_DATE_EMPTY})
                                                                       AS ptp,
        round(avgIf(audio_duration, v2v_connectivity_status = 'connected'), 1) AS avg_dur
    FROM {mb.table}
    WHERE created_at_timestamp >= now() - INTERVAL {hours} HOUR
      AND is_deleted = 0
      AND is_debug_call = 0
      AND campaign_id IN ({id_list})
    GROUP BY campaign_id
    ORDER BY total DESC
    """
    rows = mb.query(funnel_sql)
    if not rows:
        return None

    dispositions = _dispositions_by_campaign(mb, id_list, hours)

    sections: list[str] = [
        f"Performance over the last {hours}h for {len(rows)} campaign(s):"
    ]
    for r in rows:
        total = int(r["total"])
        connected = int(r["connected"])
        ptp = int(r["ptp"])

        lines = [
            f"*`{r['campaign_id']}`*  _( {r.get('org_id') or 'unknown-org'} )_",
            f"• Dialed: *{total:,}*  ·  Connectivity: *{_pct(connected, total)}* "
            f"({connected:,})",
            f"• Breakdown: no_answer {int(r['no_answer']):,} · "
            f"busy {int(r['busy']):,} · failed {int(r['failed_conn']):,}",
            f"• Engagement: *{_pct(int(r['engaged']), connected)}* of connected "
            f"(≥{engaged_min_messages} msgs) · avg {r.get('avg_dur') or 0}s",
        ]
        # PTP only matters for collections use-cases; show it only when present.
        if ptp > 0:
            lines.append(f"• PTP: *{_pct(ptp, connected)}* of connected ({ptp:,})")

        disp = dispositions.get(str(r["campaign_id"]), [])
        if disp:
            disp_total = sum(c for _, c in disp)
            top = disp[:max_dispositions]
            rendered = " · ".join(
                f"{name} {_pct(c, disp_total)}" for name, c in top
            )
            lines.append(f"• Dispositions: {rendered}")

        sections.append("\n".join(lines))

    return Report(kind="cycle_report", title="Campaign cycle report", sections=sections)
