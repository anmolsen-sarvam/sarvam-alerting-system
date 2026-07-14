"""Slack control app — the full two-way console, no file edits, no deploy.

Runs as an always-on Socket Mode service. It reads/writes the shared runtime store
(``SARVAM_ALERTING_SCOPE_URI``) and can query live data on demand. The scheduled scans read
the same store, so a Slack change (scope / owners / expected / mutes) applies on the next run.

Commands (tag the bot):

  Scope        monitor · unmonitor · include · exclude · scope · reset
  Owners       owner · unowner · owner-min · owners
  Expected     expect · unexpect · expected
  Mutes        mute <key> [4h|30m|2d] · unmute <key> · mutes
  Queries      status · campaigns · check <campaign> · report <campaign>
  help

Alert messages (posted by the bot notifier) also carry buttons — Ack / Snooze 4h / Mute /
Open — handled here. And the bot's *Home* tab shows the current scope, owners, rules & mutes.

Needs env: SLACK_BOT_TOKEN (xoxb-…), SLACK_APP_TOKEN (xapp-…), SARVAM_ALERTING_SCOPE_URI.
"""

from __future__ import annotations

import logging
import os
import re
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..clients.metabase import MetabaseClient
from ..config import load_config
from ..engine import discover_campaigns, run_scan
from ..owners import format_mention, parse_ids
from ..reports import build_cycle_report
from ..scope import FIELDS, RuntimeStore

log = logging.getLogger("sarvam_alerting.control")

_SEVERITIES = ("info", "warning", "critical")

