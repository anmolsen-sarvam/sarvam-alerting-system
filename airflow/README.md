# Deploying on the Airflow platform

This replaces the old VM/systemd deployment. The alerting jobs run as DAGs on the shared
Airflow/Kubernetes platform (`sarvamai/airflow-dags`), under the **`samvaad`** team.

## What's here

```
airflow/
├── samvaad/                              # -> copy into airflow-dags/samvaad/
│   ├── alerting_scan_dag.py              # every 30 min: detectors + run summary -> Slack
│   ├── cycle_report_dag.py               # daily ~19:30 IST: connectivity/engagement/PTP/dispositions
│   ├── conversationality_review_dag.py   # daily ~20:30 IST: LLM transcript scoring (Layer B)
│   ├── value_correctness_dag.py          # daily ~21:30 IST: LLM "are values stated correctly?"
│   ├── weekly_evals_dag.py               # Mondays: insights digest for the client
│   └── client_report_dag.py              # Mondays: per-org client reports -> S3
└── images/samvaad/                       # -> copy into airflow-dags/images/samvaad/
    ├── Dockerfile
    ├── pyproject.toml                    # installs sarvam-alerting (from git) + boto3
    └── config.toml                       # secret-free config, baked to /opt/sarvam-alerting/config.toml
```

## Prerequisites (one-time)

1. **Publish this repo** to `github.com/sarvamai/sarvam-alerting-system` (the image
   installs it via `git+https`). Pin `pyproject.toml`'s dependency to a tag/commit.
2. **Airflow access** (platform team): request the `samvaad-team` RBAC role for your
   team members, and write access to `sarvamai/airflow-dags`.
3. **K8s secret** (platform team): create `samvaad-alerting-secrets` with keys:
   | secret key | value |
   |---|---|
   | `metabase-api-key` | the Metabase API key |
   | `slack-bot-token` | Slack bot token (`xoxb-…`, scope `chat:write`, invited to the channels) |
   | `slack-webhook-url` | (optional) incoming webhook URL |
   | `azure-openai-api-key` | Azure OpenAI key (conversationality + value-correctness) |
   | `state-s3-uri` | e.g. `s3://sarvam-samvaad/alerting/state.db` (dedupe state for the scan) |
   | `scope-s3-uri` | e.g. `s3://sarvam-samvaad/alerting/scope.json` (Slack-controlled monitoring scope) |
   | `slack-app-token` (optional) | `xapp-…` for the Socket Mode scope-control service |
   | `google-creds-json` (optional) | path/JSON for the Master-QC-sheet (gsheet) notifier |

> **Scope control service** (`sarvam-alerting control-server`) is a **separate always-on
> Deployment**, not a DAG (Socket Mode needs a long-lived pod). It writes the scope JSON that
> the DAGs read. See `docs/scope-control.md`. Each discovering DAG pod needs
> `SARVAM_ALERTING_SCOPE_URI` (from `scope-s3-uri`) — the scan DAG already injects it.
4. **Slack**: create `#automation-alerts` (reports) and `#samvaad-alerts` (alerts),
   invite the bot. Adjust channel names in `images/samvaad/config.toml` if needed.

## Deploy

```bash
# in a checkout of sarvamai/airflow-dags
cp -r <this-repo>/airflow/samvaad/*        samvaad/
cp -r <this-repo>/airflow/images/samvaad/* images/samvaad/
git add samvaad images/samvaad && git commit -m "samvaad alerting DAGs + image" && git push origin main
```

- Push to `main` → GitHub Actions builds the image to ECR (`platform/airflow-images:samvaad-latest`), and git-sync registers the DAGs within ~1-2 min.
- In the Airflow UI, unpause the four `samvaad_*` DAGs.

## Schedules

| DAG | Schedule (UTC) | Purpose |
|---|---|---|
| `samvaad_alerting_scan` | `*/30 * * * *` | detectors (default-variable, connectivity, errors, short-calls, conversationality, expected-values) + run summary |
| `samvaad_cycle_report` | `0 14 * * *` | daily performance funnels |
| `samvaad_conversationality_review` | `0 15 * * *` | daily LLM transcript scoring |
| `samvaad_weekly_evals` | `0 5 * * 1` | weekly insights digest |

## Notes

- **State/dedupe:** worker pods are ephemeral, so the 30-min scan syncs its SQLite dedupe
  DB to/from S3 (`state-s3-uri`). Without it, the scan would re-alert every run.
- **Config vs secrets:** all tunables live in the baked `config.toml`; only secrets come
  from the K8s secret. Change thresholds by editing `config.toml` and rebuilding the image.
- **Resource limits:** these tasks are light (HTTP + small aggregations + a few LLM calls),
  well within the platform default (100m CPU / 128Mi request). No override needed.
- **Failure alerts:** ask the platform team to add the `samvaad_*` DAGs to the alert policy
  if you want Slack alerts when a DAG run itself fails.
