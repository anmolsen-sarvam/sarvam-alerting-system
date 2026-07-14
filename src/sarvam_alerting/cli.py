"""Command-line interface.

Commands:
  check          One-off scan, printed to the console (ignores cooldown state).
  watch          Scheduled scan -> configured notifiers, with dedupe/cooldown.
  list-campaigns Show the campaigns that would be monitored right now.
  owners         View/add/remove engagement owners (who gets @-mentioned on alerts).
  test-notify    Send a synthetic finding through the configured notifiers.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time

import typer
from rich.console import Console
from rich.table import Table

from .clients.llm import LLMClient, LLMError
from .clients.metabase import MetabaseClient
from .config import Config, ConfigError, load_config
from .engine import build_reports, discover_campaigns, run_scan
from .models import Finding, Report, Severity
from .notify import Notifier, build_notifiers
from .notify.console import ConsoleNotifier
from .owners import OwnerResolver, parse_ids
from .scope import RuntimeStore, is_muted
from .reports import (
    build_client_reports,
    build_conversationality_review,
    build_cycle_report,
    build_run_summary,
    build_value_correctness_review,
    build_weekly_evals,
)
from .state import StateStore

app = typer.Typer(add_completion=False, help="Sarvam campaign-deployment alerting.")
console = Console()
log = logging.getLogger("sarvam_alerting")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load(config_path: str | None, current_hours: int | None, baseline_hours: int | None) -> Config:
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2)
    if current_hours or baseline_hours:
        windows = dataclasses.replace(
            cfg.windows,
            current_hours=current_hours or cfg.windows.current_hours,
            baseline_hours=baseline_hours or cfg.windows.baseline_hours,
        )
        cfg = dataclasses.replace(cfg, windows=windows)
    return cfg


@app.command()
def check(
    campaign: str = typer.Option(None, "--campaign", "-c", help="Check a single campaign id (default: auto-discover)."),
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    current_hours: int = typer.Option(None, help="Override current-window hours."),
    baseline_hours: int = typer.Option(None, help="Override baseline-window hours."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a one-off scan and print findings to the console."""
    _setup_logging(verbose)
    cfg = _load(config, current_hours, baseline_hours)
    with MetabaseClient(cfg.metabase) as mb:
        findings, campaigns = run_scan(mb, cfg, only_campaign=campaign, apply_mutes=False)
    ConsoleNotifier(Severity.INFO, links=cfg.links, owners=OwnerResolver(cfg.owners)).notify(
        findings, {"campaigns_scanned": len(campaigns)}
    )
    if any(f.severity == Severity.CRITICAL for f in findings):
        raise typer.Exit(code=1)