HELP = (
    "Hi! I watch your campaigns and let you control the alerting right here — "
    "just mention me followed by a command. Changes take effect on the next scan.\n\n"

    ":mag: *See what's going on*\n"
    "• `status` — quick overview: how many campaigns are live + your current settings\n"
    "• `campaigns` — list the campaigns I'm watching\n"
    "• `check chola.com` — check one campaign *right now* and tell you what I find\n"
    "• `report <campaign id>` — pull a performance report for a campaign\n\n"

    ":eyes: *Choose what I watch*\n"
    "• `monitor chola.com` — watch only this client  ·  `reset` — go back to watching everything\n"
    "• `include PAPQ` / `exclude test` — only / never campaigns whose name contains this\n"
    "• `scope` — show what I'm watching right now\n\n"

    ":bust_in_silhouette: *Choose who I ping when something breaks*\n"
    "• `owner chola.com @you` — tag people on this client's alerts\n"
    "• `owners` — show the list  ·  `unowner chola.com` — stop tagging them\n"
    "• `owner-min warning` — ping owners for warnings too (default: only critical)\n\n"

    ":clipboard: *Tell me the right answers* (so I can catch wrong ones)\n"
    "• `expect D2C loan_type = Digital Personal Loan | Samsung Mobile Loan` — allowed values\n"
    "• `expect JUNE cohort 50000` — expected cohort size  ·  `expected` — show these rules\n\n"

    ":no_bell: *Quiet me down*\n"
    "• `mute chola.com 4h` — stop alerts on this for 4h (or `30m` / `2d`, or leave blank = until you unmute)\n"
    "• `unmute chola.com`  ·  `mutes` — show what's muted\n\n"

    "_Tip: names are matched loosely — `chola.com`, `PAPQ`, `D2C` all work as partial matches._"
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _parse_duration(text: str) -> int | None:
    """'4h' / '30m' / '2d' -> seconds. None if it doesn't parse."""
    m = re.fullmatch(r"(\d+)\s*([mhd])", (text or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _fmt_until(until: float | None, now: float) -> str:
    if until is None:
        return "until unmuted"
    remaining = int(until - now)
    if remaining <= 0:
        return "expired"
    if remaining >= 86400:
        return f"{remaining // 86400}d left"
    if remaining >= 3600:
        return f"{remaining // 3600}h left"
    return f"{max(1, remaining // 60)}m left"


def _fmt_scope(scope: dict) -> str:
    if not any(scope.get(f) for f in FIELDS):
        return "Scope: *everything* (all orgs, all campaigns)."
    labels = {
        "only_orgs": "Only orgs", "exclude_orgs": "Exclude orgs",
        "include_patterns": "Only campaigns matching", "exclude_patterns": "Exclude campaigns matching",
    }
    lines = ["*Scope:*"]
    for f in FIELDS:
        vals = scope.get(f) or []
        if vals:
            lines.append(f"• {labels[f]}: {', '.join('`' + v + '`' for v in vals)}")
    return "\n".join(lines)


def _fmt_ids(ids: list[str]) -> str:
    return " ".join(format_mention(i) for i in ids)


def _fmt_owners(owners: dict) -> str:
    if not (owners.get("org") or owners.get("campaign") or owners.get("default")):
        return "Owners: *none set*."
    lines = [f"*Owners* (paged at ≥ `{owners.get('min_severity', 'critical')}`):"]
    for key, ids in (owners.get("org") or {}).items():
        lines.append(f"• org `{key}` → {_fmt_ids(ids)}")
    for key, ids in (owners.get("campaign") or {}).items():
        lines.append(f"• campaign `{key}` → {_fmt_ids(ids)}")
    if owners.get("default"):
        lines.append(f"• default → {_fmt_ids(owners['default'])}")
    return "\n".join(lines)


def _fmt_expected(rules: list) -> str:
    if not rules:
        return "Expected-value rules: *none set*."
    lines = ["*Expected-value rules:*"]
    for r in rules:
        key = r.get("match_campaign_contains", "?")
        if "variable" in r:
            lines.append(f"• `{key}` · `{r['variable']}` ∈ {r.get('allowed', [])}")
        elif "cohort_size" in r:
            lines.append(f"• `{key}` · cohort size ≈ {r['cohort_size']:,}")
    return "\n".join(lines)


def _fmt_mutes(mutes: dict, now: float) -> str:
    active = {k: v for k, v in mutes.items() if v is None or float(v) > now}
    if not active:
        return "Mutes: *none*."
    lines = ["*Mutes:*"]
    for key, until in active.items():
        lines.append(f"• `{key}` — {_fmt_until(until, now)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command handlers (mutating the store)
# --------------------------------------------------------------------------- #
def _handle_scope(store: RuntimeStore, cmd: str, args: list[str], say) -> None:
    scope = store.load_scope()

    def add(field: str, val: str) -> None:
        scope[field] = sorted(set(scope.get(field, [])) | {val})

    if cmd in ("scope", "status_scope"):
        say(_fmt_scope(scope))
        return
    if cmd == "reset":
        store.save_scope({})
        say("Scope cleared — watching *everything* now.")
        return
    if not args:
        say(f"Usage: `{cmd} <value>`")
        return
    val = args[0]
    if cmd == "monitor":
        add("only_orgs", val)
    elif cmd == "unmonitor":
        if val in scope.get("only_orgs", []):
            scope["only_orgs"] = [x for x in scope["only_orgs"] if x != val]
        else:
            add("exclude_orgs", val)
    elif cmd == "include":
        add("include_patterns", val)
    elif cmd == "exclude":
        add("exclude_patterns", val)
    store.save_scope(scope)
    say("Updated.\n" + _fmt_scope(scope))


def _handle_owner(store: RuntimeStore, cmd: str, args: list[str], say) -> None:
    owners = store.load_owners()
    if cmd == "owners":
        say(_fmt_owners(owners))
        return
    if cmd == "owner-min":
        if not args or args[0].lower() not in _SEVERITIES:
            say(f"Usage: `owner-min <{'|'.join(_SEVERITIES)}>`")
            return
        owners["min_severity"] = args[0].lower()
        store.save_owners(owners)
        say(f"Owners now paged at ≥ `{owners['min_severity']}`.")
        return
    if cmd == "unowner":
        if not args:
            say("Usage: `unowner <key>`")
            return
        key, removed = args[0], False
        for section in ("org", "campaign"):
            if key in (owners.get(section) or {}):
                owners[section].pop(key)
                removed = True
        store.save_owners(owners)
        say((f"Removed owners for `{key}`." if removed else f"No owner rule matched `{key}`."))
        return
    if not args:
        say("Usage: `owner <key> @person …`")
        return
    section = None
    if args[0].lower() in ("org", "campaign"):
        section, key, id_tokens = args[0].lower(), (args[1] if len(args) > 1 else ""), args[2:]
    else:
        key, id_tokens = args[0], args[1:]
    if not key:
        say("Give a key, e.g. `owner chola.com @you`.")
        return
    ids = parse_ids(id_tokens)
    if not ids:
        say("Tag at least one person (`@you`) or paste a Slack id (`U…`/`S…`).")
        return
    section = section or ("org" if "." in key else "campaign")
    owners.setdefault(section, {})
    owners[section][key] = sorted(set(owners[section].get(key, [])) | set(ids))
    owners.setdefault("min_severity", "critical")
    store.save_owners(owners)
    say("Updated.\n" + _fmt_owners(owners))


def _handle_expect(store: RuntimeStore, cmd: str, args: list[str], raw: str, say) -> None:
    rules = store.load_expected()
    if cmd == "expected":
        say(_fmt_expected(rules))
        return
    if cmd == "unexpect":
        if not args:
            say("Usage: `unexpect <campaign>`")
            return
        key = args[0]
        kept = [r for r in rules if r.get("match_campaign_contains") != key]
        store.save_expected(kept)
        say((f"Removed rules for `{key}`." if len(kept) != len(rules) else f"No rule matched `{key}`."))
        return
    if len(args) < 2:
        say("Usage: `expect <campaign> <var> = <v1> | <v2>`  or  `expect <campaign> cohort <n>`")
        return
    key = args[0]
    if args[1].lower() == "cohort":
        try:
            size = int(args[2].replace(",", ""))
        except (IndexError, ValueError):
            say("Usage: `expect <campaign> cohort <n>`")
            return
        rule = {"name": f"slack:{key}:cohort", "match_campaign_contains": key, "cohort_size": size}
    else:
        if "=" not in raw:
            say("Need `=`, e.g. `expect D2C loan_type = Digital Personal Loan | Samsung Mobile Loan`")
            return
        lhs, rhs = raw.split("=", 1)
        variable = lhs.split()[-1]
        allowed = [v.strip() for v in rhs.split("|") if v.strip()]
        if not allowed:
            say("List at least one allowed value after `=` (pipe-separated).")
            return
        rule = {"name": f"slack:{key}:{variable}", "match_campaign_contains": key,
                "variable": variable, "allowed": allowed}
    rules = [r for r in rules if r.get("name") != rule["name"]] + [rule]
    store.save_expected(rules)
    say("Updated.\n" + _fmt_expected(rules))


def _handle_mute(store: RuntimeStore, cmd: str, args: list[str], say) -> None:
    mutes = store.load_mutes()
    now = time.time()
    if cmd == "mutes":
        say(_fmt_mutes(mutes, now))
        return
    if not args:
        say(f"Usage: `{cmd} <key> [4h|30m|2d]`")
        return
    key = args[0]
    if cmd == "unmute":
        mutes.pop(key, None)
        store.save_mutes(mutes)
        say(f"Unmuted `{key}`.\n" + _fmt_mutes(mutes, now))
        return
    # mute
    until: float | None = None
    if len(args) > 1:
        secs = _parse_duration(args[1])
        if secs is None:
            say("Duration must look like `4h`, `30m`, or `2d`.")
            return
        until = now + secs
    mutes[key] = until
    store.save_mutes(mutes)
    say(f"Muted `{key}` ({_fmt_until(until, now)}).\n" + _fmt_mutes(mutes, now))


# --------------------------------------------------------------------------- #
# Query handlers (read live data)
# --------------------------------------------------------------------------- #
def _handle_query(store: RuntimeStore, cmd: str, args: list[str], say) -> None:
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        say(f":warning: couldn't load config: {exc}")
        return
    now = time.time()

    if cmd == "status":
        try:
            with MetabaseClient(cfg.metabase) as mb:
                campaigns = discover_campaigns(mb, cfg)
        except Exception as exc:  # noqa: BLE001
            say(f":warning: discovery failed: {exc}")
            return
        say(
            f"*Status*\n• Watching *{len(campaigns)}* active campaign(s).\n"
            + _fmt_scope(store.load_scope()) + "\n"
            + _fmt_mutes(store.load_mutes(), now) + "\n"
            + _fmt_owners(store.load_owners())
        )
        return

    if cmd == "campaigns":
        try:
            with MetabaseClient(cfg.metabase) as mb:
                campaigns = discover_campaigns(mb, cfg)
        except Exception as exc:  # noqa: BLE001
            say(f":warning: discovery failed: {exc}")
            return
        if not campaigns:
            say("No active campaigns match the current scope.")
            return
        lines = [f"*{len(campaigns)} active campaign(s):*"]
        for c in campaigns[:30]:
            lines.append(f"• `{c.campaign_id}` · {c.org_id or '-'} · {c.calls:,} calls")
        if len(campaigns) > 30:
            lines.append(f"…and {len(campaigns) - 30} more.")
        say("\n".join(lines))
        return

    if cmd == "check":
        if not args:
            say("Usage: `check <campaign_id>`")
            return
        cid = args[0]
        say(f"Checking `{cid}`…")
        try:
            with MetabaseClient(cfg.metabase) as mb:
                findings, _ = run_scan(mb, cfg, only_campaign=cid, apply_mutes=False)
        except Exception as exc:  # noqa: BLE001
            say(f":warning: check failed: {exc}")
            return
        if not findings:
            say(f":white_check_mark: `{cid}` — no issues detected right now.")
            return
        lines = [f"*{len(findings)} finding(s) on* `{cid}`:"]
        for f in findings:
            lines.append(f"{f.severity.emoji} *{f.title}* — {f.detail[:280]}")
        say("\n".join(lines))
        return

    if cmd == "report":
        if not args:
            say("Usage: `report <campaign_id>`")
            return
        cid = args[0]
        say(f"Building cycle report for `{cid}`…")
        try:
            with MetabaseClient(cfg.metabase) as mb:
                report = build_cycle_report(mb, cfg, [cid])
        except Exception as exc:  # noqa: BLE001
            say(f":warning: report failed: {exc}")
            return
        if report is None:
            say(f"No data to report for `{cid}`.")
            return
        say(f"*{report.title}*\n" + "\n\n".join(report.sections))
        return


def _handle_feedback(store: RuntimeStore, say) -> None:
    events = store.load_feedback()
    if not events:
        say("No feedback yet. (Ack/Snooze/Mute on alerts is recorded here to spot noisy alerts.)")
        return
    counts: dict[str, int] = {}
    for e in events:
        counts[e.get("campaign_id", "?")] = counts.get(e.get("campaign_id", "?"), 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:10]
    lines = [f"*Alert feedback* — {len(events)} ack/snooze/mute action(s) logged.",
             "_Most-silenced campaigns (candidates for threshold tuning):_"]
    for cid, c in top:
        lines.append(f"• `{cid}` — {c}×")
    say("\n".join(lines))


_SCOPE_CMDS = {"monitor", "unmonitor", "include", "exclude", "scope", "reset"}
_OWNER_CMDS = {"owner", "owners", "unowner", "owner-min"}
_EXPECT_CMDS = {"expect", "unexpect", "expected"}
_MUTE_CMDS = {"mute", "unmute", "mutes"}
_QUERY_CMDS = {"status", "campaigns", "check", "report"}


def handle(tokens: list[str], raw: str, store: RuntimeStore, say) -> None:
    if not tokens or tokens[0].lower() in ("help", "?"):
        say(HELP)
        return
    cmd, args = tokens[0].lower(), tokens[1:]
    if cmd in _SCOPE_CMDS:
        _handle_scope(store, cmd, args, say)
    elif cmd in _OWNER_CMDS:
        _handle_owner(store, cmd, args, say)
    elif cmd in _EXPECT_CMDS:
        _handle_expect(store, cmd, args, raw, say)
    elif cmd in _MUTE_CMDS:
        _handle_mute(store, cmd, args, say)
    elif cmd == "feedback":
        _handle_feedback(store, say)
    elif cmd in _QUERY_CMDS:
        _handle_query(store, cmd, args, say)
    else:
        say(f"Unknown command `{cmd}`.\n" + HELP)


# --------------------------------------------------------------------------- #
# App Home dashboard
# --------------------------------------------------------------------------- #
def _home_view(store: RuntimeStore) -> dict:
    now = time.time()
    sections = [
        _fmt_scope(store.load_scope()),
        _fmt_owners(store.load_owners()),
        _fmt_expected(store.load_expected()),
        _fmt_mutes(store.load_mutes(), now),
    ]
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Sarvam Alerting — control"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Mention me with `help` for commands. Changes apply next run."}]},
        {"type": "divider"},
    ]
    for text in sections:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})
    return {"type": "home", "blocks": blocks}


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def build_app(store: RuntimeStore) -> App:
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    def on_mention(event, say, context):  # noqa: ANN001
        bot_id = context.get("bot_user_id")
        tokens = [
            t for t in event.get("text", "").split()
            if not (bot_id and re.match(rf"^<@{bot_id}(?:\|[^>]*)?>$", t))
        ]
        raw = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        try:
            handle(tokens, raw, store, say)
        except Exception:
            log.exception("control command failed")
            say(":warning: command failed — check the service logs.")

    @app.event("app_home_opened")
    def on_home(client, event):  # noqa: ANN001
        try:
            client.views_publish(user_id=event["user"], view=_home_view(store))
        except Exception:
            log.exception("home publish failed")

    def _mute_from_button(campaign_id: str, until: float | None, action: str, user: str) -> None:
        mutes = store.load_mutes()
        mutes[campaign_id] = until
        store.save_mutes(mutes)
        # Feedback signal: a quick ack/mute after an alert hints the alert may be low-value.
        store.append_feedback({"ts": time.time(), "campaign_id": campaign_id,
                               "action": action, "user": user})

    @app.action("alert_ack")
    def on_ack(ack, body, say):  # noqa: ANN001
        ack()
        cid = body["actions"][0]["value"]
        user = body.get("user", {}).get("id")
        try:
            hours = load_config().state.cooldown_hours
        except Exception:  # noqa: BLE001
            hours = 6
        _mute_from_button(cid, time.time() + hours * 3600, "ack", user)
        say(f":ok_hand: `{cid}` acknowledged by <@{user}> — snoozed {hours}h.")

    @app.action("alert_snooze")
    def on_snooze(ack, body, say):  # noqa: ANN001
        ack()
        cid = body["actions"][0]["value"]
        user = body.get("user", {}).get("id")
        _mute_from_button(cid, time.time() + 4 * 3600, "snooze", user)
        say(f":zzz: `{cid}` snoozed 4h.")

    @app.action("alert_mute")
    def on_mute(ack, body, say):  # noqa: ANN001
        ack()
        cid = body["actions"][0]["value"]
        user = body.get("user", {}).get("id")
        _mute_from_button(cid, None, "mute", user)
        say(f":mute: `{cid}` muted by <@{user}> (until unmuted).")

    @app.action("open_metabase")
    def on_open(ack):  # noqa: ANN001 - URL button; just ack so Slack is happy
        ack()

    return app


def run() -> None:
    uri = os.environ.get("SARVAM_ALERTING_SCOPE_URI", "").strip() or "store.json"
    store = RuntimeStore(uri)
    log.info("control plane using store: %s", uri)
    app = build_app(store)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
