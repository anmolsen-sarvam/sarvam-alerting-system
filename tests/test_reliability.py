"""Tests for the reliability features: heartbeat, escalation state, stalled detector, feedback."""

from __future__ import annotations

import time

from pathlib import Path

from sarvam_alerting.config import (
    Config,
    DiscoveryConfig,
    MetabaseConfig,
    NotifierConfig,
    StateConfig,
    WindowConfig,
)
from sarvam_alerting.models import Finding, Severity
from sarvam_alerting.scope import RuntimeStore
from sarvam_alerting.state import StateStore


def _config() -> Config:
    return Config(
        metabase=MetabaseConfig("http://x", 13, "schema", "EngagementFacts", "k", 90),
        windows=WindowConfig(current_hours=6, baseline_hours=72),
        discovery=DiscoveryConfig(min_calls=50, exclude=(), only=()),
        detectors={},
        state=StateConfig(path=Path("/tmp/x.db"), cooldown_hours=6),
        notifiers=(NotifierConfig(type="console"),),
        reports={},
        expected=(),
        llm={},
        links={},
        owners={},
    )


def _finding(sev=Severity.CRITICAL, cid="C1", key="C1:connectivity"):
    return Finding(detector="connectivity", severity=sev, campaign_id=cid,
                   title="Connectivity dropped", detail="x", org_id="acme.com", dedupe_key=key)


def test_heartbeat_roundtrip(tmp_path):
    st = StateStore(tmp_path / "state.db", cooldown_hours=6)
    assert st.last_beat() is None
    st.beat(1_000_000.0)
    assert st.last_beat() == 1_000_000.0
    st.close()


def test_escalation_candidates_respects_age_and_severity(tmp_path):
    st = StateStore(tmp_path / "state.db", cooldown_hours=6)
    now = 1_000_000.0
    crit = _finding(Severity.CRITICAL, "C1", "C1:crit")
    warn = _finding(Severity.WARNING, "C2", "C2:warn")
    st.mark_notified(crit, now=now - 3600)   # first seen 1h ago
    st.mark_notified(warn, now=now - 3600)

    # escalate_after 30m: the 1h-old CRITICAL qualifies; the WARNING never does.
    cands = st.escalation_candidates(escalate_after_seconds=1800, now=now)
    assert [c["campaign_id"] for c in cands] == ["C1"]

    # Too-young critical (5m old) does not qualify.
    st.clear(crit.fingerprint)
    st.mark_notified(crit, now=now - 300)
    assert st.escalation_candidates(1800, now) == []


def test_mark_escalated_prevents_repeat(tmp_path):
    st = StateStore(tmp_path / "state.db", cooldown_hours=6)
    now = 1_000_000.0
    crit = _finding(Severity.CRITICAL, "C1", "C1:crit")
    st.mark_notified(crit, now=now - 3600)
    assert len(st.escalation_candidates(1800, now)) == 1
    st.mark_escalated(crit.fingerprint)
    assert st.escalation_candidates(1800, now) == []
    st.close()


def test_active_findings_carries_severity_and_org(tmp_path):
    st = StateStore(tmp_path / "state.db", cooldown_hours=6)
    st.mark_notified(_finding())
    a = st.active_findings()[0]
    assert a["severity"] == "critical"
    assert a["org_id"] == "acme.com"
    st.close()


def test_feedback_log_append_and_cap(tmp_path):
    store = RuntimeStore(str(tmp_path / "store.json"))
    for i in range(5):
        store.append_feedback({"ts": time.time(), "campaign_id": "C1", "action": "mute", "user": "U1"})
    fb = store.load_feedback()
    assert len(fb) == 5
    assert all(e["campaign_id"] == "C1" for e in fb)


def test_stalled_detector_flags_zero_dial(monkeypatch, tmp_path):
    from sarvam_alerting.detectors import stalled
    from sarvam_alerting.clients.scheduling import CampaignRun

    # One active campaign with a cohort, started 5h ago, not ended.
    run = CampaignRun(
        org_id="acme.com", campaign_id="BIG-1", name="Big", status="active",
        app_id="app", app_version=1, start_timestamp="2020-01-01T00:00:00+00:00",
        end_timestamp=None, max_retries=None, retry_windows=None,
        cohort_count=1, total_records=1000, valid_records=1000, rejected_records=0,
    )
    monkeypatch.setattr(stalled, "active_campaign_runs", lambda mb, lh, statuses: [run])

    class FakeMB:
        @property
        def table(self):
            return "`s`.EngagementFacts"

        def query(self, sql, **kw):
            return [{"campaign_id": "BIG-1", "calls": 0}]   # zero dialing

    findings = stalled.find_stalled_campaigns(FakeMB(), _config())
    assert len(findings) == 1
    assert findings[0].detector == "stalled_campaign"
    assert findings[0].severity == Severity.CRITICAL
    assert findings[0].campaign_id == "BIG-1"
