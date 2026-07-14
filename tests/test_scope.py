"""Tests for the shared scope store + discovery overlay."""

from __future__ import annotations

from sarvam_alerting.config import DiscoveryConfig
from sarvam_alerting.scope import ScopeStore, apply_scope


def test_scope_store_roundtrip(tmp_path):
    store = ScopeStore(str(tmp_path / "scope.json"))
    assert store.load() == {}  # nothing yet

    store.save({"only_orgs": ["chola.com"], "exclude_patterns": ["test"]})
    loaded = store.load()
    assert loaded["only_orgs"] == ["chola.com"]
    assert loaded["exclude_patterns"] == ["test"]


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
