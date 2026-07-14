# Controlling everything from Slack

CS/QC run the system from Slack — **no code, no file edits, no deploy**. You control three
things by messaging a bot: **what's watched** (scope), **who gets paged** (owners), and the
**sanity rules** (expected values). The only things that stay in files are real secrets
(API keys, Slack tokens) — those live in the environment / K8s secret for security.

## How it works

```
@alerts monitor chola.com        (in Slack)
        │
        ▼
control service (Socket Mode)  ──writes──►  runtime store (one JSON, S3 in prod)
                                                   │
                                                   ▼  read at the start of every run
                              the 30-min scan + reports respect the new settings
```

- The **runtime store** is a single JSON document (sections: `scope`, `owners`, `expected`)
  at `SARVAM_ALERTING_SCOPE_URI` — a local file in dev, an **S3 object** in prod (so every
  ephemeral DAG pod reads the same thing).
- The **control service** is an always-on Slack **Socket Mode** app (no public URL). It's a
  separate long-running deployment — *not* one of the DAGs (those are ephemeral).
- Every scan/report calls `load_config()`, which overlays the store automatically, so a
  Slack change takes effect on the next run.

## Commands (tag the bot)

**Scope — what's watched**

| Command | Effect |
|---|---|
| `monitor <org>` | watch **only** these orgs (e.g. `monitor chola.com`) |
| `unmonitor <org>` | stop watching an org |
| `include <pattern>` | only campaigns whose id contains this (e.g. `include PAPQ`) |
| `exclude <pattern>` | drop campaigns whose id contains this (e.g. `exclude test`) |
| `scope` | show current scope |
| `reset` | clear scope → watch everything |

**Owners — who gets @-mentioned when a campaign breaks**

| Command | Effect |
|---|---|
| `owner <key> @a @b` | tag people for an org/campaign; a dotted key (`chola.com`) ⇒ org |
| `owner org <substr> @a` / `owner campaign <substr> @a` | force the kind |
| `unowner <key>` | remove owners for a key |
| `owner-min <info\|warning\|critical>` | only tag at/above this severity (default `critical`) |
| `owners` | show the owner map |

**Expected values — the CS sanity rules**

| Command | Effect |
|---|---|
| `expect <campaign> <var> = <v1> \| <v2>` | allowed values for a variable (pipe-separated, spaces OK) |
| `expect <campaign> cohort <n>` | expected cohort size |
| `unexpect <campaign>` | remove rules for that campaign key |
| `expected` | show the rules |

Example: `@alerts expect D2C loan_type = Digital Personal Loan | Samsung Mobile Loan`

**Mutes — silence alerts without losing coverage**

| Command | Effect |
|---|---|
| `mute <key> [4h\|30m\|2d]` | stop alerting on matching campaigns (no duration = until unmuted) |
| `unmute <key>` | lift a mute |
| `mutes` | show active mutes + time remaining |

**Queries — pull live state on demand**

| Command | Effect |
|---|---|
| `status` | active-campaign count + current scope / mutes / owners |
| `campaigns` | list the campaigns currently in scope |
| `check <campaign>` | run the detectors on one campaign right now and reply |
| `report <campaign>` | build a cycle report for one campaign |
| `feedback` | most-silenced campaigns (from Ack/Snooze/Mute) — candidates for threshold tuning |

Matching is by **substring** (`chola.com`, `PAPQ`, `D2C`).

## Alert buttons + recovery

Alerts posted via the **bot token** carry buttons — **Ack** (snooze for the cooldown),
**Snooze 4h**, and **Mute**. Clicking them updates the same store, so triage happens inline.
For the buttons to work, the alert notifier and the control service must be the **same Slack
app** (use one `SLACK_BOT_TOKEN`). When a finding clears, the next scan posts a `✅ recovered` note.
Ack/Snooze/Mute are also logged as **feedback** (see the `feedback` command) — a signal for
which alerts are noisy and worth tuning down.

### Reliability knobs (`[tuning]` in config)

- **Shadow mode** (`shadow_mode = true`) — detectors run but alerts go to the console/logs
  only, not Slack. Use during onboarding to watch alert volume and tune thresholds before
  anyone gets paged. Digests still post.
