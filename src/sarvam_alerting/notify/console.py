"""Console notifier -- rich terminal output for on-demand `check` runs."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..deeplinks import finding_links
from ..models import Finding, Report, Severity
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
    ):
        super().__init__(min_severity, streams, links)
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
