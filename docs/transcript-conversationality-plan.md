# Plan: Conversationality / transcript checks (QC "Midday" items)

Scopes automating the **Midday Quality Check** items from the CS QC checklist:
agent looping, no-closure, hallucination, long silences, calls not ending, wrong
tool/variable-update values, and unanswerable user turns — plus the weekly evals report.

This is a **two-layer** design. Layer A is cheap and buildable now from data we already
have. Layer B is the deep transcript analysis and has LLM cost/cadence implications.

---

## Layer A — metric-based conversationality detector (buildable now, no LLM)

During exploration we found ready-made signals that cover a surprising amount of the
checklist **without reading transcripts**:

| Checklist concern | Signal (already in the data) |
|---|---|
| Agent looping | `on_end_agent_variables['bot_went_loop']` (boolean per call) |
| Oddly long silences | `EngagementFacts.average_agent_response_time_in_seconds`, `average_user_response_time_in_seconds` |
| Calls not ending / dragging | high `num_messages` combined with `end_reason = 'NO_END_REASON'` |
| Immediate hangups / broken script | ultra-short connected calls (already: `short_calls` detector) |
| Overall effectiveness dropping | `on_end_agent_variables['call_effectiveness']`, `['ai_effectiveness']` |

**Proposed `conversationality` detector** (one more detector module, same pattern as the
rest — current vs baseline per campaign):

- **Looping spike**: `bot_went_loop = true` rate now vs baseline; alert when elevated and
  above an absolute floor.
- **Silence spike**: share of connected calls with `average_agent_response_time` above a
  threshold (e.g. > 8s), vs baseline.
- **No-closure spike**: share of connected calls with `end_reason = 'NO_END_REASON'` and
  high `num_messages`, vs baseline.
- **Effectiveness drop**: mean `call_effectiveness` / `ai_effectiveness` dropping vs
  baseline (only where the agent emits these).

Effort: ~1 detector file + config block, all metric queries (cheap, same cost profile as
existing detectors). This is the recommended immediate next step.

---

## Layer B — deep transcript analysis (LLM, sampled)

For the items that genuinely need reading the conversation (hallucination, "responses
don't make sense", "user asked X and agent couldn't answer", wrong tool use / wrong
variable-update *value*), use the **`transcript-analysis` skill** framework.

- **Transcripts source**: `InteractionMessages` (per-turn role/content) in the analytics
  ClickHouse DB, or pulled via the **`metabase-samvaad` MCP**. The skill's
  `analysis-framework.md` defines the 6 scoring dimensions, the P1/P2/P3 gap taxonomy,
  and the JSON (single) / markdown (batch) output formats.
- **Flow**: per active campaign, **sample N calls** (e.g. 8–10, matching the manual QC
  cadence) from the current cycle → run each through the framework → aggregate scores →
  alert when a campaign's conversationality score drops below a threshold or specific P1
  gaps (looping, hallucination) exceed a rate.
- **Cadence & cost**: this calls an LLM per sampled transcript, so it runs on a **slower
  cadence** than the 30-min metric scan (e.g. once or twice daily per the checklist's
  11:00 and 15:00 slots), not every pass. Sampling keeps cost bounded.
- **Weekly evals report**: aggregate the batch results (and/or the evals DB `db 10`,
  `post_call_eval_results`) into the "2 reports per use-case per week" client deliverable.

Open questions before building Layer B:
- Which LLM/endpoint should scoring use, and what per-day sample budget per campaign?
- Post conversationality alerts to the same per-campaign channel, or a dedicated
  QC/Growth channel?
- Should findings auto-fill the "Master QC sheet", or just post to Slack for now?

---

## Recommendation

1. **Build Layer A now** (`conversationality` detector) — high coverage, no LLM cost.
2. **Prototype Layer B** on one campaign (e.g. Chola) once the open questions above are
   answered, then wire the weekly evals report.
