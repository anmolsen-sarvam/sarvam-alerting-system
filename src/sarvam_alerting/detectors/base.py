"""Detector protocol and shared context.

A detector is a small, pure-ish unit: given a campaign snapshot context, it runs
one or two ClickHouse queries and returns a list of Findings. Detectors never
notify or mutate state -- that is the engine's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..clients.metabase import MetabaseClient, sql_str
from ..config import Config
from ..models import CampaignInfo, Finding


@dataclass
class DetectorContext:
    metabase: MetabaseClient
    campaign: CampaignInfo
    config: Config

    @property
    def current_hours(self) -> int:
        return self.config.windows.current_hours

    @property
    def baseline_hours(self) -> int:
        return self.config.windows.baseline_hours

    @property
    def total_hours(self) -> int:
        return self.current_hours + self.baseline_hours

    @property
    def campaign_literal(self) -> str:
        return sql_str(self.campaign.campaign_id)

    def window_case(self, ts_col: str = "created_at_timestamp") -> str:
        """SQL expression tagging each row as 'cur' or 'base'."""
        return (
            f"if({ts_col} >= now() - INTERVAL {self.current_hours} HOUR, 'cur', 'base')"
        )

    def base_where(self, ts_col: str = "created_at_timestamp") -> str:
        """WHERE fragment limiting to this campaign and the full (cur+base) window."""
        return (
            f"campaign_id = {self.campaign_literal} "
            f"AND {ts_col} >= now() - INTERVAL {self.total_hours} HOUR "
            f"AND is_deleted = 0 AND is_debug_call = 0"
        )


class Detector(ABC):
    #: config key under [detectors.<name>]
    name: str = "detector"

    def __init__(self, options: dict):
        self.options = options

    @property
    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))

    def opt(self, key: str, default):
        return self.options.get(key, default)

    @abstractmethod
    def run(self, ctx: DetectorContext) -> list[Finding]:
        ...
