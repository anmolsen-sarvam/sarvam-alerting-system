# Sarvam Alerting System — Full Overview (from first principles)

This document explains the whole project end to end, for someone with zero context.
It goes top-down: first *why* this exists, then *what* it is, then *how* every piece
works, then *what's proven to work* and *what's left to do*.

---

## Table of contents

1. [The problem (why we're building this)](#1-the-problem)
2. [Core concepts (vocabulary you need first)](#2-core-concepts)
3. [The one-paragraph summary](#3-the-one-paragraph-summary)
4. [Where the data lives](#4-where-the-data-lives)
5. [The key insight that makes detection work](#5-the-key-insight)
6. [Architecture — high level](#6-architecture--high-level)
7. [Architecture — low level (every module)](#7-architecture--low-level)
8. [The detectors, explained](#8-the-detectors)
9. [The reports, explained](#9-the-reports)
10. [Delivery: how alerts reach you](#10-delivery)
11. [How you run it](#11-how-you-run-it)
12. [Deployment (the VM story)](#12-deployment)
13. [What we've achieved (proven live)](#13-what-weve-achieved)
14. [What's still open / roadmap](#14-whats-still-open)
15. [Glossary + file map](#15-glossary--file-map)

---

## 1. The problem

Sarvam runs **outbound voice-AI calling campaigns** for clients (banks, NBFCs, etc.).
A campaign dials thousands of people; the AI agent greets each person by name and
talks about *their* specific loan, EMI, due date, and so on. Those per-person details
are called **agent variables**, and they come from a **cohort** (a CSV the client
uploads, one row per contact).

**The incident that started this:** a campaign went live and, for **3–4 days**, the
agent used **default values instead of each person's real data** — e.g. every single
call opened with "am I talking with `<the same default name>`?" instead of the actual
customer's name. Nobody noticed until someone happened to open Metabase and eyeball it.
That caused client escalations.

Today, catching this relies on **humans manually QC-ing calls** every day (there's a
"QC Best Practices" checklist: listen to 5–6 calls, check Metabase dashboards, look for
default values, weird dispositions, error spikes, etc.). That's slow, easy to miss, and
doesn't scale across many orgs.

**Goal:** automate those checks so problems are caught **within the hour, automatically,
across all orgs** — and post useful operational summaries so the team has visibility
without manually digging.

---

## 2. Core concepts

You need these terms to understand everything else:

- **Org / workspace** — a client tenant (e.g. `tatacapital-housing.com`, `licindia.com`).
  Everything is scoped by `org_id`.
- **App / agent** — the AI agent configuration that does the talking. Has an `app_id`
  and an `app_version`.
- **Campaign** — a scheduled dialing job: "call this list of people, with this agent,
  during these hours, retrying busy/no-answer up to N times." Has a `campaign_id` and a
  `status` (scheduled → active → ended/completed/cancelled/paused).
- **Cohort** — the uploaded list of contacts for a campaign (a CSV). When uploaded it's
  validated: `total_records`, `valid_records`, and `rejected_records` (rows filtered out
  for bad data).
- **Cohort transformation** — a JSON config that maps CSV columns → agent variables, and
  marks some variables `required`, some with a `fallback_value` (a default).
- **Agent variables** — the per-call values the agent uses (`customer_name`, `EMI`,
  `due_date`, `bot_name`, `gst_rate`, …). Some are **per-contact** (differ every call),
  some are **campaign-level config** (same for everyone).
- **Default / fallback** — the value used when the real per-contact value is missing or
  the mapping breaks. **The bug is when the default gets used for everyone.**
- **Disposition** — the outcome/classification of a call (connected? completed? PTP?).
- **Connectivity** — did the call connect (`connected` / `no_answer` / `busy` /
  `failed`).
- **Retry windows** — how long after a failed attempt the system re-dials
  (e.g. `[30, 30, 30]` = retry after 30, then 30, then 30 minutes).

---

## 3. The one-paragraph summary

We built a **Python service** that, on a schedule, looks at every active campaign's data
in **Metabase**, runs a set of **detectors** to catch silent regressions (most importantly
the default-variable bug), and posts **alerts** to Slack when something's wrong. It also
posts two **operational reports**: a **run summary** (what's live: cohorts, rows uploaded
vs filtered, campaign IDs, retry windows — per org) and a **cycle report** (per-campaign
performance: connectivity, engagement, disposition funnels). It works **across all orgs**,
runs **headless on a VM**, and is safe to re-run (it only alerts on *new* problems).

---

## 4. Where the data lives

Everything is queried through **Metabase's REST API** (`/api/dataset`, native SQL). We
authenticate with a **Metabase API key** — this is important because the key works
**headless** (from a VM, no browser, no login), unlike the alternative below.

Metabase exposes several databases; we use:

| DB (Metabase id) | Engine | What's in it | We use it for |
|---|---|---|---|
| **13** `SamvaadClickhouseReadReplica` | ClickHouse | **`EngagementFacts`** — one row per call: campaign_id, org_id, app_id, connectivity, completion, durations, and **`on_start_agent_variables`** (the actual variables each call used) | detectors, cycle report |
| **6** `Scheduling Service DB` | Postgres | `campaigns`, `cohorts`, `cohort_transformations` — campaign config, upload stats, variable mappings | run summary, required-variable hints |
| 10 `agent-evals` | Postgres | eval / post-call results | (future: conversationality checks) |

**The `samvaad` shell helper** (`~/bin/sarvam-helpers.sh`) is a different way to hit the
Sarvam API. We deliberately **do not** use it for v1 because it gets its auth token by
scraping a cookie from a running Chrome — that's tied to a laptop and won't work on a
headless VM. It's stubbed for future enrichment only (`clients/samvaad.py`).

---

## 5. The key insight

The naive idea — "a variable with the same value everywhere is a bug" — is **wrong**, and
real data proves it. Many variables are *supposed* to be constant across a campaign:
`bot_name` = "नेहा", `gst_rate` = 18, `campaign_code` = "CMP0526". Flagging those would be
pure noise.

The **real fingerprint** of the bug is: a variable that is **normally per-contact**
suddenly **collapses to one value**. So every detector compares a campaign against **its
own recent history (a baseline)**, not an absolute threshold.

Two signals tell us a variable is "supposed to be per-contact":

1. **History (baseline):** in the last few days this variable had *many* distinct values
   (e.g. `customer_name` had 386,690 distinct values). If it drops to 1, that's the bug.
2. **Intent (`required` flag):** the cohort transformation marks the variable `required`,
   meaning it *must* come from the uploaded cohort. A required variable that collapses is
   a high-confidence bug — and this works **even on a brand-new campaign** before any
   history exists (the "catch it the moment the campaign goes live" case).

That's the heart of the whole system.

---

## 6. Architecture — high level

```
                 ┌─────────────────────────────────────────────┐
                 │              Metabase REST API               │
                 │   ClickHouse (calls)   +   Postgres (config) │
                 └───────────────┬─────────────────────────────┘
                                 │ native SQL
                 ┌───────────────▼───────────────┐
                 │            Engine              │
                 │  1. discover active campaigns  │
                 │  2. run detectors per campaign │
                 │  3. build reports              │
                 └───────┬───────────────┬────────┘
                         │ findings      │ reports
                 ┌───────▼──────┐  ┌─────▼──────────┐
                 │ State store  │  │   Notifiers    │
                 │ (dedupe so   │  │ console /      │
                 │ we alert on  │  │ Slack webhook /│
                 │ NEW problems)│  │ Slack bot      │
                 └──────────────┘  └────────────────┘
```

Two independent output **streams**:

- **alerts** — severity findings from detectors (deduped).
- **reports** — always-posted digests (run summary, cycle report).

A notifier subscribes to one or both.

---

## 7. Architecture — low level

The code lives in `src/sarvam_alerting/`. Module by module:

- **`config.py`** — loads `config/config.toml` (all tunables) and pulls secrets from env
  (`METABASE_API_KEY`, Slack creds). Config is secret-free and committable.
- **`models.py`** — the core data types:
  - `Severity` (info/warning/critical),
  - `CampaignInfo` (a discovered campaign),
  - `Finding` (one detected problem: severity, campaign, title, detail, metrics,
    `dedupe_key`, `org_id`),
  - `Report` (a digest: title + sections).
- **`clients/metabase.py`** — runs native SQL against any Metabase DB (defaults to the
  ClickHouse facts DB; pass `scheduling_db` for Postgres). Handles the quirk that the
  dataset endpoint returns **HTTP 202** (not 200) on success, and retries on 5xx.
- **`clients/scheduling.py`** — Postgres queries: `active_campaign_runs()` (campaigns +
  cohort stats + retry windows) and `required_variables()` (an app's required-variable
  set from the latest transformation).
- **`clients/samvaad.py`** — stub for future Sarvam-API enrichment (not used in v1).
- **`detectors/`** — one file per detector (see §8). Each is a class with a `run(ctx)`
  that returns `Finding`s. `ctx` (`DetectorContext`) carries the Metabase client, the
  campaign, config, and helpers to build the "current vs baseline" SQL windows.
- **`baseline` concept** — not a separate file; it's the `current_hours` vs
  `baseline_hours` windows every detector uses (see `[windows]` in config).
- **`state.py`** — a SQLite store. Records each finding's fingerprint + when it was last
  alerted, so the same problem isn't re-spammed within `cooldown_hours`.
- **`reports/`** — `run_summary.py` and `cycle_report.py` build `Report` objects.
- **`notify/`** — the delivery layer (see §10): `console`, `slack_webhook`, `slack_bot`,
  a shared `slack_format` (Block Kit), and a `base` interface with the `streams` concept.
- **`engine.py`** — orchestration: `discover_campaigns()`, `run_scan()` (runs detectors
  over all campaigns), `build_reports()`.
- **`cli.py`** — the command-line entry point (see §11).

**Data flow of one scan pass:**
`discover_campaigns` → for each campaign build a `DetectorContext` → run each enabled
detector → collect `Finding`s (stamped with `org_id`) → filter to *new* ones via the
state store → hand to notifiers (alerts stream) → build reports → hand to notifiers
(reports stream) → mark findings notified.

---

## 8. The detectors

All live in `src/sarvam_alerting/detectors/`. Each compares a **current window** (default
last 6h) against a **baseline window** (default previous 72h) for one campaign.

1. **`variable_collapse`** (the priority — catches the escalation bug)
   - Pulls per-variable stats from `on_start_agent_variables`: how many distinct values
     now vs in the baseline.
   - Flags a variable if it **collapsed to one value now** AND was **per-contact before**
     (many distinct values in baseline) **or** is marked **`required`** in the cohort
     transformation.
   - Result: "Variable `customer_name` collapsed to a single value — all 800 calls used
     'Rahul'; baseline had 4,200 distinct values. Default-variable fallback."

2. **`connectivity`** — connected-rate dropping sharply vs baseline, or an absolute
   failure-rate ceiling (catches dialer/telephony/config breakage).

3. **`short_calls`** — a spike in ultra-short *connected* calls vs baseline (proxy for a
   broken script/agent or immediate hangups).

4. **`errors`** — elevated `has_log_issues` rate vs baseline.

Each finding has a **severity** (critical/warning) and a stable **dedupe key** so it
alerts once per problem per cooldown, not every run.

---

## 9. The reports

Digests posted to an **automation-alerts** channel (any notifier subscribed to the
`reports` stream). Unlike alerts, they're posted every run regardless of "problems".

1. **`run_summary`** (`reports/run_summary.py`) — "what is the automation doing right now",
   grouped by org. Per campaign: status, cohort count, **rows uploaded vs filtered**,
   campaign ID, **retry windows**. Answers: which cohorts are active, how many rows were
   filtered vs uploaded, what the retry config is.

2. **`cycle_report`** (`reports/cycle_report.py`) — per-campaign performance funnel:
   dialed → connectivity % (+ breakdown of no_answer/busy/failed) → engagement % of
   connected → completion %, plus avg call duration. This is the post-cycle performance
   report (connectivity, engagement, disposition funnels).

---

## 10. Delivery

Notifiers implement one interface and subscribe to `streams` (`alerts` and/or `reports`):

- **`console`** — rich terminal output; used by the on-demand `check` command.
- **`slack_webhook`** — HTTP POST to a Slack incoming-webhook URL. **One webhook = one
  channel.** Simplest, fully headless. Point one webhook at your automation-alerts channel
  for reports, another at an alerts channel.
- **`slack_bot`** — a Slack bot token (`chat.postMessage`). One token posts to **any**
  channel; supports **per-org** and **per-campaign** alert routing, and a dedicated
  **report channel**.

**Why not the Slack MCP for the scheduled job?** The MCP runs through an interactive editor
session (laptop on, editor open) — wrong for an unattended daemon. MCP is for interactive
use; the webhook/bot token is for the automation.

**Dedupe/cooldown** (`state.py`) ensures you get alerted when a problem *appears*, and
again only after it's been quiet for `cooldown_hours` — no spam.

---

## 11. How you run it

Requires Python ≥ 3.11 and `uv`.

```bash
uv sync                                        # install deps
cp config/config.example.toml config/config.toml
cp .env.example .env                           # then set METABASE_API_KEY (+ Slack creds)

uv run sarvam-alerting list-campaigns          # what's active right now
uv run sarvam-alerting check                   # scan all active campaigns for alerts
uv run sarvam-alerting check -c <campaign_id>  # scan one campaign
uv run sarvam-alerting run-summary             # post the automation run summary
uv run sarvam-alerting cycle-report -c <id>    # post a campaign performance report
uv run sarvam-alerting test-notify             # verify Slack wiring
uv run sarvam-alerting watch --interval 30     # the scheduled loop (alerts + reports)
uv run sarvam-alerting watch --once            # single pass (for cron/systemd timers)
```

All tunables (windows, thresholds, discovery, notifiers, reports) are in
`config/config.toml`, which is heavily commented (`config/config.example.toml`).

---

## 12. Deployment

Production runs on the shared **Airflow / Kubernetes** platform (`sarvamai/airflow-dags`)
under the `samvaad` team — the VM/systemd path is deprecated. Four DAGs (in `airflow/`):

- `samvaad_alerting_scan` — every 30 min: detectors + run summary → Slack.
- `samvaad_cycle_report` — daily: connectivity/engagement/PTP/disposition funnels.
- `samvaad_conversationality_review` — daily: LLM transcript scoring (Layer B).
- `samvaad_weekly_evals` — weekly: insights digest for the client.

They run the custom `samvaad` image (installs this package + boto3), read a secret-free
`config.toml` baked into the image, and pull secrets (Metabase/Slack/Azure keys) from the
`samvaad-alerting-secrets` K8s secret. Because pods are ephemeral, the scan DAG syncs its
dedupe state to/from **S3**. Full steps in `airflow/README.md`. The CLI remains for
local/ad-hoc runs.

---

## 13. What we've achieved (proven live)

All of the following ran against **live production Metabase** during development:

- **Data model fully mapped** — confirmed `EngagementFacts` (calls) + the scheduling DB
  (campaigns/cohorts/transformations) have everything needed.
- **`variable_collapse` validated on real data** — on the Chola `VFD-PAPQ` campaign,
  `customer_name` showed **180,352 distinct values** (healthy, correctly *not* flagged),
  while config vars like `initial_language` (10 distinct) were correctly ignored. The
  detector would fire the instant `customer_name` collapsed to one value — the exact
  escalation scenario. Fire path covered by unit tests.
- **All 4 detectors run clean** end-to-end on a live campaign.
- **Run summary works** — produced a live digest across **364 campaigns / 32 orgs**,
  ~**2.5M rows uploaded**, ~**16k filtered**, with per-campaign retry windows.
- **Cycle report works** — for `PAPQ-11th` (tatacapital): **31,168 dialed · 42%
  connectivity · 65% engagement of connected · avg 36.2s · 20% completed**, with
  connectivity breakdown.
- **Multi-org** built in (discovery, detectors, reports, and per-org Slack routing).
- **Tests pass, no lint errors.** Runs on Python 3.14 with `uv`; dependencies are
  pure-Python (`httpx`, `typer`, `rich`).

---

## 14. What's still open

- **Slack credentials + channels** — you need to create the webhook(s) / bot token, put
  them in `.env`, and enable the notifier block(s) in config. Then `test-notify`.
- **VM scheduling** — check the repo out on the VM and install the systemd timer (or cron).
- **CS-team checklist** — the manual QC checklist from @cs-team should be folded in; each
  item becomes a config-driven detector. Framework is ready for it.
- **Transcript conversationality checks (v2)** — using the `transcript-analysis` skill +
  `metabase-samvaad` MCP: sample each campaign's calls and score for looping, no-closure,
  wrong variable-update values, etc.; alert when quality drops.
- **Name the exact configured default** — currently we detect the *collapse*; naming the
  precise agent-configured default value needs the Samvaad config API (v2).
- **Cheaper discovery** — the full-table 6h scan occasionally times out (504) on the
  Metabase gateway; a cached/narrower discovery would make it snappier.
- **Cadence tuning** — decide whether `cycle_report` runs on the 30-min `watch` or a
  separate daily timer (currently disabled in `watch` by default; run via CLI/daily timer).

---

## 15. Glossary + file map

**Key tables**
- `EngagementFacts` (ClickHouse, db 13) — one row per call; `on_start_agent_variables` is
  the map of variables that call actually used.
- `campaigns`, `cohorts`, `cohort_transformations` (Postgres, db 6) — campaign config,
  upload stats, variable mappings/required flags.

**File map** (under `sarvam-alerting-system/`)
```
config/config.example.toml   # every setting, commented
src/sarvam_alerting/
  config.py                  # config + secrets loading
  models.py                  # Severity, Finding, Report, CampaignInfo
  engine.py                  # discover → detect → report orchestration
  cli.py                     # commands: check / watch / run-summary / cycle-report / ...
  state.py                   # SQLite dedupe/cooldown
  clients/metabase.py        # native SQL over ClickHouse + Postgres
  clients/scheduling.py      # campaigns/cohorts/required-vars queries
  clients/samvaad.py         # (v2 stub)
  detectors/                 # variable_collapse, connectivity, short_calls, errors
  reports/                   # run_summary, cycle_report
  notify/                    # console, slack_webhook, slack_bot, slack_format, base
deploy/                      # systemd service+timer, cron example
tests/                       # detector unit tests
README.md                    # quickstart + reference
OVERVIEW.md                  # this document
```

**Mental model in one line:** *pull each live campaign's calls from Metabase, compare
each variable/metric to its own recent history, alert on collapses/spikes, and post
run + performance digests — across all orgs, headless on a VM.*
