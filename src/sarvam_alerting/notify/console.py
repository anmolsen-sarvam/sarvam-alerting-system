"""Console notifier -- rich terminal output for on-demand `check` runs."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..deeplinks import finding_links
from ..models import Finding, Report, Severity
from ..owners import OwnerResolver
from .base import Notifier, group_by_campaign

_STYLE = {
    Severity.INFO: "cyan",
    Severity.WARNING: "yellow",
    Severity.CRITICAL: "bold red",
}


class ConsoleNotifier(Notifier):
    def __init__(
        self,
        min_severity: Severity,
        streams: tuple[str, ...] = ("alerts",),
        links: dict | None = None,
        owners: OwnerResolver | None = None,
    ):
        super().__init__(min_severity, streams, links, owners)
        self._console = Console()

    def _emit(self, findings: list[Finding], meta: dict) -> None:
        scanned = meta.get("campaigns_scanned", 0)
        if not findings:
            self._console.print(
                f"[green]No issues[/green] across {scanned} campaign(s) scanned."
            )
            return

        for campaign_id, items in group_by_campaign(findings).items():
            body = Text()
            for f in items:
                style = _STYLE[f.severity]
                body.append(f"\n[{f.severity.value.upper()}] ", style=style)
                body.append(f.title, style="bold")
                body.append(f"\n   {f.detail}\n")
                evidence = [f"{label}: {url}" for label, url in finding_links(f, self.links)]
                if f.interaction_ids and not any("call" in e for e in evidence):
                    evidence.append("calls: " + ", ".join(i[:8] for i in f.interaction_ids[:3]))
                if evidence:
                    body.append("   " + "  ".join(evidence) + "\n", style="dim")
            if self.owners is not None:
                org_id = items[0].org_id if items else ""
                worst_rank = max(f.severity.rank for f in items)
                mentions = self.owners.mentions_for(org_id, campaign_id, worst_rank)
                if mentions:
                    body.append("   owners: " + " ".join(mentions) + "\n", style="magenta")
            self._console.print(
                Panel(body, title=f"campaign {campaign_id}", border_style="red")
            )
        self._console.print(
            f"\n{len(findings)} finding(s) across {scanned} campaign(s)."
        )

    def _emit_report(self, report: Report) -> None:
        body = Text()
        for section in report.sections:
            body.append(f"\n{section}\n")
        self._console.print(Panel(body, title=report.title, border_style="cyan"))

    def _emit_recovery(self, recoveries: list[dict]) -> None:
        for r in recoveries:
            self._console.print(
                f"[green]RECOVERED[/green] {r['campaign_id']}: {r['title']}"
            )

    def _emit_escalation(self, items: list[dict]) -> None:
        for r in items:
            self._console.print(
                f"[bold red]ESCALATION[/bold red] {r['campaign_id']}: {r['title']} "
                f"(still unresolved)"
            )
