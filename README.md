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
- `[[notify]]` — `console` / `slack_webhook` / `slack_bot` / `gsheet`, each with `streams`
  (`alerts` and/or `reports`).
- `[llm]` — Azure model for the (optional, encryption-guarded) transcript checks.

### Slack + scope control
- **Alerts/reports** post via an incoming webhook (`SLACK_WEBHOOK_URL`) or a bot token
  (any channel, per-campaign threads, per-org routing).
- **CS/QC control the scope from Slack** — `@alerts monitor chola.com`, `include PAPQ`,
  `exclude test`, `scope`, `reset` — via an always-on Socket Mode service
  (`sarvam-alerting control-server`). See [`docs/scope-control.md`](docs/scope-control.md).

## Deployment

Production runs as **Airflow DAGs on Kubernetes** (30-min scan + daily/weekly reports). The
bundle — DAGs, custom image, K8s-secret wiring — is in [`airflow/`](airflow/README.md). The
CLI remains for local/ad-hoc runs. (The old VM `systemd`/`cron` files in `deploy/` are
deprecated, kept for reference.)

## Documentation

| Doc | For |
|---|---|
| [`docs/high-level-review.md`](docs/high-level-review.md) | the big picture, in plain terms |
| [`docs/low-level-review.md`](docs/low-level-review.md) | the mechanics, for engineers |
| [`docs/cs-qc-guide.md`](docs/cs-qc-guide.md) | how CS/QC/FDSE use it day-to-day |
| [`docs/pm-brief.md`](docs/pm-brief.md) | personas, rollout, metrics, roadmap |
| [`docs/scope-control.md`](docs/scope-control.md) | controlling scope from Slack |
| [`docs/deep-dive.html`](docs/deep-dive.html) | first-principles deep dive |
| [`OVERVIEW.md`](OVERVIEW.md) | end-to-end system overview |

## Project layout

```
src/sarvam_alerting/
  config.py · models.py · engine.py · cli.py · state.py · scope.py · deeplinks.py
  clients/    metabase · scheduling · llm · samvaad(stub)
  detectors/  8 detectors (see table above)
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
