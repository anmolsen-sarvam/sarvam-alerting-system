"""Layer B: LLM-scored transcript conversationality review.

Samples calls per campaign, reconstructs transcripts from InteractionMessages, scores
each with the LLM against the transcript-analysis framework, aggregates, and returns a
report plus alert Findings when a campaign's quality drops.

This is LLM-backed, so it runs on a slower cadence (a daily timer / the CLI command),
NOT on the 30-minute metric scan.
"""

from __future__ import annotations

import base64
import json
import logging

from ..clients.llm import LLMClient
from ..clients.metabase import MetabaseClient, sql_str
from ..config import Config
from ..models import Finding, Report, Severity

log = logging.getLogger("sarvam_alerting.reports.conversationality")

# Compact form of skills/transcript-analysis/reference/analysis-framework.md,
# constrained to a single-transcript JSON verdict.
SYSTEM_PROMPT = """You are an expert conversation-quality analyst for voice-bot \
collections/outreach calls (often in Hindi/Hinglish or Indian regional languages). \
You are given ONE call transcript as a JSON list of turns (role = "assistant"=agent or \
"user"=customer, content, optional tools). Judge the AGENT's quality.

Detect these agent gaps: looping (same message/intent 3+ times), no_closure (customer \
committed but agent keeps going), ptp_date_loop (re-asking for a date the customer gave), \
over_pushing (pushing after 2+ refusals), no_empathy_on_hardship, monologue, \
hallucination/nonsensical replies, language_mismatch, abrupt_ending.

Calibration (be conservative — do NOT invent gaps):
- long != broken; dismissive "haan haan" is not agreement; paraphrased consequence \
pushes still count as a loop; hardship (death/medical/job loss) MUST be de-escalated.
- If the CUSTOMER dropped/went silent within a few turns (early_drop) and the agent made \
no error, do NOT penalize the agent — that is not an agent gap.
- Only flag "language_mismatch" if the agent clearly speaks a DIFFERENT language than the \
customer for 2+ turns. Hindi/Hinglish/Devanagari with English words mixed in is NOT a \
mismatch. When unsure, do not flag it.
- "problems" must be an empty list if no clear agent gap is present.

Return ONLY a JSON object (no prose) with this exact schema:
{"flow_category": "clean_resolution|partial_engagement|early_drop|agent_loop|customer_confusion|off_script|platform_timeout",
 "customer_pattern": "cooperative|deflecting|disputing|already_paid|hardship|hostile|confused|third_party|silent_minimal|wants_branch_offline",
 "problems": ["gap keys from the list above"],
 "primary_problem": "single most important gap or 'none'",
 "scores": {"objection_handling":1-5,"closing_ability":1-5,"conversation_control":1-5,"empathy_tone":1-5,"loop_avoidance":1-5},
 "overall_score": 1-5,
 "summary": "2-3 sentence summary of what happened and what went wrong"}"""


def _sample_interactions(
    mb: MetabaseClient,
    campaign_id: str,
    hours: int,
    sample_size: int,
    min_messages: int,
    prefer_completed: bool = True,
) -> list[dict]:
    """Sample connected calls for LLM review.

    By default prefers *substantive* calls -- completed first, then most messages -- so the
    LLM evaluates real conversations (where the agent reaches the offer/EMI) rather than the
    early hangups that dominate low-connectivity campaigns.
    """
    order = "num_messages DESC, created_at_timestamp DESC"
    if prefer_completed:
        order = "(completion_status = 'completed') DESC, " + order
    sql = f"""
    SELECT interaction_id,
           num_messages,
           on_end_agent_variables['disposition'] AS disposition,
           final_duration
    FROM {mb.table}
    WHERE campaign_id = {sql_str(campaign_id)}
      AND created_at_timestamp >= now() - INTERVAL {hours} HOUR
      AND is_deleted = 0
      AND is_debug_call = 0
      AND v2v_connectivity_status = 'connected'
      AND num_messages >= {min_messages}
    ORDER BY {order}
    LIMIT {sample_size}
    """
    return mb.query(sql)


def looks_encrypted(text: str) -> bool:
    """Transcript text in Metabase is encrypted (base64 of '<keyid>|||<org>|||<cipher>').
    Detect that so we don't feed ciphertext to the LLM and emit misleading results."""
    s = (text or "").strip()
    if len(s) < 16 or " " in s[:40]:
        return False
    try:
        return b"|||" in base64.b64decode(s[:40])
    except Exception:
        return False


