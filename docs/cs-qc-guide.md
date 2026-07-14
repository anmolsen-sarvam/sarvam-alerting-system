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

## You run it from Slack — no code, no files
You control everything the system does by messaging the bot. Changes apply on the next run,
no deploy. Three things you own:

**What's watched**
- `@alerts monitor chola.com` — watch **only** Chola (onboard client-by-client)
- `@alerts include PAPQ` — only PAPQ campaigns · `@alerts exclude test` — drop test runs
- `@alerts scope` — see what's watched · `@alerts reset` — watch everything

**Who gets paged** (engagement owners)
- `@alerts owner chola.com @you` — tag yourself (or teammates) on Chola alerts
- `@alerts owner campaign PAPQ @you` — tag on a specific campaign
- `@alerts unowner chola.com` — remove · `@alerts owner-min critical` — severity gate · `@alerts owners` — show
- No Slack bot yet? Do the same from a terminal: `sarvam-alerting owners add chola.com U0…`,
  `owners remove chola.com U0…`, `owners list` (writes the same store the scans read).

**The sanity rules** (what a value *should* be)
- `@alerts expect D2C loan_type = Digital Personal Loan | Samsung Mobile Loan`
- `@alerts expect JUNE cohort 50000` · `@alerts expected` — show

**Silence noise** (without dropping coverage)
- `@alerts mute chola.com 4h` — snooze alerts for 4h · `@alerts unmute chola.com` · `@alerts mutes`

**Ask it things** (live, on demand)
- `@alerts status` — what's active + current settings · `@alerts campaigns` — list them
- `@alerts check <campaign>` — scan one campaign right now · `@alerts report <campaign>` — cycle report

**On each alert** you also get buttons — *Ack*, *Snooze 4h*, *Mute*, *Open in Metabase* — and a
`✅ recovered` note when the problem clears. The bot's **Home** tab is a live dashboard of
everything currently set.

Full list in `docs/scope-control.md`. The only things not in Slack are secrets (API keys /
tokens) — those live in the deploy for security.

## What you need to tell it (one-time, per use-case)
The system can't know your business rules until you tell it — and you tell it **in Slack**,
not in a file:
1. **Expected values** per use-case — `expect D2C loan_type = Digital Personal Loan`. Powers
   the `expected_values` alert.
2. **Expected cohort sizes** — `expect <campaign> cohort <n>`, so it flags size mismatches.
3. **Engagement owner** per client/campaign — `owner chola.com @you`, so the right person is
   pinged when that client's campaign breaks (critical alerts only, by default).

The one thing set at deploy (not Slack): **which channels** alerts vs reports post to.

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
