"""Connectivity / dialer-failure detector.

Both signals are **baseline-relative** (vs the campaign's own recent normal), because
absolute connectivity is meaningless for cold outbound dialing — 85-90% no-answer/failed
is normal, so an absolute "failure ceiling" just fires on every campaign. We compare to
history instead. Uses ``v2v_connectivity_status`` (the purpose-built connectivity column),
not ``completion_status`` (which is unreliable — often UNKNOWN, and conflates no-answer
with genuine failure).
"""

from __future__ import annotations

from ..models import Finding, Severity
from .base import Detector, DetectorContext


def _rate(num: int, den: int) -> float:
    return (num / den) if den else 0.0


class ConnectivityDetector(Detector):
    name = "connectivity"

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 200))
        connected_drop_ratio = float(self.opt("connected_drop_ratio", 0.4))
        failed_spike_mult = float(self.opt("failed_spike_mult", 1.75))
        failed_rate_min = float(self.opt("failed_rate_min", 0.25))

        win = ctx.window_case()
        sql = f"""
        SELECT
            countIf({win} = 'cur')  AS n_cur,
            countIf({win} = 'cur'  AND v2v_connectivity_status = 'connected') AS conn_cur,
            countIf({win} = 'cur'  AND v2v_connectivity_status = 'failed')     AS fail_cur,
            countIf({win} = 'base') AS n_base,
            countIf({win} = 'base' AND v2v_connectivity_status = 'connected') AS conn_base,
            countIf({win} = 'base' AND v2v_connectivity_status = 'failed')     AS fail_base
        FROM {ctx.metabase.table}
        WHERE {ctx.base_where()}
        """
        rows = ctx.metabase.query(sql)
        if not rows:
            return []
        r = rows[0]
        n_cur, n_base = int(r["n_cur"]), int(r["n_base"])
        if n_cur < min_calls or n_base < min_calls:
            return []

        conn_cur = _rate(int(r["conn_cur"]), n_cur)
        conn_base = _rate(int(r["conn_base"]), n_base)
        fail_cur = _rate(int(r["fail_cur"]), n_cur)
        fail_base = _rate(int(r["fail_base"]), n_base)

        findings: list[Finding] = []

        # 1) Connected-rate dropped sharply vs this campaign's own baseline.
        if conn_base > 0:
            drop = (conn_base - conn_cur) / conn_base
            if drop >= connected_drop_ratio:
                findings.append(
                    Finding(
                        detector=self.name,
                        severity=Severity.CRITICAL if drop >= 0.7 else Severity.WARNING,
                        campaign_id=ctx.campaign.campaign_id,
                        title="Connectivity dropped sharply",
                        detail=(
                            f"Connected-rate is {conn_cur:.0%} in the last "
                            f"{ctx.current_hours}h vs {conn_base:.0%} baseline "
                            f"(a {drop:.0%} relative drop) over {n_cur} calls."
                        ),
                        metrics={
                            "connected_rate_current": round(conn_cur, 4),
                            "connected_rate_baseline": round(conn_base, 4),
                            "relative_drop": round(drop, 4),
                            "n_current": n_cur,
                        },
                        dedupe_key=f"{ctx.campaign.campaign_id}:connectivity:drop",
                    )
                )

        # 2) Dialer-failure spike (v2v='failed', i.e. couldn't connect for a real reason —
        #    not no-answer/busy) rising vs baseline. Requires an absolute floor so normal
        #    campaigns don't trip it.
        if fail_cur >= failed_rate_min and (fail_base == 0 or fail_cur >= fail_base * failed_spike_mult):
            findings.append(
                Finding(
                    detector=self.name,
                    severity=Severity.WARNING,
                    campaign_id=ctx.campaign.campaign_id,
                    title="Dialer-failure rate spiking",
                    detail=(
                        f"{fail_cur:.0%} of the last {n_cur} calls failed to connect "
                        f"(v2v='failed') vs {fail_base:.0%} baseline — likely a "
                        f"telephony/number-pool issue."
                    ),
                    metrics={
                        "failed_rate_current": round(fail_cur, 4),
                        "failed_rate_baseline": round(fail_base, 4),
                        "n_current": n_cur,
                    },
                    dedupe_key=f"{ctx.campaign.campaign_id}:connectivity:failed_spike",
                )
            )
        return findings
