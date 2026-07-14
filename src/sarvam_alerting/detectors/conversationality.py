"""Layer A conversationality detector -- metric-based, no LLM.

Covers the parts of the "Midday QC" checklist that are visible directly in the data:
  - agent looping        -> on_end_agent_variables['bot_went_loop']
  - oddly long silences  -> average_agent_response_time_in_seconds
  - calls not ending     -> end_reason = 'NO_END_REASON' with many messages

Each is compared against the campaign's own baseline (elevated vs normal) plus an
absolute floor, to avoid noise. Deep semantic checks (hallucination, wrong answers)
are Layer B (LLM); see docs/transcript-conversationality-plan.md.
"""

from __future__ import annotations

from ..models import Finding, Severity
from .base import Detector, DetectorContext

_TRUE = "('True', 'true', 'TRUE', '1', 'yes', 'Yes')"


def _rate(num: int, den: int) -> float:
    return (num / den) if den else 0.0


class ConversationalityDetector(Detector):
    name = "conversationality"

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 100))
        loop_abs = float(self.opt("loop_rate_abs", 0.05))
        loop_mult = float(self.opt("loop_rate_mult", 2.0))
        silence_seconds = float(self.opt("silence_seconds", 8.0))
        silence_abs = float(self.opt("silence_rate_abs", 0.15))
        silence_mult = float(self.opt("silence_rate_mult", 1.75))
        nocls_min_messages = int(self.opt("nocls_min_messages", 15))
        nocls_abs = float(self.opt("nocls_rate_abs", 0.2))
        nocls_mult = float(self.opt("nocls_rate_mult", 1.75))

        win = ctx.window_case()
        conn = "v2v_connectivity_status = 'connected'"
        loop = f"on_end_agent_variables['bot_went_loop'] IN {_TRUE}"
        sil = f"average_agent_response_time_in_seconds > {silence_seconds}"
        noc = f"end_reason = 'NO_END_REASON' AND num_messages >= {nocls_min_messages}"

        sql = f"""
        SELECT
            countIf({win} = 'cur'  AND {conn})            AS conn_cur,
            countIf({win} = 'cur'  AND {conn} AND {loop}) AS loop_cur,
            countIf({win} = 'cur'  AND {conn} AND {sil})  AS sil_cur,
            countIf({win} = 'cur'  AND {conn} AND ({noc})) AS noc_cur,
            countIf({win} = 'base' AND {conn})            AS conn_base,
            countIf({win} = 'base' AND {conn} AND {loop}) AS loop_base,
            countIf({win} = 'base' AND {conn} AND {sil})  AS sil_base,
            countIf({win} = 'base' AND {conn} AND ({noc})) AS noc_base
        FROM {ctx.metabase.table}
        WHERE {ctx.base_where()}
        """
        rows = ctx.metabase.query(sql)
        if not rows:
            return []
        r = rows[0]
        conn_cur = int(r["conn_cur"])
        if conn_cur < min_calls:
            return []
        conn_base = int(r["conn_base"])

        findings: list[Finding] = []

        def check(kind, title, cur, base, abs_floor, mult, sev=Severity.WARNING):
            rate_cur = _rate(int(cur), conn_cur)
            rate_base = _rate(int(base), conn_base)
            elevated = rate_base == 0 or rate_cur >= rate_base * mult
            if rate_cur >= abs_floor and elevated:
                findings.append(
                    Finding(
                        detector=self.name,
                        severity=sev,
                        campaign_id=ctx.campaign.campaign_id,
                        title=title,
                        detail=(
                            f"{rate_cur:.0%} of {conn_cur:,} connected calls in the last "
                            f"{ctx.current_hours}h (baseline {rate_base:.0%})."
                        ),
                        metrics={
                            "rate_current": round(rate_cur, 4),
                            "rate_baseline": round(rate_base, 4),
                            "connected_current": conn_cur,
                        },
                        dedupe_key=f"{ctx.campaign.campaign_id}:conversationality:{kind}",
                    )
                )

        check("looping", "Agent looping spike", r["loop_cur"], r["loop_base"], loop_abs, loop_mult, Severity.CRITICAL)
        check("silence", "Long agent silences", r["sil_cur"], r["sil_base"], silence_abs, silence_mult)
        check("no_closure", "Calls not closing properly", r["noc_cur"], r["noc_base"], nocls_abs, nocls_mult)
        return findings
