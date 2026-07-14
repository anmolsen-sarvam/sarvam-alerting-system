"""Command-line interface.

Commands:
  check          One-off scan, printed to the console (ignores cooldown state).
  watch          Scheduled scan -> configured notifiers, with dedupe/cooldown.
  list-campaigns Show the campaigns that would be monitored right now.
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
        findings, campaigns = run_scan(mb, cfg, only_campaign=campaign)
    ConsoleNotifier(Severity.INFO, links=cfg.links).notify(
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
    notifiers = build_notifiers(cfg.notifiers, cfg.links)
    state = StateStore(cfg.state.path, cfg.state.cooldown_hours)

    def one_pass() -> None:
        with MetabaseClient(cfg.metabase) as mb:
            findings, campaigns = run_scan(mb, cfg)
            reports = build_reports(mb, cfg, campaigns)
        new = [f for f in findings if state.is_new(f)]
        log.info(
            "scan complete: %d finding(s), %d new, %d campaign(s), %d report(s)",
            len(findings), len(new), len(campaigns), len(reports),
        )
        meta = {"campaigns_scanned": len(campaigns)}
        for notifier in notifiers:
            try:
                if notifier.wants("alerts"):
                    notifier.notify(new, meta)
                if notifier.wants("reports"):
                    for report in reports:
                        notifier.deliver_report(report)
            except Exception:
                log.exception("notifier %s failed", type(notifier).__name__)
        for f in new:
            state.mark_notified(f)

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
    for notifier in build_notifiers(cfg.notifiers, cfg.links):
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
    ConsoleNotifier(Severity.INFO, ("alerts",), cfg.links).notify(findings, meta)
    _deliver_report(cfg, report)
    for notifier in build_notifiers(cfg.notifiers, cfg.links):
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
    ConsoleNotifier(Severity.INFO, ("alerts",), cfg.links).notify(findings, meta)
    _deliver_report(cfg, report)
    for notifier in build_notifiers(cfg.notifiers, cfg.links):
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
    """Run the Slack scope-control service (Socket Mode, always-on). Needs slack_bolt +
    SLACK_BOT_TOKEN, SLACK_APP_TOKEN, and SARVAM_ALERTING_SCOPE_URI."""
    _setup_logging(True)
    _load(config, None, None)  # validate config/secrets before starting the loop
    missing = [v for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not os.environ.get(v)]
    if missing:
        console.print(f"[red]Missing env: {', '.join(missing)}[/red] (Socket Mode needs both).")
        raise typer.Exit(code=2)
    if not os.environ.get("SARVAM_ALERTING_SCOPE_URI"):
        console.print("[yellow]SARVAM_ALERTING_SCOPE_URI not set — using local scope.json.[/yellow]")
    try:
        from .control.slack_control import run  # optional dep: slack_bolt
    except ImportError:
        console.print("[red]slack_bolt not installed.[/red] Run: uv pip install 'slack-bolt>=1.18'")
        raise typer.Exit(code=2)
    console.print("[green]Starting Slack scope-control service (Ctrl-C to stop)…[/green]")
    run()


@app.command("test-notify")
def test_notify(
    config: str = typer.Option(None, "--config", help="Path to config.toml."),
) -> None:
    """Send a synthetic critical finding through all configured notifiers."""
    _setup_logging(False)
    cfg = _load(config, None, None)
    notifiers = build_notifiers(cfg.notifiers, cfg.links)
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
