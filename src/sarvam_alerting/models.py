"""Core domain types shared across the system."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]

    @property
    def emoji(self) -> str:
        return {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}[self.value]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank >= other.rank
        return NotImplemented


@dataclass(frozen=True)
class CampaignInfo:
    """A campaign discovered as currently active."""

    campaign_id: str
    calls: int
    first_call: datetime | None = None
    last_call: datetime | None = None
    apps: int = 0
    org_id: str = ""


@dataclass(frozen=True)
class Finding:
    """A single problem detected on a campaign."""

    detector: str
    severity: Severity
    campaign_id: str
    title: str
    detail: str
    metrics: dict = field(default_factory=dict)
    org_id: str = ""
    #: example interaction ids that evidence the finding (for deep links)
    interaction_ids: tuple[str, ...] = ()
    # Stable identifier for a *kind* of problem, used for dedupe/cooldown.
    # Example: campaign + detector + variable name (not the changing values).
    dedupe_key: str = ""

    @property
    def fingerprint(self) -> str:
        basis = self.dedupe_key or f"{self.campaign_id}:{self.detector}:{self.title}"
        return hashlib.sha1(basis.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Report:
    """An always-posted digest (not a severity alert): run summary, cycle report, etc."""

    kind: str            # "run_summary" | "cycle_report"
    title: str
    sections: list[str]  # each rendered as one Slack mrkdwn section / console block
    channel_key: str = "reports"

