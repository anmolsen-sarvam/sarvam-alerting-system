"""Value-correctness via transcript (LLM).

Checks whether the agent actually *stated* key per-contact values correctly (EMI amount,
due date, total due, ...). This needs transcript understanding -- numbers are often spoken
as words and dates in varied formats, especially in Hindi/regional languages -- so it uses
the LLM rather than string matching. Complements `variable_collapse` (which only checks the
value was populated, not that it was conveyed correctly).
"""

from __future__ import annotations

import json
import logging
import re

from ..clients.llm import LLMClient
from ..clients.metabase import MetabaseClient, sql_str
from ..config import Config
from ..models import Finding, Report, Severity
from .conversationality_review import (
    _fetch_transcripts,
    _sample_interactions,
    transcripts_encrypted,
)

log = logging.getLogger("sarvam_alerting.reports.value_correctness")

_SAFE = re.compile(r"^[A-Za-z0-9_]+$")

SYSTEM_PROMPT = """You verify whether a voice agent stated specific values CORRECTLY in a \
call transcript. Calls are in Hindi/Hinglish or Indian regional languages; numbers are \
often spoken as words and dates in varied formats, so judge by meaning, not exact string.

You are given expected variable values and the transcript. For each variable decide:
- "correct"       -> the agent conveyed that value (allow spoken-number / format variants)
- "incorrect"     -> the agent stated a DIFFERENT value for it
- "not_mentioned" -> that value never came up in the call

Return ONLY JSON: {"results": {"<variable>": "correct|incorrect|not_mentioned", ...}, "notes": "brief"}"""


def _fetch_values(mb: MetabaseClient, interaction_ids: list[str], variables: list[str]) -> dict[str, dict]:
    if not interaction_ids or not variables:
        return {}
    id_list = ", ".join(sql_str(i) for i in interaction_ids)
    cols = ", ".join(
        f"on_start_agent_variables[{sql_str(v)}] AS {v}" for v in variables
    )
    sql = f"""
    SELECT interaction_id, {cols}
    FROM {mb.table}
    WHERE interaction_id IN ({id_list})
    """
    out: dict[str, dict] = {}
    for r in mb.query(sql):
        iid = str(r["interaction_id"])
        out[iid] = {v: r.get(v) for v in variables if r.get(v) not in (None, "")}
    return out


def build_value_correctness_review(
    mb: MetabaseClient,
    config: Config,
    llm: LLMClient,
    campaign_ids: list[str],
) -> tuple[Report | None, list[Finding]]:
    opts = config.reports.get("value_correctness", {})
    variables = [v for v in opts.get("check_variables", []) if _SAFE.match(str(v))]
    if not variables:
        return None, []
    hours = int(opts.get("lookback_hours", 24))
    sample_size = int(opts.get("sample_size", 10))
    max_campaigns = int(opts.get("max_campaigns", 5))
    min_messages = int(opts.get("min_messages", 6))
    prefer_completed = bool(opts.get("prefer_completed", True))
    incorrect_rate_alert = float(opts.get("incorrect_rate_alert", 0.15))
    min_checked = int(opts.get("min_checked", 5))

    ids = [c for c in campaign_ids if c][:max_campaigns]
    if not ids:
        return None, []

    sections: list[str] = []
    findings: list[Finding] = []

    for campaign_id in ids:
        sampled = _sample_interactions(mb, campaign_id, hours, sample_size, min_messages, prefer_completed)
        if not sampled:
            continue
        iids = [str(s["interaction_id"]) for s in sampled]
        transcripts = _fetch_transcripts(mb, iids)
        if transcripts_encrypted(transcripts):
            sections.append(
                f"*`{campaign_id}`* — transcripts are encrypted in Metabase; "
                f"value-correctness check skipped."
            )
            continue
        values = _fetch_values(mb, iids, variables)

        # counts[var] = {correct, incorrect, not_mentioned}; bad_ids[var] = [iid,...]
        counts: dict[str, dict[str, int]] = {v: {"correct": 0, "incorrect": 0, "not_mentioned": 0} for v in variables}
        bad_ids: dict[str, list[str]] = {v: [] for v in variables}
        scored = 0

        for iid in iids:
            turns = transcripts.get(iid)
            expected = values.get(iid)
            if not turns or not expected:
                continue
            payload = json.dumps({"values": expected, "transcript": turns}, ensure_ascii=False)
            try:
                verdict = llm.json(SYSTEM_PROMPT, payload, max_tokens=400)
            except Exception:
                log.exception("value-correctness scoring failed for %s", iid)
                continue
            scored += 1
            results = verdict.get("results", {}) if isinstance(verdict, dict) else {}
            for v in expected:
                r = str(results.get(v, "not_mentioned"))
                if r not in counts[v]:
                    r = "not_mentioned"
                counts[v][r] += 1
                if r == "incorrect":
                    bad_ids[v].append(iid)

        lines = [f"*`{campaign_id}`* — {scored} calls checked"]
        flagged_any = False
        for v in variables:
            c = counts[v]
            checked = c["correct"] + c["incorrect"]
            lines.append(
                f"• `{v}`: {c['correct']} correct · {c['incorrect']} incorrect · "
                f"{c['not_mentioned']} not mentioned"
            )
            if checked >= min_checked:
                rate = c["incorrect"] / checked
                if rate >= incorrect_rate_alert:
                    flagged_any = True
                    findings.append(
                        Finding(
                            detector="value_correctness",
                            severity=Severity.CRITICAL if rate >= 0.4 else Severity.WARNING,
                            campaign_id=campaign_id,
                            title=f"Agent stating `{v}` incorrectly",
                            detail=(
                                f"{rate:.0%} of checked calls ({c['incorrect']}/{checked}) had the "
                                f"agent state a wrong `{v}` vs the cohort value."
                            ),
                            metrics={"variable": v, "incorrect_rate": round(rate, 3), "checked": checked},
                            interaction_ids=tuple(bad_ids[v][:5]),
                            dedupe_key=f"{campaign_id}:value_correctness:{v}",
                        )
                    )
        if scored > 0:
            sections.append("\n".join(lines))

    if not sections:
        return None, findings
    report = Report(kind="value_correctness", title="Value-correctness review (LLM)", sections=sections)
    return report, findings
