"""Notifier interface + shared grouping helpers.

A notifier can subscribe to two streams:
  - "alerts"  -> severity findings (deduped by the engine)
  - "reports" -> always-posted digests (run summary, cycle report)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Finding, Report, Severity


class Notifier(ABC):
    def __init__(
        self,
        min_severity: Severity,
        streams: tuple[str, ...] = ("alerts",),
        links: dict | None = None,
    ):
        self.min_severity = min_severity
        self.streams = set(streams)
        self.links = links or {}

    def wants(self, stream: str) -> bool:
        return stream in self.streams

    def notify(self, findings: list[Finding], meta: dict) -> None:
        filtered = [f for f in findings if f.severity >= self.min_severity]
        self._emit(filtered, meta)

    def deliver_report(self, report: Report) -> None:
        self._emit_report(report)

    @abstractmethod
    def _emit(self, findings: list[Finding], meta: dict) -> None:
        ...

    def _emit_report(self, report: Report) -> None:  # optional override
        ...


def group_by_campaign(findings: list[Finding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = {}
    for f in sorted(findings, key=lambda x: (-x.severity.rank, x.campaign_id)):
        grouped.setdefault(f.campaign_id, []).append(f)
    return grouped
