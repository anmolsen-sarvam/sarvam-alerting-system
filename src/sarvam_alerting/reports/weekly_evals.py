"""Weekly evals report -- aggregate the live insights metrics per campaign.

Source: ``insights_result`` (per-interaction metric values, keyed by campaign_id) joined
to ``insights_run`` for recency. Boolean metrics (hallucination, loop_detection, ...) are
reported as rates; integer metrics (call_effectiveness, ...) as averages. This is the
current, campaign-attributed eval pipeline (the old post_call_eval_results is stale).
"""

from __future__ import annotations

import re

from ..clients.metabase import MetabaseClient
from ..config import Config
from ..models import Report

_SAFE = re.compile(r"^[A-Za-z0-9_]+$")

_DEFAULT_BOOL_METRICS = [
    "hallucination",
    "loop_detection",
    "escalation_miss",
    "data_exfiltration",
    "harassing_language",
    "third_party_disclosure",
]
_DEFAULT_SCORE_METRICS = ["call_effectiveness", "confidence_disp_accuracy_score"]


def _safe(names: list[str]) -> list[str]:
    return [n for n in names if _SAFE.match(str(n))]


def build_weekly_evals(mb: MetabaseClient, config: Config) -> Report | None:
    opts = config.reports.get("weekly_evals", {})
    days = int(opts.get("days", 7))
    min_interactions = int(opts.get("min_interactions", 20))
    max_campaigns = int(opts.get("max_campaigns", 15))
    bool_metrics = _safe(opts.get("bool_metrics", _DEFAULT_BOOL_METRICS))
    score_metrics = _safe(opts.get("score_metrics", _DEFAULT_SCORE_METRICS))

    results = mb.evals_table("insights_result")
    runs = mb.evals_table("insights_run")

    bool_cols = ",\n".join(
        f"round(avg(CASE WHEN r.metric_name = '{m}' THEN r.boolean_value::int END)::numeric, 3) AS {m}"
        for m in bool_metrics
    )
    score_cols = ",\n".join(
        f"round(avg(CASE WHEN r.metric_name = '{m}' THEN r.numeric_value END)::numeric, 2) AS {m}"
        for m in score_metrics
    )
    metric_cols = ",\n".join(c for c in (bool_cols, score_cols) if c)

    sql = f"""
    SELECT
        r.campaign_id AS campaign_id,
        count(DISTINCT r.interaction_id) AS interactions{',' if metric_cols else ''}
        {metric_cols}
    FROM {results} r
    JOIN {runs} ir ON ir.id = r.run_id
    WHERE ir.last_run_at >= now() - INTERVAL '{days} days'
      AND r.campaign_id IS NOT NULL AND r.campaign_id <> ''
    GROUP BY r.campaign_id
    HAVING count(DISTINCT r.interaction_id) >= {min_interactions}
    ORDER BY interactions DESC
    LIMIT {max_campaigns}
    """
    rows = mb.query(sql, database_id=mb.evals_db)
    if not rows:
        return None

    def as_pct(v):
        try:
            return f"{float(v) * 100:.1f}%"
        except (TypeError, ValueError):
            return "-"

    total_int = sum(int(r["interactions"]) for r in rows)
    sections: list[str] = [
        f"Insights evals over the last {days} days · *{len(rows)}* campaigns · "
        f"*{total_int:,}* interactions evaluated."
    ]
    for r in rows:
        lines = [f"*`{r['campaign_id']}`* — {int(r['interactions']):,} evaluated"]
        bad = [f"{m} {as_pct(r.get(m))}" for m in bool_metrics if r.get(m) not in (None, 0)]
        if bad:
            lines.append("• Flags: " + " · ".join(bad))
        scores = [f"{m} {r.get(m)}" for m in score_metrics if r.get(m) is not None]
        if scores:
            lines.append("• Scores: " + " · ".join(scores))
        sections.append("\n".join(lines))

    return Report(kind="weekly_evals", title="Weekly evals report", sections=sections)
