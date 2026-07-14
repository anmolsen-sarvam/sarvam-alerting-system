"""Tests for the shared runtime store + config overlays."""

from __future__ import annotations

import json

from sarvam_alerting.config import DiscoveryConfig
from sarvam_alerting.scope import (
    RuntimeStore,
    ScopeStore,
    apply_expected,
    apply_owners,
    apply_scope,
    is_muted,
)


def test_scope_store_roundtrip(tmp_path):
    store = ScopeStore(str(tmp_path / "scope.json"))
    assert store.load() == {}  # nothing yet

    store.save({"only_orgs": ["chola.com"], "exclude_patterns": ["test"]})
    loaded = store.load()
    assert loaded["only_orgs"] == ["chola.com"]
    assert loaded["exclude_patterns"] == ["test"]


def test_runtime_store_sections_are_independent(tmp_path):
    store = RuntimeStore(str(tmp_path / "store.json"))
    store.save_scope({"only_orgs": ["chola.com"]})
    store.save_owners({"min_severity": "critical", "org": {"chola.com": ["U1"]}})
    store.save_expected([{"name": "r", "match_campaign_contains": "D2C", "cohort_size": 100}])

    # Saving one section must not wipe the others.
    assert store.load_scope()["only_orgs"] == ["chola.com"]
    assert store.load_owners()["org"] == {"chola.com": ["U1"]}
    assert store.load_expected()[0]["cohort_size"] == 100


def test_runtime_store_reads_legacy_flat_scope(tmp_path):
    path = tmp_path / "scope.json"
    path.write_text(json.dumps({"only_orgs": ["chola.com"]}))  # old flat format
    store = RuntimeStore(str(path))
    assert store.load_scope() == {"only_orgs": ["chola.com"]}
    assert store.load_owners() == {}


def test_apply_owners_merges_stored_over_config():
    base = {"min_severity": "critical", "org": {"chola.com": ["U0"]}}
    stored = {"org": {"idfc.com": ["U9"]}, "campaign": {"PAPQ": ["U5"]}, "min_severity": "warning"}
    merged = apply_owners(base, stored)
    assert merged["org"] == {"chola.com": ["U0"], "idfc.com": ["U9"]}
    assert merged["campaign"] == {"PAPQ": ["U5"]}
    assert merged["min_severity"] == "warning"


def test_apply_expected_dedupes_by_name():
    base = ({"name": "a", "match_campaign_contains": "X", "cohort_size": 1},)
    stored = [{"name": "a", "match_campaign_contains": "X", "cohort_size": 2}]
    merged = apply_expected(base, stored)
    assert len(merged) == 1
    assert merged[0]["cohort_size"] == 2  # stored wins


def test_mutes_roundtrip_and_section_isolation(tmp_path):
    store = RuntimeStore(str(tmp_path / "store.json"))
    store.save_scope({"only_orgs": ["chola.com"]})
    store.save_mutes({"PAPQ": None, "chola.com": 1720000000.0})
    assert store.load_mutes() == {"PAPQ": None, "chola.com": 1720000000.0}
    assert store.load_scope()["only_orgs"] == ["chola.com"]  # untouched


def test_is_muted_substring_expiry_and_indefinite():
    now = 1_000_000.0
    mutes = {"chola.com": None, "PAPQ": now + 100, "OLD": now - 100}
    assert is_muted("x-chola.com-y", mutes, now) is True     # indefinite
    assert is_muted("PAPQ-1", mutes, now) is True            # not yet expired
    assert is_muted("OLD-1", mutes, now) is False            # expired
    assert is_muted("idfc-1", mutes, now) is False           # no match


def test_apply_scope_overrides_discovery():
    disc = DiscoveryConfig(min_calls=500, exclude=(), only=())
    scoped = apply_scope(disc, {"only_orgs": ["chola.com"], "exclude_patterns": ["test"]})
    assert scoped.only_orgs == ("chola.com",)
    assert scoped.exclude_patterns == ("test",)

    # the scope now drives accepts()
    assert scoped.accepts("PAPQ-chola-123", "chola.com") is True
    assert scoped.accepts("PAPQ-123", "idfc.com") is False          # wrong org
    assert scoped.accepts("test-chola-123", "chola.com") is False   # excluded pattern


def test_apply_scope_empty_is_noop():
    disc = DiscoveryConfig(min_calls=500, exclude=(), only=(), only_orgs=("x",))
    assert apply_scope(disc, {}) is disc
