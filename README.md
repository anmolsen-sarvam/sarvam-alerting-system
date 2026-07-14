# Sarvam Alerting System

Autonomous quality-control and regression watchdog for Sarvam voice-AI **campaign
deployments**. It continuously watches every active campaign and raises Slack alerts for
**silent** failures — most importantly the *default-variable* bug, where a per-contact
variable (customer name, EMI amount, due date, …) collapses to a single default value for
the whole cohort.

> **Why:** a campaign once ran for 3–4 days with every call opening "am I talking with
> `<same default name>`?" before anyone noticed in Metabase — causing client escalations.
> Nothing *errored*; the failure was in the call *content*. This system is built to catch
> that class of silent regression within the hour.

> **Positioning — a control, not a copilot.** Think CI checks + smoke tests for campaigns,
> not a chat-with-your-data analytics assistant. It runs itself: no one has to ask it
> anything. It encodes the CS/QC team's checklist as always-on, deterministic guardrails.

---

## What it detects

Every detector compares a campaign's **current window** (default 6h) against its **own
baseline** (prior 72h) — never an absolute threshold — so variables that are *legitimately*
constant (bot name, GST rate) never false-positive.

| Detector | Catches |
|---|---|
| `variable_collapse` | the default-variable bug — a per-contact variable collapsing to one value |
| `value_sanity` | placeholder / un-rendered-template (`{{name}}`) / implausible variable values (rules + optional LLM) |
| `stalled_campaign` | a campaign active with a cohort uploaded but (almost) no calls — dialing stalled |
| `required_populated` | a required cohort column arriving blank ("Overview column not populated") |
| `expected_values` | wrong loan type / mismapped value / cohort size ≠ what the client sent |
| `connectivity` | connected-rate drop or dialer-failure spike (baseline-relative) |
| `short_calls` | spike in ultra-short connected calls (broken script/agent) |
| `errors` | runtime error-rate surge |
| `disposition_accuracy` | agent tagging call outcomes wrong (from the insights pipeline) |
| `insights_quality` | hallucination / looping / escalation-miss / safety issues (insights pipeline) |

## Reports (digests, not alarms)

| Report | Contents |
|---|---|
| `run_summary` | per org: active campaigns, cohorts, rows uploaded vs filtered, campaign IDs, retry windows |
| `cycle_report` | per campaign: connectivity, engagement, PTP %, disposition breakdown |
| `weekly_evals` | per campaign: hallucination / loop / escalation rates + effectiveness scores |
| `client_report` | per-org markdown deliverable for clients |

## How it works

```
discover active campaigns (scheduling DB)
        │  for each campaign
        ▼
   run detectors (ClickHouse aggregation via Metabase)
        │ findings                     │ reports
        ▼                              ▼
   dedupe / cooldown (SQLite/S3) ──► notifiers ◄── digests
                                  console · Slack webhook · Slack bot · Google Sheet
```

Data is read via the **Metabase REST API** (headless, works on a VM/pod): `EngagementFacts`
(ClickHouse) for calls, the scheduling DB (Postgres) for campaigns/cohorts, and the insights
pipeline for conversation-quality. Note: **transcript text is encrypted at rest**, so
conversation-quality signals come from the insights pipeline, not from re-reading calls.

## Quickstart

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp config/config.example.toml config/config.toml
cp .env.example .env          # set METABASE_API_KEY (+ Slack / Azure keys as needed)

