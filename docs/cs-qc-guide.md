# For CS / QC / FDSE — how this helps you and how to use it

You know the daily QC grind: open Metabase, check connectivity, listen to 5–6 calls hoping
to spot a default value or a wrong EMI, watch the tracking channel for error spikes, fill
the Master QC sheet. It's slow, and things slip through (that's how the default-variable bug
ran for 4 days).

**This system does that watching for you and pings you when something's actually wrong.**
You stop hunting for problems; the problems come to you with the evidence attached.

## What lands in your Slack — and what to do

Each alert names the campaign, says what's wrong in plain language, and links to the evidence
(the Metabase view + example calls). Here's how to read the main ones:

| Alert | What it means | What you do |
|---|---|---|
| 🔴 **Variable `customer_name` collapsed to a single value** | The default-variable bug — the agent is using one value for everyone instead of each person's. | **Escalate to FDSE now.** The cohort mapping is broken; the campaign is effectively broadcasting wrong info. |
| 🔴 **Required variable `emi_amount` is blank** | A column the client sent isn't reaching the agent (empty for many calls). | Flag to FDSE — check the cohort upload / transformation. |
| 🟡/🔴 **Unexpected values for `loan_type`** | The agent is using a loan type outside what's allowed for this use-case (e.g. not "Digital Personal Loan" for D2C). | Verify the mapping and the sample cohort with FDSE. |
| 🔴 **Failure-rate critically high** | Most calls aren't completing (dialer/telephony/number issue). | Check the number pool / connection config; flag to FDSE. |
| 🟡 **Connectivity dropped sharply** | Connected-rate fell vs this campaign's normal. | Sanity-check the numbers/spam status. |
| 🟡 **Spike in ultra-short connected calls** | Many calls connect and die in seconds — often a broken script/agent. | Listen to 2–3 of the linked calls; flag if the agent is broken. |
| 🔴 **High `hallucination` rate** / 🟡 **High `loop_detection` rate** | Sarvam's own eval says the agent is hallucinating or looping on this campaign. | Flag to FDSE/Growth for agent/prompt fixes. |
| 🔴 **Dispositions being marked inaccurately** | The agent is tagging outcomes wrong. | Flag to FDSE — disposition logic needs review. |
| **Cohort size differs from expected** | Rows uploaded ≠ what the client said they'd send. | Confirm with the client / re-check the cohort file. |

> Alerts are **deduped**: you get pinged when a problem *appears*, and again only if it's
> still happening after the cooldown. No spam.

## The reports you get (no action needed unless something stands out)

- **Run summary** (each run) → the automation-alerts channel: which campaigns are live, per
  client, **rows uploaded vs filtered out**, cohort counts, retry windows. Your morning
  "is everything set up right?" glance — replaces the start-of-campaign sanity check.
- **Cycle report** (daily, after calling hours): per campaign — connectivity %, engagement %,
  **PTP %**, and the **disposition breakdown** (e.g. `NA 58% · Voicemail 25% · Not Interested 8%`).
  Replaces the "check Metabase dashboard metrics" part of midday QC.
- **Weekly evals**: per-campaign hallucination / looping / escalation-miss rates + effectiveness
  scores — the basis for the client eval reports.
- **Client report**: a per-client markdown summary you can clean up and share.

## Your day, before vs after

| Old manual step (QC checklist) | Now |
|---|---|
| 11:00 sanity: connectivity, cohort size, failure rates | Run summary + connectivity/failure alerts — automatic |
| Listen for default values / wrong mappings | `variable_collapse` / `required_populated` / `expected_values` alerts |
| Check tracking channel for error spikes | `errors` + `connectivity` alerts |
| Midday: engagement %, PTP %, disposition breakdown, short calls | Cycle report + `short_calls` alert |
| QC calls for looping / hallucination | `insights_quality` alerts (from Sarvam's evals) |
| Weekly evals report for client | Weekly evals + client report |

**Still manual (for now):** the pre-launch **spam-number check** and setting up the
**test campaign** — those need inputs the system can't pull yet.

## Controlling what's monitored (from Slack, no code)
You decide which clients/campaigns are watched by messaging the bot — changes apply on the
next run, no deploy:
- `@alerts monitor chola.com` — watch **only** Chola (onboard client-by-client)
- `@alerts include PAPQ` — only PAPQ-type campaigns · `@alerts exclude test` — drop test runs
- `@alerts scope` — see what's currently monitored · `@alerts reset` — watch everything
Full list in `docs/scope-control.md`.

## What you need to give it (one-time, per use-case)
The system can't know your business rules until you tell it. Hand these to whoever configures it:
1. **Expected values** per use-case — e.g. "D2C loan type must be *Digital Personal Loan*",
   "Samsung must be *Samsung Mobile Loan*". These power the `expected_values` alert.
2. **Expected cohort sizes** — roughly how many rows each client sends, so it can flag mismatches.
3. **Which Slack channels** alerts and reports should go to (per client if you want).

## Important honesty
- It reads **numbers and variables**, not the **words** of the call (transcripts are
  encrypted). So "is the agent hallucinating/looping?" comes from Sarvam's existing eval
  scores, not from the tool re-reading calls. It's a strong signal, but if you need to
  confirm *exactly what was said*, you still listen to the linked calls.
- It's **not live yet** — once Slack + deployment are wired, this all starts flowing
  automatically. Until then it can be run on-demand for any campaign.

## The point
You were the safety net catching silent failures by luck and diligence. This makes the
safety net automatic, consistent, and fast — so a broken campaign gets caught in the first
hour, not on day four, and you spend your time fixing issues instead of hunting for them.
