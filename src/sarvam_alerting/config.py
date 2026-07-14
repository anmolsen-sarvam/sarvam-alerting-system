"""Configuration loading.

Config lives in a TOML file (parsed with the stdlib ``tomllib``) while secrets
come from environment variables referenced by ``*_env`` keys. This keeps the
committed config secret-free and portable between a laptop and the VM.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class MetabaseConfig:
    base_url: str
    database_id: int
    schema: str
    facts_table: str
    api_key: str
    timeout_seconds: int
    scheduling_database_id: int = 6
    scheduling_schema: str = "public"
    evals_database_id: int = 10
    evals_schema: str = "public"


@dataclass(frozen=True)
class WindowConfig:
    current_hours: int
    baseline_hours: int


@dataclass(frozen=True)
class DiscoveryConfig:
    min_calls: int
    exclude: tuple[str, ...]          # exact campaign_ids to drop
    only: tuple[str, ...]             # exact campaign_id allowlist (empty = all)
    #: "scheduling" (cheap: active campaigns from the scheduling DB) or
    #: "facts" (scan EngagementFacts; heavier, can time out on the gateway)
    source: str = "scheduling"
    #: campaign statuses treated as active (scheduling source only)
    statuses: tuple[str, ...] = ("active", "running")
    #: org scoping (org_id substring match); empty only_orgs = all orgs
    only_orgs: tuple[str, ...] = ()
    exclude_orgs: tuple[str, ...] = ()
    #: campaign_id substring scoping within the selected orgs
    include_patterns: tuple[str, ...] = ()   # empty = all campaigns
    exclude_patterns: tuple[str, ...] = ()

    def accepts(self, campaign_id: str, org_id: str) -> bool:
        """Whether a campaign passes the org + campaign scoping filters."""
        if campaign_id in self.exclude:
            return False
        if self.only and campaign_id not in self.only:
            return False
        if self.exclude_orgs and any(o and o in org_id for o in self.exclude_orgs):
            return False
        if self.only_orgs and not any(o and o in org_id for o in self.only_orgs):
            return False
        if self.exclude_patterns and any(p in campaign_id for p in self.exclude_patterns):
            return False
        if self.include_patterns and not any(p in campaign_id for p in self.include_patterns):
            return False
        return True


@dataclass(frozen=True)
class NotifierConfig:
    type: str
    min_severity: str = "warning"
    #: which streams this notifier receives: "alerts" and/or "reports"
    streams: tuple[str, ...] = ("alerts",)
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StateConfig:
    path: Path
    cooldown_hours: int


@dataclass(frozen=True)
class Config:
    metabase: MetabaseConfig
    windows: WindowConfig
    discovery: DiscoveryConfig
    detectors: dict  # detector name -> options dict (always includes "enabled")
    state: StateConfig
    notifiers: tuple[NotifierConfig, ...]
    reports: dict  # report name -> options dict (always includes "enabled")
    expected: tuple  # list of expected-value / cohort-size rules (raw dicts)
    llm: dict  # LLM config for transcript analysis (may be empty)
    links: dict  # deep-link templates (metabase / call URLs)


def _require_env(var: str) -> str:
    value = os.environ.get(var, "").strip()
    if not value:
        raise ConfigError(
            f"Environment variable {var!r} is required but not set. "
            f"See .env.example."
        )
    return value


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency). Existing env wins."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def load_config(config_path: str | os.PathLike | None = None) -> Config:
    """Load configuration from TOML + environment.

    Search order for the config file: explicit ``config_path`` argument,
    ``$SARVAM_ALERTING_CONFIG``, then ``config/config.toml`` in the CWD.
    """
    # Load .env from the current working directory if present.
    _load_dotenv(Path.cwd() / ".env")

    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path))
    if env_path := os.environ.get("SARVAM_ALERTING_CONFIG"):
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "config" / "config.toml")

    path = next((p for p in candidates if p.expanduser().is_file()), None)
    if path is None:
        raise ConfigError(
            "No config file found. Copy config/config.example.toml to "
            "config/config.toml, or pass --config / set $SARVAM_ALERTING_CONFIG."
        )

    with path.expanduser().open("rb") as fh:
        raw = tomllib.load(fh)

    try:
        mb = raw["metabase"]
        metabase = MetabaseConfig(
            base_url=mb["base_url"].rstrip("/"),
            database_id=int(mb["database_id"]),
            schema=mb["schema"],
            facts_table=mb.get("facts_table", "EngagementFacts"),
            api_key=_require_env(mb.get("api_key_env", "METABASE_API_KEY")),
            timeout_seconds=int(mb.get("timeout_seconds", 90)),
            scheduling_database_id=int(mb.get("scheduling_database_id", 6)),
            scheduling_schema=mb.get("scheduling_schema", "public"),
            evals_database_id=int(mb.get("evals_database_id", 10)),
            evals_schema=mb.get("evals_schema", "public"),
        )

        win = raw.get("windows", {})
        windows = WindowConfig(
            current_hours=int(win.get("current_hours", 6)),
            baseline_hours=int(win.get("baseline_hours", 72)),
        )

        disc = raw.get("discovery", {})
        discovery = DiscoveryConfig(
            min_calls=int(disc.get("min_calls", 500)),
            exclude=tuple(disc.get("exclude", ["NO_CAMPAIGN_ID", ""])),
            only=tuple(disc.get("only", [])),
            source=disc.get("source", "scheduling"),
            statuses=tuple(disc.get("statuses", ["active", "running"])),
            only_orgs=tuple(disc.get("only_orgs", [])),
            exclude_orgs=tuple(disc.get("exclude_orgs", [])),
            include_patterns=tuple(disc.get("include_patterns", [])),
            exclude_patterns=tuple(disc.get("exclude_patterns", [])),
        )

        detectors = dict(raw.get("detectors", {}))

        st = raw.get("state", {})
        state = StateConfig(
            path=Path(st.get("path", "~/.sarvam-alerting/state.db")).expanduser(),
            cooldown_hours=int(st.get("cooldown_hours", 6)),
        )

        notifiers: list[NotifierConfig] = []
        for block in raw.get("notify", []):
            opts = {
                k: v
                for k, v in block.items()
                if k not in ("type", "min_severity", "streams")
            }
            notifiers.append(
                NotifierConfig(
                    type=block["type"],
                    min_severity=block.get("min_severity", "warning"),
                    streams=tuple(block.get("streams", ["alerts"])),
                    options=opts,
                )
            )
        if not notifiers:
            notifiers.append(
                NotifierConfig(
                    type="console", min_severity="info", streams=("alerts", "reports")
                )
            )

        reports = dict(raw.get("reports", {}))
        expected = tuple(raw.get("expected", []))
        llm = dict(raw.get("llm", {}))
        links = dict(raw.get("links", {}))
    except KeyError as exc:
        raise ConfigError(f"Missing required config key: {exc}") from exc

    config = Config(
        metabase=metabase,
        windows=windows,
        discovery=discovery,
        detectors=detectors,
        state=state,
        notifiers=tuple(notifiers),
        reports=reports,
        expected=expected,
        llm=llm,
        links=links,
    )
    # Overlay the runtime scope store (e.g. Slack-controlled) if configured.
    # Imported here (not at module top) to avoid a circular import: scope imports config.
    from .scope import overlay_config

    return overlay_config(config)
