# Low-level review — the mechanics

For someone who wants to know how it actually works under the hood.

## Stack
- **Python ≥ 3.11**, managed with `uv`. Pure-Python deps only: `httpx`, `typer`, `rich`
  (plus `boto3`/`gspread` optionally for S3 state and the Google-Sheet sink).
- No ORM, no framework. Data access is **native SQL through the Metabase REST API**
  (`POST /api/dataset`). Aggregation happens server-side in ClickHouse/Postgres; we pull
  back small result sets.

## Data sources (all via Metabase)
| Metabase DB | Engine | Key objects | Notes |
|---|---|---|---|
| 13 | ClickHouse | `EngagementFacts` (1 row/call) | facts: connectivity, completion, durations, `on_start/on_end_agent_variables` (`Map(String,String)`), `has_log_issues` |
| 6 | Postgres | `campaigns`, `cohorts`, `cohort_transformations` | scheduling/config; `cohorts.result` has total/valid/rejected |
| 10 | Postgres | `insights_result`, `insights_run` | upstream eval metrics keyed by `campaign_id` |

Hard facts / data-quality rules (learned the hard way, some from the analytics-copilot team's schema notes):
- **Transcript text is encrypted** (`InteractionMessages.content`, `on_end_interaction_transcript`) —
  base64 of `<keyid>|||<org>|||<AES ciphertext>`. Structured fields are plaintext.
- **`/api/dataset` returns HTTP 202** (not 200) on success — the client treats 200/202 as OK.
- **Always filter `is_debug_call = 0`** (and `is_deleted = 0`) on every EngagementFacts query,
  or internal test calls pollute the metrics. (Every query does this.)
- **`completion_status` is unreliable** — often `UNKNOWN`, and for cold outbound "failed"
  mostly means "no-answer," so an *absolute* failure rate is meaningless (85-90% is normal).
  Use `v2v_connectivity_status` for connectivity, and compare **relative to baseline**, never
  an absolute ceiling. (This is why the old absolute failure-rate alert was a false positive.)
- Date filters use `created_at_timestamp` (non-null); `start_datetime` is ~25% NULL.

## The scan lifecycle (one pass)
`engine.run_scan()` →
1. **`discover_campaigns()`** — source is configurable:
   - `scheduling` (default): `SELECT DISTINCT campaign_id, org_id FROM campaigns WHERE status IN ('active','running')`
     (cheap), then **one bounded** `SELECT campaign_id, count() … WHERE campaign_id IN (…) AND created_at >= now()-INTERVAL 6 HOUR GROUP BY campaign_id HAVING count() >= min_calls`.
     This avoids the full-table scan that caused gateway **504s**.
   - `facts`: the old full-table aggregation (kept as fallback).
2. For each `CampaignInfo`, build a **`DetectorContext`** (metabase client + campaign + config,
   plus helpers `window_case()`, `base_where()`, `campaign_literal`).
3. Run every enabled **detector**; each returns `list[Finding]`. Exceptions are caught
   per-detector so one bad query doesn't sink the pass.
4. Findings are stamped with `org_id` (via `dataclasses.replace`).

`engine.build_reports()` builds the enabled digests separately.

## The baseline mechanism (the core algorithm)
Every detector compares a **current window** (default 6h) to a **baseline window**
(default prior 72h) *for the same campaign*, in one query, using ClickHouse conditional
aggregation:
```sql
window_case = if(created_at_timestamp >= now() - INTERVAL 6 HOUR, 'cur', 'base')
-- then: countIf(win='cur' AND …), countIf(win='base' AND …), etc.
```
`variable_collapse` is the flagship: it `ARRAY JOIN`s `mapKeys/mapValues` of
`on_start_agent_variables`, computes per-variable distinct counts in cur vs base, and flags
a variable that was per-contact in baseline (`distinct_base` high) but collapsed now
(`distinct_cur <= 1`). It also consults `cohort_transformations` for the `required` flag so
it can fire on brand-new campaigns with no baseline.

## Module map
```
config.py     TOML + env (secrets via *_env keys); dataclasses; load_config()
models.py     Severity (info/warning/critical, comparable), Finding (has fingerprint,
              org_id, interaction_ids, dedupe_key), Report, CampaignInfo
clients/
  metabase.py  query(sql, database_id=…); 200/202 handling; retries on 5xx; table helpers
  scheduling.py active_campaign_runs(), required_variables(), campaign_cohort_totals()
  llm.py        Azure OpenAI chat; JSON mode; auto-adapts max_tokens↔max_completion_tokens
                (gpt-5) and drops temperature when rejected; optional fallback model
  samvaad.py    stub (token is Chrome-bound; unused)
detectors/    base.Detector (ABC) + DetectorContext; 8 detectors; DETECTOR_CLASSES registry
reports/      run_summary, cycle_report, weekly_evals, client_report,
              conversationality_review + value_correctness (LLM, encryption-guarded)
notify/       base.Notifier (min_severity + streams {alerts,reports} + links);
              console, slack_webhook, slack_bot (threads/routing), gsheet; slack_format
state.py      SQLite dedupe: is_new(finding, cooldown) / mark_notified()
engine.py     discover_campaigns, run_campaign, run_scan, build_reports
cli.py        typer app: check / watch / run-summary / cycle-report / weekly-evals /
              conversationality-review / value-correctness / client-report / test-notify
deeplinks.py  builds Metabase/call links from config templates
```

## Detectors (signal → SQL basis)
| Detector | Basis |
|---|---|
| `variable_collapse` | per-variable distinct cur vs base on `on_start_agent_variables` + `required` hint |
| `required_populated` | `countIf(on_start_agent_variables['v']='')` for required vars |
| `expected_values` | value distribution vs config `allowed` set; cohort size vs config |
| `connectivity` | `v2v_connectivity_status='connected'` rate drop; `completion_status='failed'` ceiling |
| `short_calls` | `audio_duration <= N` share among connected, cur vs base |
| `errors` | `has_log_issues` rate cur vs base |
| `disposition_accuracy` | `avg(numeric_value)` where `metric_name='confidence_disp_accuracy_score'` (insights) |
| `insights_quality` | `avg(boolean_value)` per safety/quality metric (insights), vs thresholds |

## Delivery + dedupe
- `Notifier.notify(findings)` filters by `min_severity`; `deliver_report(report)` for digests.
  A notifier subscribes to `streams` (`alerts` and/or `reports`).
- `slack_bot` posts one parent message per campaign, findings as **threaded replies**;
  routes by `org_channels` / `channels`; reports go to `report_channel`.
- `state.py` fingerprints each finding (`sha1(dedupe_key)`); re-alerts only after
  `cooldown_hours`. On Kubernetes the SQLite file is synced **to/from S3** around each run
  (ephemeral pods).

## Config
One TOML (`config/config.toml`), secrets from env via `*_env` keys. Sections: `[metabase]`,
`[windows]`, `[discovery]`, `[detectors.*]`, `[reports.*]`, `[[expected]]` rules, `[llm]`,
`[[notify]]` blocks, `[state]`, `[links]`. On Airflow the config is baked (secret-free) into
the custom image; secrets come from a K8s Secret.

## Deployment
`airflow/samvaad/*_dag.py` — 6 DAGs (scan every 30 min; cycle/conversationality/value/weekly/
client on daily/weekly cron). Custom image `images/samvaad` installs the package from git.
Secrets via `pod_override` env from `samvaad-alerting-secrets`. Scan syncs dedupe state to S3.

## Testing / verifying
`tests/` unit-tests detector logic with a fake Metabase client (no network).
`scripts/verify_campaign.py <id> <org> <out.md>` runs every functionality against one live
campaign and writes a markdown report (see `verification/`).
