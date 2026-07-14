"""Variable-collapse detector -- the priority detector.

Catches the escalation bug: a per-contact variable (customer name, EMI amount,
due date, ...) silently falling back to a single default value across the whole
cohort -- e.g. every call opening with "am I talking with <same name>?".

Key insight from the data: many agent variables are *legitimately* constant across
a campaign (bot_name, gst_rate, campaign_code, ...). So "low cardinality" alone is
NOT a bug. The real fingerprint is a variable that was per-contact in the baseline
window suddenly collapsing to a single value in the current window. We therefore
compare each variable against *its own* baseline rather than an absolute threshold.
"""

from __future__ import annotations

import logging

from ..clients.scheduling import required_variables
from ..models import Finding, Severity
from .base import Detector, DetectorContext

log = logging.getLogger("sarvam_alerting.detectors.variable_collapse")


class VariableCollapseDetector(Detector):
    name = "variable_collapse"

    def _dominant_app(self, ctx: DetectorContext) -> tuple[str, int | None]:
        """Find the campaign's most-used (app_id, app_version) in the current window."""
        sql = f"""
        SELECT app_id, app_version, count() AS c
        FROM {ctx.metabase.table}
        WHERE campaign_id = {ctx.campaign_literal}
          AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
          AND is_deleted = 0
          AND is_debug_call = 0
        GROUP BY app_id, app_version
        ORDER BY c DESC
        LIMIT 1
        """
        rows = ctx.metabase.query(sql)
        if not rows:
            return "", None
        r = rows[0]
        try:
            version = int(r.get("app_version"))
        except (TypeError, ValueError):
            version = None
        return str(r.get("app_id") or ""), version

    def _required_hints(self, ctx: DetectorContext) -> dict[str, dict]:
        """Best-effort lookup of the app's required-variable set from the scheduling DB."""
        if not bool(self.opt("use_required_hints", True)):
            return {}
        try:
            app_id, app_version = self._dominant_app(ctx)
            return required_variables(ctx.metabase, app_id, app_version)
        except Exception:
            log.debug("required-variable lookup failed", exc_info=True)
            return {}

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 50))
        per_contact_min_distinct = int(self.opt("per_contact_min_distinct", 15))
        per_contact_min_ratio = float(self.opt("per_contact_min_ratio", 0.05))
        collapse_max_distinct = int(self.opt("collapse_max_distinct", 1))
        hints = self._required_hints(ctx)

        sql = f"""
        SELECT
            key,
            countIf(win = 'cur')            AS n_cur,
            uniqExactIf(val, win = 'cur')   AS distinct_cur,
            anyIf(val, win = 'cur')         AS sample_cur,
            countIf(win = 'base')           AS n_base,
            uniqExactIf(val, win = 'base')  AS distinct_base
        FROM (
            SELECT key, val, {ctx.window_case()} AS win
            FROM (
                SELECT on_start_agent_variables AS m, created_at_timestamp
                FROM {ctx.metabase.table}
                WHERE {ctx.base_where()} AND length(on_start_agent_variables) > 0
            )
            ARRAY JOIN mapKeys(m) AS key, mapValues(m) AS val
        )
        GROUP BY key
        HAVING n_cur >= {min_calls}
        """
        rows = ctx.metabase.query(sql)

        findings: list[Finding] = []
        for r in rows:
            n_cur = int(r["n_cur"])
            distinct_cur = int(r["distinct_cur"])
            n_base = int(r["n_base"])
            distinct_base = int(r["distinct_base"])
            key = str(r["key"])
            sample_cur = r.get("sample_cur")

            if n_cur < min_calls:
                continue

            # Intent signal from the cohort transformation: a `required` agent
            # variable must come from the cohort, so it should never collapse.
            is_required = bool(hints.get(key, {}).get("required"))

            # Was this variable per-contact in the baseline?
            per_contact = distinct_base >= per_contact_min_distinct or (
                n_base > 0 and (distinct_base / n_base) >= per_contact_min_ratio
            )
            collapsed = distinct_cur <= collapse_max_distinct
            # Required variables are flagged on collapse even without baseline history
            # (that is exactly the "catch it as soon as the campaign is up" case).
            if not (collapsed and (per_contact or is_required)):
                continue

            if is_required:
                basis = (
                    f"This variable is marked *required* in the cohort transformation, "
                    f"so it must be populated per-contact from the uploaded cohort. "
                    f"Every one of the {n_cur} calls in the last {ctx.current_hours}h "
                    f"instead used the same value \"{sample_cur}\" — the cohort mapping "
                    f"is not being applied (default-variable fallback)."
                )
            else:
                basis = (
                    f"All {n_cur} calls in the last {ctx.current_hours}h used "
                    f"`{key}` = \"{sample_cur}\". In the baseline ({ctx.baseline_hours}h) "
                    f"this variable had {distinct_base} distinct values across "
                    f"{n_base} calls, so it is normally per-contact. This looks like a "
                    f"default-variable fallback (the cohort mapping is not being applied)."
                )

            findings.append(
                Finding(
                    detector=self.name,
                    severity=Severity.CRITICAL,
                    campaign_id=ctx.campaign.campaign_id,
                    title=f"Variable `{key}` collapsed to a single value",
                    detail=basis,
                    metrics={
                        "variable": key,
                        "current_value": sample_cur,
                        "required": is_required,
                        "n_current": n_cur,
                        "distinct_current": distinct_cur,
                        "n_baseline": n_base,
                        "distinct_baseline": distinct_base,
                    },
                    dedupe_key=f"{ctx.campaign.campaign_id}:variable_collapse:{key}",
                )
            )
        return findings
