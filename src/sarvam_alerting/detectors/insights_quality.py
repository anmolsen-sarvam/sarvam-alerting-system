"""Insights-based conversation-quality detector.

Since transcript text in Metabase is encrypted, we get conversation-quality signals from
Sarvam's own insights pipeline (`insights_result`), which computes them upstream on
decrypted transcripts. Alerts when per-campaign rates of quality/safety issues
(hallucination, looping, escalation-miss, ...) exceed configured thresholds.
"""

from __future__ import annotations

from ..clients.metabase import sql_str
from ..models import Finding, Severity
from .base import Detector, DetectorContext

# Metrics treated as safety-critical (CRITICAL severity when breached).
_CRITICAL_METRICS = {
    "hallucination",
    "data_exfiltration",
    "harassing_language",
    "third_party_disclosure",
}

_DEFAULT_THRESHOLDS = {
    "hallucination": 0.05,
    "loop_detection": 0.30,
    "escalation_miss": 0.15,
    "data_exfiltration": 0.01,
    "harassing_language": 0.02,
    "third_party_disclosure": 0.02,
}


class InsightsQualityDetector(Detector):
    name = "insights_quality"

    def run(self, ctx: DetectorContext) -> list[Finding]:
        days = int(self.opt("lookback_days", 7))
        min_evals = int(self.opt("min_evals", 20))
        thresholds = dict(self.opt("thresholds", _DEFAULT_THRESHOLDS))
        metrics = [m for m in thresholds if str(m).replace("_", "").isalnum()]
        if not metrics:
            return []

        metric_list = ", ".join(sql_str(m) for m in metrics)
        results = ctx.metabase.evals_table("insights_result")
        runs = ctx.metabase.evals_table("insights_run")
        sql = f"""
        SELECT r.metric_name AS metric_name,
               round(avg(r.boolean_value::int)::numeric, 4) AS rate,
               count(*) AS n
        FROM {results} r
        JOIN {runs} ir ON ir.id = r.run_id
        WHERE r.campaign_id = {ctx.campaign_literal}
          AND r.metric_name IN ({metric_list})
          AND ir.last_run_at >= now() - INTERVAL '{days} days'
        GROUP BY r.metric_name
        """
        rows = ctx.metabase.query(sql, database_id=ctx.metabase.evals_db)

        findings: list[Finding] = []
        for r in rows:
            metric = str(r["metric_name"])
            n = int(r.get("n") or 0)
            if n < min_evals or r.get("rate") is None:
                continue
            rate = float(r["rate"])
            threshold = float(thresholds.get(metric, 1.0))
            if rate < threshold:
                continue
            sev = Severity.CRITICAL if metric in _CRITICAL_METRICS else Severity.WARNING
            findings.append(
                Finding(
                    detector=self.name,
                    severity=sev,
                    campaign_id=ctx.campaign.campaign_id,
                    title=f"High `{metric}` rate",
                    detail=(
                        f"`{metric}` flagged in {rate:.1%} of {n} evaluated calls "
                        f"(last {days}d, threshold {threshold:.1%})."
                    ),
                    metrics={"metric": metric, "rate": round(rate, 4), "evaluated": n},
                    dedupe_key=f"{ctx.campaign.campaign_id}:insights_quality:{metric}",
                )
            )
        return findings