@app.command()
def watch(
    once: bool = typer.Option(False, "--once", help="Run a single pass and exit (for cron/systemd timers)."),
    interval_minutes: int = typer.Option(30, "--interval", help="Loop interval when not --once."),
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scan on a schedule and deliver new findings to the configured notifiers."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    notifiers = build_notifiers(cfg.notifiers, cfg.links, cfg.owners)
    state = StateStore(cfg.state.path, cfg.state.cooldown_hours)

    shadow = bool(cfg.tuning.get("shadow_mode", False))
    escalate_after = int(cfg.tuning.get("escalate_after_minutes", 30)) * 60
    if shadow:
        log.warning("SHADOW MODE: alerts are logged to console only, not delivered to Slack.")

    def _alert_notifiers() -> list[Notifier]:
        # In shadow mode, only the console sees alerts (tune without spamming Slack).
        if shadow:
            return [n for n in notifiers if isinstance(n, ConsoleNotifier)]
        return notifiers

    def one_pass() -> None:
        with MetabaseClient(cfg.metabase) as mb:
            findings, campaigns = run_scan(mb, cfg)
            reports = build_reports(mb, cfg, campaigns)
        new = [f for f in findings if state.is_new(f)]
        log.info(
            "scan complete: %d finding(s), %d new, %d campaign(s), %d report(s)",
            len(findings), len(new), len(campaigns), len(reports),
        )
        # Recovery: anything that was open (within cooldown) but is absent now has cleared.
        current_fps = {f.fingerprint for f in findings}
        now = time.time()
        resolved = [
            a for a in state.active_findings(now)
            if a["fingerprint"] not in current_fps
            and not is_muted(a["campaign_id"], cfg.mutes, now)
        ]
        # Escalation: still-open criticals nobody acknowledged (muted = acknowledged).
        escalations = [
            e for e in state.escalation_candidates(escalate_after, now)
            if e["fingerprint"] in current_fps
            and not is_muted(e["campaign_id"], cfg.mutes, now)
        ]

        meta = {"campaigns_scanned": len(campaigns)}
        for notifier in _alert_notifiers():
            try:
                if notifier.wants("alerts"):
                    notifier.notify(new, meta)
                    notifier.notify_recovery(resolved)
                    notifier.notify_escalation(escalations)
            except Exception:
                log.exception("notifier %s failed", type(notifier).__name__)
        # Reports/digests are not alarms — they post even in shadow mode.
        for notifier in notifiers:
            try:
                if notifier.wants("reports"):
                    for report in reports:
                        notifier.deliver_report(report)
            except Exception:
                log.exception("notifier %s failed", type(notifier).__name__)
        for f in new:
            state.mark_notified(f)
        for a in resolved:
            state.clear(a["fingerprint"])
        for e in escalations:
            state.mark_escalated(e["fingerprint"])
        state.beat(now)  # heartbeat: record a successful pass for the dead-man's switch

    try:
        if once:
            one_pass()
            return
        while True:
            try:
                one_pass()
            except Exception:
                # A transient failure (e.g. a Metabase 504 on the discovery scan)
                # must not kill the daemon -- log and try again next interval.
                log.exception("scan pass failed; will retry next interval")
            log.info("sleeping %d minutes", interval_minutes)
            time.sleep(interval_minutes * 60)
    finally:
        state.close()


@app.command("list-campaigns")
def list_campaigns(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List campaigns that meet the discovery threshold right now."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    with MetabaseClient(cfg.metabase) as mb:
        campaigns = discover_campaigns(mb, cfg)
    table = Table(
        title=f"Active campaigns (>= {cfg.discovery.min_calls} calls / "
        f"{cfg.windows.current_hours}h · source={cfg.discovery.source}) — {len(campaigns)}"
    )
    table.add_column("campaign_id")
    table.add_column("org_id")
    table.add_column("calls", justify="right")
    table.add_column("last_call")
    for c in campaigns:
        table.add_row(c.campaign_id, c.org_id or "-", str(c.calls), str(c.last_call or "-"))
    console.print(table)


def _deliver_report(cfg: Config, report: Report | None) -> None:
    """Print the report to the console and deliver to any reports-stream notifiers."""
    if report is None:
        console.print("[yellow]Nothing to report.[/yellow]")
        return
    ConsoleNotifier(Severity.INFO, ("reports",), cfg.links).deliver_report(report)
    for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
        if isinstance(notifier, ConsoleNotifier) or not notifier.wants("reports"):
            continue
        try:
            notifier.deliver_report(report)
        except Exception:
            log.exception("notifier %s failed", type(notifier).__name__)


@app.command("run-summary")
def run_summary(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Post the automation run summary (cohorts, rows uploaded/filtered, retry windows)."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    with MetabaseClient(cfg.metabase) as mb:
        report = build_run_summary(mb, cfg)
    _deliver_report(cfg, report)


@app.command("cycle-report")
def cycle_report(
    campaign: str = typer.Option(None, "--campaign", "-c", help="One campaign id (default: all active)."),
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Post a post-cycle performance report (connectivity, engagement, dispositions)."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    with MetabaseClient(cfg.metabase) as mb:
        if campaign:
            ids = [campaign]
        else:
            ids = [c.campaign_id for c in discover_campaigns(mb, cfg)]
        report = build_cycle_report(mb, cfg, ids)
    _deliver_report(cfg, report)


@app.command("weekly-evals")
def weekly_evals(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Post the weekly evals digest (predicted CSAT, engagement, top issues, per goal)."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    with MetabaseClient(cfg.metabase) as mb:
        report = build_weekly_evals(mb, cfg)
    _deliver_report(cfg, report)


@app.command("conversationality-review")
def conversationality_review(
    campaign: str = typer.Option(None, "--campaign", "-c", help="One campaign id (default: all active)."),
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Layer B: LLM-score a sample of transcripts per campaign for conversation quality."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    try:
        llm = LLMClient.from_config(cfg.llm)
    except LLMError as exc:
        console.print(f"[red]LLM error:[/red] {exc}")
        raise typer.Exit(code=2)
    if llm is None:
        console.print("[red]LLM is not configured/enabled ([llm] in config.toml).[/red]")
        raise typer.Exit(code=2)

    with MetabaseClient(cfg.metabase) as mb:
        ids = [campaign] if campaign else [c.campaign_id for c in discover_campaigns(mb, cfg)]
        report, findings = build_conversationality_review(mb, cfg, llm, ids)

    meta = {"campaigns_scanned": len(ids)}
    ConsoleNotifier(Severity.INFO, ("alerts",), cfg.links, OwnerResolver(cfg.owners)).notify(findings, meta)
    _deliver_report(cfg, report)
    for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
        if isinstance(notifier, ConsoleNotifier) or not notifier.wants("alerts"):
            continue
        try:
            notifier.notify(findings, meta)
        except Exception:
            log.exception("notifier %s failed", type(notifier).__name__)


@app.command("value-correctness")
def value_correctness(
    campaign: str = typer.Option(None, "--campaign", "-c", help="One campaign id (default: all active)."),
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """LLM-check whether key values (EMI/date/amount) are stated correctly in transcripts."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    try:
        llm = LLMClient.from_config(cfg.llm)
    except LLMError as exc:
        console.print(f"[red]LLM error:[/red] {exc}")
        raise typer.Exit(code=2)
    if llm is None:
        console.print("[red]LLM is not configured/enabled ([llm] in config.toml).[/red]")
        raise typer.Exit(code=2)

    with MetabaseClient(cfg.metabase) as mb:
        ids = [campaign] if campaign else [c.campaign_id for c in discover_campaigns(mb, cfg)]
        report, findings = build_value_correctness_review(mb, cfg, llm, ids)

    meta = {"campaigns_scanned": len(ids)}
    ConsoleNotifier(Severity.INFO, ("alerts",), cfg.links, OwnerResolver(cfg.owners)).notify(findings, meta)
    _deliver_report(cfg, report)
    for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
        if isinstance(notifier, ConsoleNotifier) or not notifier.wants("alerts"):
            continue
        try:
            notifier.notify(findings, meta)
        except Exception:
            log.exception("notifier %s failed", type(notifier).__name__)


@app.command("client-report")
def client_report(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate per-use-case (per-org) weekly client reports as markdown files."""
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    with MetabaseClient(cfg.metabase) as mb:
        campaigns = discover_campaigns(mb, cfg)
        paths = build_client_reports(mb, cfg, campaigns)
    if not paths:
        console.print("[yellow]No campaigns / data to report.[/yellow]")
        return
    console.print(f"[green]Wrote {len(paths)} client report(s):[/green]")
    for p in paths:
        console.print(f"  {p}")


@app.command("control-server")
def control_server(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
) -> None:
    """Run the Slack control service (Socket Mode, always-on): scope + owners + expected
    rules, all from Slack. Needs slack_bolt + SLACK_BOT_TOKEN, SLACK_APP_TOKEN, and
    SARVAM_ALERTING_SCOPE_URI."""
    _setup_logging(True)
    _load(config, None, None)  # validate config/secrets before starting the loop
    missing = [v for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not os.environ.get(v)]
    if missing:
        console.print(f"[red]Missing env: {', '.join(missing)}[/red] (Socket Mode needs both).")
        raise typer.Exit(code=2)
    if not os.environ.get("SARVAM_ALERTING_SCOPE_URI"):
        console.print("[yellow]SARVAM_ALERTING_SCOPE_URI not set — using local store.json.[/yellow]")
    try:
        from .control.slack_control import run  # optional dep: slack_bolt
    except ImportError:
        console.print("[red]slack_bolt not installed.[/red] Run: uv pip install 'slack-bolt>=1.18'")
        raise typer.Exit(code=2)
    console.print("[green]Starting Slack control service (Ctrl-C to stop)…[/green]")
    run()


owners_app = typer.Typer(
    add_completion=False,
    help="View / add / remove engagement owners (who gets @-mentioned on alerts).",
)
app.add_typer(owners_app, name="owners")

_SEVERITIES = ("info", "warning", "critical")


def _runtime_store(store: str | None) -> tuple[RuntimeStore, str, bool]:
    """Resolve the runtime store: --store, else $SARVAM_ALERTING_SCOPE_URI, else store.json.
    Returns (store, uri, env_is_set) — scans only read owners when the env var is set."""
    env = os.environ.get("SARVAM_ALERTING_SCOPE_URI", "").strip()
    uri = store or env or "store.json"
    return RuntimeStore(uri), uri, bool(env)


def _print_owners(owners: dict, uri: str) -> None:
    if not (owners.get("org") or owners.get("campaign") or owners.get("default")):
        console.print(f"[dim]{uri}[/dim]  —  no engagement owners set.")
        return
    table = Table(
        title=f"Engagement owners  (paged at \u2265 {owners.get('min_severity', 'critical')})  ·  {uri}"
    )
    table.add_column("kind")
    table.add_column("match key")
    table.add_column("owner ids")
    for key, ids in (owners.get("campaign") or {}).items():
        table.add_row("campaign", key, " ".join(ids))
    for key, ids in (owners.get("org") or {}).items():
        table.add_row("org", key, " ".join(ids))
    if owners.get("default"):
        table.add_row("default", "*", " ".join(owners["default"]))
    console.print(table)


def _store_hint(env_is_set: bool, uri: str) -> None:
    if not env_is_set:
        console.print(
            f"[yellow]Heads up:[/yellow] scans read owners from the store only when "
            f"[bold]SARVAM_ALERTING_SCOPE_URI[/bold] is set. To activate this file, run:\n"
            f"  export SARVAM_ALERTING_SCOPE_URI={uri}"
        )


@owners_app.command("list")
def owners_list(
    store: str = typer.Option(None, "--store", help="Runtime store URI (default: $SARVAM_ALERTING_SCOPE_URI or store.json)."),
) -> None:
    """Show the current engagement owners."""
    rs, uri, _ = _runtime_store(store)
    _print_owners(rs.load_owners(), uri)


@owners_app.command("add")
def owners_add(
    key: str = typer.Argument(..., help="org/campaign substring, e.g. chola.com or PAPQ."),
    ids: list[str] = typer.Argument(..., help="Slack ids: U0.. / S0.. / here (names don't ping)."),
    kind: str = typer.Option(None, "--kind", help="org|campaign (default: infer — a dotted key is an org)."),
    store: str = typer.Option(None, "--store"),
) -> None:
    """Add owner(s) for an org or campaign."""
    rs, uri, env_is_set = _runtime_store(store)
    parsed = parse_ids(ids)
    if not parsed:
        console.print("[red]No valid Slack ids.[/red] Use U…/S…/here (plain names can't be @-mentioned).")
        raise typer.Exit(code=2)
    if kind and kind not in ("org", "campaign"):
        console.print("[red]--kind must be org or campaign.[/red]")
        raise typer.Exit(code=2)
    section = kind or ("org" if "." in key else "campaign")
    owners = rs.load_owners()
    owners.setdefault(section, {})
    owners[section][key] = sorted(set(owners[section].get(key, [])) | set(parsed))
    owners.setdefault("min_severity", "critical")
    rs.save_owners(owners)
    console.print(f"[green]Added[/green] {parsed} to {section} `{key}`.")
    _print_owners(owners, uri)
    _store_hint(env_is_set, uri)


@owners_app.command("remove")
def owners_remove(
    key: str = typer.Argument(..., help="org/campaign match key to remove from."),
    ids: list[str] = typer.Argument(None, help="Specific ids to remove (omit to remove the whole key)."),
    store: str = typer.Option(None, "--store"),
) -> None:
    """Remove an owner id, or the whole key if no ids are given."""
    rs, uri, env_is_set = _runtime_store(store)
    owners = rs.load_owners()
    parsed = set(parse_ids(ids)) if ids else set()
    removed = False
    for section in ("org", "campaign"):
        section_map = owners.get(section, {})
        if key in section_map:
            if parsed:
                section_map[key] = [x for x in section_map[key] if x not in parsed]
                if not section_map[key]:
                    section_map.pop(key)
            else:
                section_map.pop(key)
            removed = True
    rs.save_owners(owners)
    console.print("[green]Updated.[/green]" if removed else f"[yellow]No owner rule for `{key}`.[/yellow]")
    _print_owners(owners, uri)
    _store_hint(env_is_set, uri)


@owners_app.command("min")
def owners_min(
    severity: str = typer.Argument(..., help="info | warning | critical — only page owners at/above this."),
    store: str = typer.Option(None, "--store"),
) -> None:
    """Set the minimum severity at which owners get @-mentioned."""
    if severity.lower() not in _SEVERITIES:
        console.print(f"[red]severity must be one of {_SEVERITIES}.[/red]")
        raise typer.Exit(code=2)
    rs, uri, env_is_set = _runtime_store(store)
    owners = rs.load_owners()
    owners["min_severity"] = severity.lower()
    rs.save_owners(owners)
    console.print(f"[green]Owners now paged at \u2265 {severity.lower()}.[/green]")
    _store_hint(env_is_set, uri)


@app.command("heartbeat-check")
def heartbeat_check(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Dead-man's switch: alert if the scan hasn't run successfully recently.

    Run this on a SEPARATE schedule from `watch` (ideally a different host/DAG) so it still
    fires if the main scan is dead. Reads the scan's heartbeat from the shared state store.
    """
    _setup_logging(verbose)
    cfg = _load(config, None, None)
    max_silence_h = float(cfg.tuning.get("heartbeat_max_silence_hours", 2.0))
    state = StateStore(cfg.state.path, cfg.state.cooldown_hours)
    last = state.last_beat()
    state.close()
    now = time.time()

    if last is not None and (now - last) < max_silence_h * 3600:
        mins = int((now - last) / 60)
        console.print(f"[green]OK[/green] — last successful scan {mins} min ago.")
        return

    ago = "never" if last is None else f"{int((now - last) / 3600)}h ago"
    finding = Finding(
        detector="heartbeat",
        severity=Severity.CRITICAL,
        campaign_id="ALERTING-SYSTEM",
        title="Alerting scan has stopped running",
        detail=(
            f"No successful scan in over {max_silence_h:g}h (last: {ago}). The watchdog may be "
            f"down — alerts are NOT being generated right now. Check the scan job/DAG."
        ),
        dedupe_key="heartbeat:stale",
    )
    console.print(f"[red]STALE[/red] — last successful scan {ago}. Alerting.")
    for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
        if notifier.wants("alerts"):
            try:
                notifier.notify([finding], {"campaigns_scanned": 0})
            except Exception:
                log.exception("notifier %s failed", type(notifier).__name__)
    raise typer.Exit(code=1)


@app.command("test-notify")
def test_notify(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
) -> None:
    """Send a synthetic critical finding through all configured notifiers."""
    _setup_logging(False)
    cfg = _load(config, None, None)
    notifiers = build_notifiers(cfg.notifiers, cfg.links, cfg.owners)
    demo = Finding(
        detector="variable_collapse",
        severity=Severity.CRITICAL,
        campaign_id="TEST-CAMPAIGN",
        title="Variable `customer_name` collapsed to a single value",
        detail="This is a test alert from `sarvam-alerting test-notify`.",
        interaction_ids=("abc12345-demo", "def67890-demo"),
        dedupe_key="test",
    )
    for notifier in notifiers:
        notifier.notify([demo], {"campaigns_scanned": 0})
    console.print("[green]Sent test notification through all configured notifiers.[/green]")


if __name__ == "__main__":
    app()
