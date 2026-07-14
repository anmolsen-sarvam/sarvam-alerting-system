"""Error-rate detector.

Uses the per-call ``has_log_issues`` flag on EngagementFacts (which carries the
campaign_id) as the v1 error signal. A full error-type breakdown -- joining
InteractionErrors on interaction_id -- is a v2 enrichment.
"""

from __future__ import annotations

from ..models import Finding, Severity
from .base import Detector, DetectorContext


def _rate(num: int, den: int) -> float:
    return (num / den) if den else 0.0


class ErrorRateDetector(Detector):
    name = "errors"

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 100))
        rate_abs = float(self.opt("log_issue_rate_abs", 0.1))
        rate_mult = float(self.opt("log_issue_rate_mult", 2.0))

        win = ctx.window_case()
        sql = f"""
        SELECT
            countIf({win} = 'cur')  AS n_cur,
            countIf({win} = 'cur'  AND has_log_issues = 1) AS err_cur,
            countIf({win} = 'base') AS n_base,
            countIf({win} = 'base' AND has_log_issues = 1) AS err_base
        FROM {ctx.metabase.table}
        WHERE {ctx.base_where()}
        """
        rows = ctx.metabase.query(sql)
        if not rows:
            return []
        r = rows[0]
        n_cur = int(r["n_cur"])
        if n_cur < min_calls:
            return []

        rate_cur = _rate(int(r["err_cur"]), n_cur)
        rate_base = _rate(int(r["err_base"]), int(r["n_base"]))

        elevated = rate_base == 0 or rate_cur >= rate_base * rate_mult
        if rate_cur >= rate_abs and elevated:
            return [
                Finding(
                    detector=self.name,
                    severity=Severity.WARNING if rate_cur < 0.3 else Severity.CRITICAL,
                    campaign_id=ctx.campaign.campaign_id,
                    title="Elevated error rate",
                    detail=(
                        f"{rate_cur:.0%} of the last {n_cur} calls flagged log issues "
                        f"(baseline {rate_base:.0%})."
                    ),
                    metrics={
                        "error_rate_current": round(rate_cur, 4),
                        "error_rate_baseline": round(rate_base, 4),
                        "n_current": n_cur,
                    },
                    dedupe_key=f"{ctx.campaign.campaign_id}:errors",
                )
            ]
        return []
