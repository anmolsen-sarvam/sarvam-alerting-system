"""Disposition-correctness detector.

Uses the platform's own insights metric ``confidence_disp_accuracy_score`` (0-10) from the
insights pipeline, per campaign, rather than re-judging with an LLM. Alerts when a
campaign's average disposition-accuracy score drops below a threshold -- i.e. the agent is
marking dispositions incorrectly.
"""

from __future__ import annotations

from ..models import Finding, Severity
from .base import Detector, DetectorContext


class DispositionAccuracyDetector(Detector):
    name = "disposition_accuracy"

    def run(self, ctx: DetectorContext) -> list[Finding]:
        metric = str(self.opt("metric_name", "confidence_disp_accuracy_score"))
        days = int(self.opt("lookback_days", 7))
        min_evals = int(self.opt("min_evals", 20))
        min_score = float(self.opt("min_score", 6.0))

        results = ctx.metabase.evals_table("insights_result")
        runs = ctx.metabase.evals_table("insights_run")
        sql = f"""
        SELECT count(*) AS n, round(avg(r.numeric_value)::numeric, 2) AS avg_score
        FROM {results} r
        JOIN {runs} ir ON ir.id = r.run_id
        WHERE r.campaign_id = {ctx.campaign_literal}
          AND r.metric_name = '{metric}'
          AND r.numeric_value IS NOT NULL
          AND ir.last_run_at >= now() - INTERVAL '{days} days'
        """
        rows = ctx.metabase.query(sql, database_id=ctx.metabase.evals_db)
        if not rows:
            return []
        r = rows[0]
        n = int(r.get("n") or 0)
        if n < min_evals or r.get("avg_score") is None:
            return []
        avg = float(r["avg_score"])
        if avg >= min_score:
            return []
        return [
            Finding(
                detector=self.name,
                severity=Severity.CRITICAL if avg < min_score * 0.6 else Severity.WARNING,
                campaign_id=ctx.campaign.campaign_id,
                title="Dispositions being marked inaccurately",
                detail=(
                    f"Average disposition-accuracy score is {avg}/10 across {n} evaluated "
                    f"calls (last {days}d, threshold {min_score}). The agent is likely "
                    f"tagging call outcomes incorrectly."
                ),
                metrics={"avg_disposition_accuracy": avg, "evaluated": n},
                dedupe_key=f"{ctx.campaign.campaign_id}:disposition_accuracy",
            )
        ]
