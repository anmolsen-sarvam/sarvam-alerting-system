"""Short-call / disposition-anomaly detector.

A spike in ultra-short *connected* calls is a strong proxy for "something is broken"
(agent crashing, wrong script, immediate hangups). We measure the share of connected
calls under a duration threshold and alert when it is both high in absolute terms and
elevated relative to baseline.
"""

from __future__ import annotations

from ..models import Finding, Severity
from .base import Detector, DetectorContext


def _rate(num: int, den: int) -> float:
    return (num / den) if den else 0.0


class ShortCallsDetector(Detector):
    name = "short_calls"

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 100))
        short_seconds = float(self.opt("short_duration_seconds", 10.0))
        short_share_warn = float(self.opt("short_share_warn", 0.5))
        short_share_mult = float(self.opt("short_share_mult", 1.75))

        win = ctx.window_case()
        connected = "v2v_connectivity_status = 'connected'"
        short = f"audio_duration <= {short_seconds}"
        sql = f"""
        SELECT
            countIf({win} = 'cur'  AND {connected})              AS conn_cur,
            countIf({win} = 'cur'  AND {connected} AND {short})  AS short_cur,
            countIf({win} = 'base' AND {connected})              AS conn_base,
            countIf({win} = 'base' AND {connected} AND {short})  AS short_base
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

        share_cur = _rate(int(r["short_cur"]), conn_cur)
        share_base = _rate(int(r["short_base"]), int(r["conn_base"]))

        elevated = share_base == 0 or share_cur >= share_base * short_share_mult
        if share_cur >= short_share_warn and elevated:
            return [
                Finding(
                    detector=self.name,
                    severity=Severity.WARNING,
                    campaign_id=ctx.campaign.campaign_id,
                    title="Spike in ultra-short connected calls",
                    detail=(
                        f"{share_cur:.0%} of connected calls in the last "
                        f"{ctx.current_hours}h ended within {short_seconds:.0f}s "
                        f"(baseline {share_base:.0%}), across {conn_cur} connected calls. "
                        f"Often indicates a broken script/agent or immediate hangups."
                    ),
                    metrics={
                        "short_share_current": round(share_cur, 4),
                        "short_share_baseline": round(share_base, 4),
                        "connected_current": conn_cur,
                        "threshold_seconds": short_seconds,
                    },
                    dedupe_key=f"{ctx.campaign.campaign_id}:short_calls",
                )
            ]
        return []