def transcripts_encrypted(transcripts: dict[str, list[dict]]) -> bool:
    """True if the sampled transcripts appear encrypted (check a few messages)."""
    checked = 0
    for turns in transcripts.values():
        for t in turns[:2]:
            if looks_encrypted(t.get("content", "")):
                return True
            checked += 1
            if checked >= 6:
                return False
    return False


def _fetch_transcripts(mb: MetabaseClient, interaction_ids: list[str]) -> dict[str, list[dict]]:
    if not interaction_ids:
        return {}
    id_list = ", ".join(sql_str(i) for i in interaction_ids)
    sql = f"""
    SELECT interaction_id, turn_id, role, content
    FROM {mb.analytics_table('InteractionMessages')}
    WHERE interaction_id IN ({id_list})
      AND content != ''
    ORDER BY interaction_id, turn_id
    """
    out: dict[str, list[dict]] = {}
    for r in mb.query(sql):
        out.setdefault(str(r["interaction_id"]), []).append(
            {"role": str(r["role"]), "content": str(r["content"])}
        )
    return out


def build_conversationality_review(
    mb: MetabaseClient,
    config: Config,
    llm: LLMClient,
    campaign_ids: list[str],
) -> tuple[Report | None, list[Finding]]:
    opts = config.reports.get("conversationality_review", {})
    hours = int(opts.get("lookback_hours", 24))
    sample_size = int(opts.get("sample_size", 10))
    max_campaigns = int(opts.get("max_campaigns", 5))
    min_messages = int(opts.get("min_messages", 4))
    prefer_completed = bool(opts.get("prefer_completed", True))
    alert_below = float(opts.get("alert_below_score", 3.0))

    ids = [c for c in campaign_ids if c][:max_campaigns]
    if not ids:
        return None, []

    sections: list[str] = []
    findings: list[Finding] = []

    for campaign_id in ids:
        sampled = _sample_interactions(mb, campaign_id, hours, sample_size, min_messages, prefer_completed)
        if not sampled:
            continue
        transcripts = _fetch_transcripts(mb, [str(s["interaction_id"]) for s in sampled])
        if transcripts_encrypted(transcripts):
            sections.append(
                f"*`{campaign_id}`* — transcripts are encrypted in Metabase; "
                f"LLM review skipped (use the insights pipeline for conversation quality)."
            )
            continue

        scores: list[float] = []
        flow_counts: dict[str, int] = {}
        problem_counts: dict[str, int] = {}
        scored = 0
        for s in sampled:
            iid = str(s["interaction_id"])
            turns = transcripts.get(iid)
            if not turns:
                continue
            user_payload = json.dumps(
                {"_meta": {"interaction_id": iid, "disposition": s.get("disposition")}, "transcript": turns},
                ensure_ascii=False,
            )
            try:
                verdict = llm.json(SYSTEM_PROMPT, user_payload, max_tokens=700)
            except Exception:
                log.exception("scoring failed for %s", iid)
                continue
            scored += 1
            try:
                scores.append(float(verdict.get("overall_score", 0)))
            except (TypeError, ValueError):
                pass
            flow = str(verdict.get("flow_category", "unknown"))
            flow_counts[flow] = flow_counts.get(flow, 0) + 1
            for p in verdict.get("problems", []) or []:
                problem_counts[str(p)] = problem_counts.get(str(p), 0) + 1

        if scored == 0:
            continue
        avg = sum(scores) / len(scores) if scores else 0.0
        top_flows = sorted(flow_counts.items(), key=lambda x: -x[1])[:4]
        top_problems = sorted(problem_counts.items(), key=lambda x: -x[1])[:5]

        sections.append(
            "\n".join(
                [
                    f"*`{campaign_id}`* — {scored} calls scored · avg quality *{avg:.1f}/5*",
                    "• Flow: " + " · ".join(f"{k} {v}" for k, v in top_flows),
                    "• Top gaps: "
                    + (" · ".join(f"{k} {v}" for k, v in top_problems) or "none"),
                ]
            )
        )

        if avg < alert_below:
            top = top_problems[0][0] if top_problems else "n/a"
            findings.append(
                Finding(
                    detector="conversationality_review",
                    severity=Severity.WARNING,
                    campaign_id=campaign_id,
                    title="Conversation quality below threshold",
                    detail=(
                        f"Sampled {scored} calls scored avg *{avg:.1f}/5* "
                        f"(threshold {alert_below}). Most common gap: {top}."
                    ),
                    metrics={"avg_score": round(avg, 2), "scored": scored, "top_gap": top},
                    dedupe_key=f"{campaign_id}:conversationality_review",
                )
            )

    if not sections:
        return None, findings
    report = Report(
        kind="conversationality_review",
        title="Conversationality review (LLM)",
        sections=sections,
    )
    return report, findings
