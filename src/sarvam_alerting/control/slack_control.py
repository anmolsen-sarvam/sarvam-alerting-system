"""Slack control app — let CS/QC change the monitoring scope from Slack, no deploy.

Runs as an always-on Socket Mode service (no public URL / TLS needed). It reads and writes
the shared scope store (``SARVAM_ALERTING_SCOPE_URI``); the scheduled DAG scans read that
same store, so a Slack command takes effect on the next scan.

Commands (tag the bot):
    @alerts scope                     show current scope
    @alerts monitor <org>             monitor ONLY these orgs (e.g. `monitor chola.com`)
    @alerts unmonitor <org>           stop monitoring an org
    @alerts include <pattern>         only campaigns whose id contains <pattern>
    @alerts exclude <pattern>         drop campaigns whose id contains <pattern>
    @alerts reset                     clear scope → monitor everything
    @alerts help

Needs env: SLACK_BOT_TOKEN (xoxb-…), SLACK_APP_TOKEN (xapp-…), SARVAM_ALERTING_SCOPE_URI.
"""

from __future__ import annotations

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..scope import SCOPE_ENV, FIELDS, ScopeStore

log = logging.getLogger("sarvam_alerting.control")

HELP = (
    "*Scope control* — I decide which orgs/campaigns get monitored.\n"
    "• `scope` — show current scope\n"
    "• `monitor <org>` — monitor ONLY these orgs (substring, e.g. `monitor chola.com`)\n"
    "• `unmonitor <org>` — stop monitoring an org\n"
    "• `include <pattern>` — only campaigns whose id contains this (e.g. `include PAPQ`)\n"
    "• `exclude <pattern>` — drop campaigns whose id contains this (e.g. `exclude test`)\n"
    "• `reset` — clear everything → monitor all orgs/campaigns"
)


def _fmt_scope(scope: dict) -> str:
    if not any(scope.get(f) for f in FIELDS):
        return "Current scope: *everything* (all orgs, all campaigns)."
    lines = ["*Current scope:*"]
    labels = {
        "only_orgs": "Only orgs",
        "exclude_orgs": "Exclude orgs",
        "include_patterns": "Only campaigns matching",
        "exclude_patterns": "Exclude campaigns matching",
    }
    for f in FIELDS:
        vals = scope.get(f) or []
        if vals:
            lines.append(f"• {labels[f]}: {', '.join('`' + v + '`' for v in vals)}")
    return "\n".join(lines)


def handle(tokens: list[str], store: ScopeStore, say) -> None:
    scope = store.load()
    if not tokens or tokens[0].lower() in ("help", "?"):
        say(HELP)
        return
    cmd, args = tokens[0].lower(), tokens[1:]

    if cmd in ("scope", "show", "status"):
        say(_fmt_scope(scope))
        return
    if cmd == "reset":
        store.save({})
        say("Scope cleared — monitoring *everything* now.")
        return
    if not args:
        say(f"Usage: `{cmd} <value>`\n" + HELP)
        return

    val = args[0]

    def add(field: str) -> None:
        scope[field] = sorted(set(scope.get(field, [])) | {val})

    def remove(field: str) -> None:
        scope[field] = [x for x in scope.get(field, []) if x != val]

    if cmd == "monitor":
        add("only_orgs")
    elif cmd == "unmonitor":
        if val in scope.get("only_orgs", []):
            remove("only_orgs")
        else:
            add("exclude_orgs")
    elif cmd == "include":
        add("include_patterns")
    elif cmd == "exclude":
        add("exclude_patterns")
    else:
        say(f"Unknown command `{cmd}`.\n" + HELP)
        return

    store.save(scope)
    say("Updated.\n" + _fmt_scope(scope))


def build_app(store: ScopeStore) -> App:
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    def on_mention(event, say):  # noqa: ANN001
        tokens = [t for t in event.get("text", "").split() if not t.startswith("<@")]
        try:
            handle(tokens, store, say)
        except Exception:
            log.exception("control command failed")
            say(":warning: command failed — check the service logs.")

    return app


def run() -> None:
    uri = os.environ.get(SCOPE_ENV, "").strip() or "scope.json"
    store = ScopeStore(uri)
    log.info("scope control using store: %s", uri)
    app = build_app(store)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
