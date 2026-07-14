"""Expected-value & cohort-size checks (config-driven, from the QC checklist).

Automates the pre-launch / sanity items that require knowing what the value *should*
be (which the system cannot infer -- it needs client/CS input):

  - "ensure loan type is 'Digital Personal Loan' for D2C" -> a variable value-set check
  - "validate cohort size is correct as per what client shared" -> a cohort-size check

Rules live under ``[[expected]]`` in config. Each rule targets campaigns via
match_* conditions, then asserts either an allowed value-set for a variable, or an
expected cohort size within a tolerance. This is where CS-team inputs get encoded.
"""

from __future__ import annotations

import logging

from ..clients.metabase import sql_str
from ..clients.scheduling import campaign_cohort_totals
from ..models import Finding, Severity
from .base import Detector, DetectorContext

log = logging.getLogger("sarvam_alerting.detectors.expected_values")


class ExpectedValuesDetector(Detector):
    name = "expected_values"

    def _matches(self, rule: dict, ctx: DetectorContext, app_id: str) -> bool:
        cid = ctx.campaign.campaign_id
        conds = []
        if "match_campaign_contains" in rule:
            conds.append(str(rule["match_campaign_contains"]) in cid)
        if "match_org_id" in rule:
            conds.append(str(rule["match_org_id"]) == ctx.campaign.org_id)
        if "match_app_contains" in rule:
            conds.append(str(rule["match_app_contains"]) in app_id)
        # A rule must specify at least one match condition, and all must pass.
        return bool(conds) and all(conds)

    def _dominant_app_id(self, ctx: DetectorContext) -> str:
        sql = f"""
        SELECT app_id, count() AS c
        FROM {ctx.metabase.table}
        WHERE campaign_id = {ctx.campaign_literal}
          AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
          AND is_deleted = 0
          AND is_debug_call = 0
        GROUP BY app_id ORDER BY c DESC LIMIT 1
        """
        rows = ctx.metabase.query(sql)
        return str(rows[0].get("app_id")) if rows else ""

    def _check_variable(self, rule: dict, ctx: DetectorContext) -> Finding | None:
        variable = str(rule["variable"])
        allowed = {str(v) for v in rule.get("allowed", [])}
        min_share = float(rule.get("min_share", 0.02))
        if not allowed:
            return None

        key = sql_str(variable)
        sql = f"""
        SELECT on_start_agent_variables[{key}] AS val, count() AS c
        FROM {ctx.metabase.table}
        WHERE campaign_id = {ctx.campaign_literal}
          AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
          AND is_deleted = 0
          AND is_debug_call = 0
          AND on_start_agent_variables[{key}] != ''
        GROUP BY val ORDER BY c DESC LIMIT 100
        """
        rows = ctx.metabase.query(sql)
        total = sum(int(r["c"]) for r in rows)
        if total == 0:
            return None
        disallowed = [(str(r["val"]), int(r["c"])) for r in rows if str(r["val"]) not in allowed]
        bad = sum(c for _, c in disallowed)
        share = bad / total
        if share < min_share:
            return None

        examples = ", ".join(f'"{v}" ({c:,})' for v, c in disallowed[:5])
        return Finding(
            detector=self.name,
            severity=Severity.CRITICAL if share >= 0.5 else Severity.WARNING,
            campaign_id=ctx.campaign.campaign_id,
            title=f"Unexpected values for `{variable}`",
            detail=(
                f"{share:.0%} of calls ({bad:,}/{total:,}) in the last "
                f"{ctx.current_hours}h have `{variable}` outside the allowed set "
                f"{sorted(allowed)}. Seen: {examples}."
            ),
            metrics={"variable": variable, "disallowed_share": round(share, 4), "allowed": sorted(allowed)},
            dedupe_key=f"{ctx.campaign.campaign_id}:expected_values:{variable}",
        )

    def _check_cohort_size(self, rule: dict, ctx: DetectorContext) -> Finding | None:
        expected = int(rule["cohort_size"])
        tolerance = float(rule.get("cohort_tolerance_pct", 20)) / 100.0
        if expected <= 0:
            return None
        _, valid, _ = campaign_cohort_totals(ctx.metabase, ctx.campaign.campaign_id)
        if valid == 0:
            return None
        deviation = abs(valid - expected) / expected
        if deviation <= tolerance:
            return None
        return Finding(
            detector=self.name,
            severity=Severity.WARNING,
            campaign_id=ctx.campaign.campaign_id,
            title="Cohort size differs from expected",
            detail=(
                f"Uploaded cohort has {valid:,} valid records, but the expected size "
                f"is {expected:,} (±{tolerance:.0%}). Deviation {deviation:.0%} — "
                f"verify the cohort matches what the client shared."
            ),
            metrics={"expected": expected, "actual_valid": valid, "deviation": round(deviation, 4)},
            dedupe_key=f"{ctx.campaign.campaign_id}:expected_values:cohort_size",
        )

    def run(self, ctx: DetectorContext) -> list[Finding]:
        rules = ctx.config.expected
        if not rules:
            return []
        app_id = ""
        # Only resolve the app id if a rule needs it (avoids an extra query otherwise).
        if any("match_app_contains" in r for r in rules):
            try:
                app_id = self._dominant_app_id(ctx)
            except Exception:
                log.debug("app id lookup failed", exc_info=True)

        findings: list[Finding] = []
        for rule in rules:
            if not self._matches(rule, ctx, app_id):
                continue
            try:
                if "variable" in rule:
                    f = self._check_variable(rule, ctx)
                    if f:
                        findings.append(f)
                if "cohort_size" in rule:
                    f = self._check_cohort_size(rule, ctx)
                    if f:
                        findings.append(f)
            except Exception:
                log.exception("expected rule %r failed", rule.get("name"))
        return findings