- **Escalation** (`escalate_after_minutes`) — if a CRITICAL stays open and unacknowledged
  (no Ack/Mute) for this long, owners get re-pinged with a "still unresolved" escalation.
- **Dead-man's switch** (`heartbeat_max_silence_hours`) — the scan writes a heartbeat each
  run; `sarvam-alerting heartbeat-check` (a separate schedule / the `samvaad_alerting_heartbeat`
  DAG) alerts if no successful scan happened within the window. This is what tells you the
  watchdog itself died.

### Optional: an "Open in Metabase" button

The button appears only when `[links].campaign_dashboard_url_template` is set. There's no
campaign-id dashboard in Metabase today and the alerting API key can't create one (403), so
it's **off** by default. To enable a per-campaign call drill-down, create a Metabase question
manually (any user with question-create rights), then paste its URL into the template.

Native question (database: the ClickHouse read-replica), with a **text** variable `campaign_id`:

```sql
SELECT created_at_timestamp, interaction_id, v2v_connectivity_status,
       completion_status, num_messages, final_duration,
       on_end_agent_variables['disposition'] AS disposition
FROM `sarvam-app-analytics-db`.EngagementFacts
WHERE campaign_id = {{campaign_id}} AND is_deleted = 0 AND is_debug_call = 0
ORDER BY created_at_timestamp DESC LIMIT 500
```

Save it, then set in `config.toml`:

```toml
[links]
campaign_dashboard_url_template = "https://metabase.sarvam.ai/question/<ID>?campaign_id={campaign_id}"
```

The button returns automatically on the next alert. (`{org_id}` is also available if you
prefer an org-level dashboard link.)

## App Home

The bot's **Home** tab shows a live dashboard: current scope, owners, expected rules, and
mutes. Enable *App Home* + the *Home Tab* in the app settings for it to appear.

## What still lives in files / env (and why)

- **Secrets** (Metabase API key, Slack tokens, Azure key, S3 URIs) — environment / K8s secret.
  Never settable from chat.
- **Detector thresholds** and **channel routing** — baked config (`config.toml`). These are
  tuning/plumbing changed rarely by whoever owns the deploy, not day-to-day by CS.

Everything CS touches day-to-day (scope, owners, expected values) is Slack-driven.

## One-time setup

1. **Create a Slack app** at api.slack.com/apps → *From scratch*.
   - **Socket Mode**: enable it → generate an **app-level token** (`xapp-…`) with `connections:write`.
     (Socket Mode also carries button clicks — no Interactivity request URL needed.)
   - **OAuth scopes** (bot): `app_mentions:read`, `chat:write`. Install → copy the **bot token** (`xoxb-…`).
   - **Event Subscriptions**: subscribe to the `app_mention` and `app_home_opened` bot events.
   - **App Home**: enable the *Home Tab* (for the dashboard).
   - Invite the bot to the control channel (channel ▸ Integrations ▸ Add an app is more
     reliable than `/invite`).
2. **Install the control extra:** `uv sync --extra control` (adds `slack-bolt`).
3. **Set env:**
   ```
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_APP_TOKEN=xapp-…
   SARVAM_ALERTING_SCOPE_URI=store.json            # a local path in dev
   ```
4. **Run it:**
   ```bash
   uv run sarvam-alerting control-server
   ```

## Prod (Kubernetes)

- The control service is a **separate always-on deployment** (a Deployment, not an Airflow
  DAG) — Socket Mode keeps a websocket open, so it needs a long-lived pod.
- Add to the `samvaad-alerting-secrets` K8s secret: `slack-app-token` and `scope-s3-uri`
  (e.g. `s3://sarvam-samvaad/alerting/store.json`).
- Every **DAG** pod that discovers campaigns must also get `SARVAM_ALERTING_SCOPE_URI`
  (from `scope-s3-uri`) so runs honor the Slack-set settings — the scan DAG already injects
  it; the cycle / conversationality / value / client DAG pods do too.
- The control pod and the DAG pods share the one S3 object; no other coupling.

## Safety notes

- Changes are **not destructive** — worst case you watch too much/little or page the wrong
  person; no data is touched. Limit the control channel's membership to CS/QC leads.
- If the store is unreachable, runs fall back to the baked config (fail-open to the
  configured default, not to "watch nothing").
- The store is human-readable JSON; you can eyeball or hand-edit it in a pinch.
