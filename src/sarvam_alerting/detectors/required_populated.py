"""Required-variable population detector ("are all Overview columns populated?").

For variables the cohort transformation marks ``required``, checks the share of current
calls where the value is *blank*. A required variable that is empty for many calls means a
column isn't being populated -- complements `variable_collapse` (which catches a variable
collapsing to one non-empty default).
"""

from __future__ import annotations

import logging

from ..clients.metabase import sql_str
from ..clients.scheduling import required_variables
from ..models import Finding, Severity
from .base import Detector, DetectorContext

log = logging.getLogger("sarvam_alerting.detectors.required_populated")


class RequiredPopulatedDetector(Detector):
    name = "required_populated"

    def _required_vars(self, ctx: DetectorContext, max_vars: int) -> list[str]:
        app_sql = f"""
        SELECT app_id, app_version, count() AS c
        FROM {ctx.metabase.table}
        WHERE campaign_id = {ctx.campaign_literal}
          AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
          AND is_deleted = 0
          AND is_debug_call = 0
        GROUP BY app_id, app_version ORDER BY c DESC LIMIT 1
        """
        rows = ctx.metabase.query(app_sql)
        if not rows:
            return []
        app_id = str(rows[0].get("app_id") or "")
        try:
            version = int(rows[0].get("app_version"))
        except (TypeError, ValueError):
            version = None
        hints = required_variables(ctx.metabase, app_id, version)
        return [v for v, info in hints.items() if info.get("required")][:max_vars]

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 100))
        blank_rate_alert = float(self.opt("blank_rate_alert", 0.1))
        max_vars = int(self.opt("max_vars", 40))

        try:
            required = self._required_vars(ctx, max_vars)
        except Exception:
            log.debug("required-var lookup failed", exc_info=True)
            return []
        if not required:
            return []

        blank_cols = ",\n".join(
            f"countIf(on_start_agent_variables[{sql_str(v)}] = '') AS blank_{i}"
            for i, v in enumerate(required)
        )
        sql = f"""
        SELECT count() AS n,
               {blank_cols}
        FROM {ctx.metabase.table}
        WHERE campaign_id = {ctx.campaign_literal}
          AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
          AND is_deleted = 0
          AND is_debug_call = 0
        """
        rows = ctx.metabase.query(sql)
        if not rows:
            return []
        row = rows[0]
        n = int(row.get("n") or 0)
        if n < min_calls:
            return []

        findings: list[Finding] = []
        for i, v in enumerate(required):
            blank = int(row.get(f"blank_{i}") or 0)
            rate = blank / n if n else 0.0
            if rate >= blank_rate_alert:
                findings.append(
                    Finding(
                        detector=self.name,
                        severity=Severity.CRITICAL if rate >= 0.5 else Severity.WARNING,
                        campaign_id=ctx.campaign.campaign_id,
                        title=f"Required variable `{v}` is blank",
                        detail=(
                            f"`{v}` is required by the cohort transformation but is empty in "
                            f"{rate:.0%} of the last {n} calls ({blank:,}). A column is not "
                            f"being populated from the cohort."
                        ),
                        metrics={"variable": v, "blank_rate": round(rate, 4), "n_current": n},
                        dedupe_key=f"{ctx.campaign.campaign_id}:required_populated:{v}",
                    )
                )
        return findings
