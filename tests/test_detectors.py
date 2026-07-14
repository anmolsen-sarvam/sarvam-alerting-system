"""Unit tests for detector logic using a fake Metabase client (no network)."""

from __future__ import annotations

import dataclasses

from sarvam_alerting.config import (
    Config,
    DiscoveryConfig,
    MetabaseConfig,
    NotifierConfig,
    StateConfig,
    WindowConfig,
)
from sarvam_alerting.detectors.base import DetectorContext
from sarvam_alerting.detectors.connectivity import ConnectivityDetector
from sarvam_alerting.detectors.expected_values import ExpectedValuesDetector
from sarvam_alerting.detectors.variable_collapse import VariableCollapseDetector
from sarvam_alerting.models import CampaignInfo, Severity


class FakeMetabase:
    def __init__(self, rows):
        self._rows = rows

    @property
    def table(self):
        return "`schema`.EngagementFacts"

    def query(self, sql, **kwargs):
        return self._rows


def _config(expected=()) -> Config:
    from pathlib import Path

    return Config(
        metabase=MetabaseConfig("http://x", 13, "schema", "EngagementFacts", "k", 90),
        windows=WindowConfig(current_hours=6, baseline_hours=72),
        discovery=DiscoveryConfig(min_calls=500, exclude=(), only=()),
        detectors={},
        state=StateConfig(path=Path("/tmp/x.db"), cooldown_hours=6),
        notifiers=(NotifierConfig(type="console"),),
        reports={},
        expected=expected,
        llm={},
        links={},
    )


def _ctx(rows, expected=()) -> DetectorContext:
    return DetectorContext(
        metabase=FakeMetabase(rows),
        campaign=CampaignInfo(campaign_id="C1", calls=1000, org_id="acme.com"),
        config=_config(expected),
    )


def test_variable_collapse_flags_collapsed_per_contact_variable():
    rows = [
        # customer_name: was per-contact (500 distinct in baseline), now 1 value -> BUG
        {"key": "customer_name", "n_cur": 800, "distinct_cur": 1, "sample_cur": "Rahul",
         "n_base": 5000, "distinct_base": 4200},
        # bot_name: legitimately constant everywhere -> must NOT flag
        {"key": "bot_name", "n_cur": 800, "distinct_cur": 1, "sample_cur": "Neha",
         "n_base": 5000, "distinct_base": 1},
    ]
    findings = VariableCollapseDetector({"enabled": True}).run(_ctx(rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == Severity.CRITICAL
    assert f.metrics["variable"] == "customer_name"
    assert f.metrics["current_value"] == "Rahul"


def test_variable_collapse_ignores_low_volume():
    rows = [
        {"key": "customer_name", "n_cur": 10, "distinct_cur": 1, "sample_cur": "Rahul",
         "n_base": 5000, "distinct_base": 4200},
    ]
    findings = VariableCollapseDetector({"enabled": True, "min_calls": 50}).run(_ctx(rows))
    assert findings == []


def test_connectivity_flags_drop_vs_baseline():
    # connected-rate collapsed 60% -> 5% vs baseline; failure spiked 30% -> 95%.
    rows = [{
        "n_cur": 1000, "conn_cur": 50, "fail_cur": 950,
        "n_base": 5000, "conn_base": 3000, "fail_base": 1500,
    }]
    findings = ConnectivityDetector({"enabled": True}).run(_ctx(rows))
    titles = {f.title for f in findings}
    assert "Connectivity dropped sharply" in titles
    assert "Dialer-failure rate spiking" in titles


def test_connectivity_quiet_on_normal_cold_dial():
    # 90% no-answer/failed but STABLE vs baseline (normal cold outbound) -> no alert.
    rows = [{
        "n_cur": 1000, "conn_cur": 100, "fail_cur": 300,
        "n_base": 5000, "conn_base": 500, "fail_base": 1500,
    }]
    findings = ConnectivityDetector({"enabled": True}).run(_ctx(rows))
    assert findings == []


def test_expected_values_flags_disallowed_loan_type():
    rows = [
        {"val": "Digital Personal Loan", "c": 900},
        {"val": "Some Wrong Loan", "c": 100},   # 10% disallowed
    ]
    rule = {
        "name": "D2C loan type",
        "match_campaign_contains": "C1",
        "variable": "loan_type",
        "allowed": ["Digital Personal Loan"],
        "min_share": 0.02,
    }
    findings = ExpectedValuesDetector({"enabled": True}).run(_ctx(rows, expected=(rule,)))
    assert len(findings) == 1
    assert findings[0].metrics["variable"] == "loan_type"
    assert findings[0].metrics["disallowed_share"] == 0.1


def test_expected_values_no_rule_no_finding():
    rows = [{"val": "anything", "c": 100}]
    findings = ExpectedValuesDetector({"enabled": True}).run(_ctx(rows, expected=()))
    assert findings == []


def test_expected_values_ignores_non_matching_campaign():
    rows = [{"val": "Wrong", "c": 100}]
    rule = {"match_campaign_contains": "ZZZ", "variable": "loan_type", "allowed": ["Right"]}
    findings = ExpectedValuesDetector({"enabled": True}).run(_ctx(rows, expected=(rule,)))
    assert findings == []


def test_severity_ordering():
    assert Severity.CRITICAL >= Severity.WARNING
    assert Severity.WARNING >= Severity.INFO
    assert not (Severity.INFO >= Severity.CRITICAL)
