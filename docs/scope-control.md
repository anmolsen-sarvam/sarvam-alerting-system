# Controlling the scope from Slack

Non-engineers (CS/QC) decide **which orgs and campaigns are monitored** by messaging a
Slack bot — no code, no deploy. This doc explains the model and setup.

## How it works

```
@alerts monitor chola.com        (in Slack)
        │
        ▼
control service (Socket Mode)  ──writes──►  scope store (S3 JSON)
                                                   │
                                                   ▼  read at the start of every run
                              the 30-min scan + reports respect the new scope
```

- The **scope store** is a small JSON (`only_orgs`, `exclude_orgs`, `include_patterns`,
  `exclude_patterns`) at `SARVAM_ALERTING_SCOPE_URI` — a local file in dev, an **S3 object**
  in prod (so every ephemeral DAG pod reads the same thing).
- The **control service** is an always-on Slack **Socket Mode** app (no public URL). It's a
  separate long-running deployment — *not* one of the DAGs (those are ephemeral).
- Every scan/report calls `load_config()`, which overlays the scope store automatically, so
  a Slack change takes effect on the next run.

## Commands (tag the bot)

| Command | Effect |
|---|---|
| `@alerts scope` | show the current scope |
| `@alerts monitor <org>` | monitor **only** these orgs (e.g. `monitor chola.com`) |
| `@alerts unmonitor <org>` | stop monitoring an org |
| `@alerts include <pattern>` | only campaigns whose id contains this (e.g. `include PAPQ`) |
| `@alerts exclude <pattern>` | drop campaigns whose id contains this (e.g. `exclude test`) |
| `@alerts reset` | clear scope → monitor everything |
| `@alerts help` | usage |

Matching is by **substring** (`chola.com`, `tatacapital`, `PAPQ`).

## One-time setup

1. **Create a Slack app** at api.slack.com/apps → *From scratch*.
   - **Socket Mode**: enable it → generate an **app-level token** (`xapp-…`) with `connections:write`.
   - **OAuth scopes** (bot): `app_mentions:read`, `chat:write`. Install → copy the **bot token** (`xoxb-…`).
   - **Event Subscriptions**: subscribe to the `app_mention` bot event.
   - Invite the bot to the control channel.
2. **Install the control extra:** `uv pip install 'slack-bolt>=1.18'` (or `uv sync --extra control`).
3. **Set env:**
   ```
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_APP_TOKEN=xapp-…
   SARVAM_ALERTING_SCOPE_URI=s3://your-bucket/alerting/scope.json   # local path in dev
   ```
4. **Run it:**
   ```bash
   uv run sarvam-alerting control-server
   ```

## Prod (Kubernetes)

- The control service is a **separate always-on deployment** (a Deployment, not an Airflow
  DAG) — Socket Mode keeps a websocket open, so it needs a long-lived pod.
- Add to the `samvaad-alerting-secrets` K8s secret: `slack-app-token` and `scope-s3-uri`.
- Every **DAG** pod that discovers campaigns must also get `SARVAM_ALERTING_SCOPE_URI`
  (from `scope-s3-uri`) so scans honor the Slack-set scope — the scan DAG already injects it;
  add the same `_secret("SARVAM_ALERTING_SCOPE_URI", "scope-s3-uri")` line to the cycle /
  conversationality / value / client DAG pods.
- The control pod and the DAG pods share the one S3 scope object; no other coupling.

## Safety notes

- Scope changes are **not destructive** — worst case you monitor too much or too little; no
  data is touched. Consider limiting the control channel's membership to CS/QC leads.
- If the scope store is unreachable, scans fall back to the baked config's scope (fail-open
  to the configured default, not to "monitor nothing").