uv run sarvam-alerting list-campaigns          # what's active now
uv run sarvam-alerting check                   # scan all active campaigns for alerts
uv run sarvam-alerting check -c <campaign_id>  # scan one campaign
uv run sarvam-alerting run-summary             # post the automation digest
uv run sarvam-alerting cycle-report            # post per-campaign performance
uv run sarvam-alerting weekly-evals            # post the weekly insights digest
uv run sarvam-alerting client-report           # write per-org client reports
uv run sarvam-alerting owners list             # view engagement owners (who gets paged)
uv run sarvam-alerting owners add chola.com U0ANMOL   # add owner (dotted key => org)
uv run sarvam-alerting owners remove chola.com U0ANMOL # remove owner (omit id = whole key)
uv run sarvam-alerting test-notify             # verify notifier wiring
uv run sarvam-alerting watch --once            # a single scheduled pass (for cron/Airflow)
```

## Configuration

Everything lives in `config/config.toml` (see `config.example.toml` for the fully-commented
reference). Secrets are **never** in config — they come from env vars named by `*_env` keys.
Highlights:

- `[windows]` — current vs baseline window sizes.
- `[discovery]` — source, and **scoping** (`only_orgs` / `exclude_orgs` / `include_patterns`
  / `exclude_patterns`) so you monitor specific clients/campaigns, not all 30+ orgs.
- `[detectors.*]` — per-detector thresholds.
- `[[expected]]` — CS-owned rules: allowed loan types, expected cohort sizes.
- `[owners]` — map org/campaign → Slack ids to `@`-mention the engagement owner on alerts.
- `[[notify]]` — `console` / `slack_webhook` / `slack_bot` / `gsheet`, each with `streams`
  (`alerts` and/or `reports`).
- `[llm]` — Azure model used by `value_sanity`'s optional adjudication and the
  (encryption-guarded) transcript checks.

### Slack (the control plane)
- **Alerts/reports** post via an incoming webhook (`SLACK_WEBHOOK_URL`) or a bot token
  (any channel, per-campaign threads, per-org routing).
- **Engagement-owner tagging** — critical alerts `@`-mention the person who owns that client
  (severity-gated to stay low-noise). Turns an alert into a direct page, not just a post.
- **CS/QC run it as a two-way console from Slack — no file edits.** An always-on Socket Mode
  service (`sarvam-alerting control-server`) reads/writes a shared store and answers live
  queries; every scan reads the store on its next pass:
  - **scope** — `monitor chola.com` · `include PAPQ` · `exclude test` · `scope` · `reset`
  - **owners** — `owner chola.com @you` · `owner-min critical` · `owners`
  - **expected values** — `expect D2C loan_type = Digital Personal Loan | Samsung Mobile Loan`
    · `expect JUNE cohort 50000` · `expected`
  - **mutes** — `mute chola.com 4h` · `unmute chola.com` · `mutes`
  - **queries** — `status` · `campaigns` · `check <campaign>` · `report <campaign>` · `feedback`
  - **alert buttons** — Ack / Snooze / Mute on each alert · `✅ recovered` when it clears
  - **App Home** — a live dashboard of scope, owners, rules & mutes

  Only real secrets (API keys, Slack tokens) stay in env. See
  [`docs/scope-control.md`](docs/scope-control.md).
- The `[owners]` / `[[expected]]` blocks in `config.toml` still work as seed defaults that
  Slack overrides — useful for baking a baseline into the deploy image.

## Deployment

Production runs as **Airflow DAGs on Kubernetes** (30-min scan + daily/weekly reports). The
bundle — DAGs, custom image, K8s-secret wiring — is in [`airflow/`](airflow/README.md). The
CLI remains for local/ad-hoc runs. (The old VM `systemd`/`cron` files in `deploy/` are
deprecated, kept for reference.)

## Project layout

```
src/sarvam_alerting/
  config.py · models.py · engine.py · cli.py · state.py · scope.py · owners.py · deeplinks.py
  clients/    metabase · scheduling · llm · samvaad(stub)
  detectors/  10 detectors + scan-level stalled-campaign check (see table above)
  reports/    run_summary · cycle_report · weekly_evals · client_report · (+LLM, guarded)
  notify/     console · slack_webhook · slack_bot · gsheet
  control/    slack scope-control (Socket Mode)
airflow/      DAGs + custom image + deploy guide
docs/         the docs above
tests/        unit tests (fake Metabase client, no network)
```

## Development

```bash
uv run pytest          # unit tests
```

## Note on committed content

Secrets (`.env`), local editor/tooling config, the local `config/config.toml`, and generated
artifacts with **live client data** (`client-reports/`, `verification/`) are git-ignored on
purpose. Never commit real API keys, tokens, or client data.
