"""Tests for the Slack control command handling (store-mutating commands) + recovery state."""

from __future__ import annotations

import time

from sarvam_alerting.control.slack_control import _parse_duration, handle
from sarvam_alerting.scope import RuntimeStore
from sarvam_alerting.state import StateStore


def _runner(store):
    said: list[str] = []

    def run(text: str) -> None:
        tokens = text.split()
        raw = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        handle(tokens, raw, store, said.append)

    return run, said


def test_parse_duration():
    assert _parse_duration("4h") == 4 * 3600
    assert _parse_duration("30m") == 30 * 60
    assert _parse_duration("2d") == 2 * 86400
    assert _parse_duration("nope") is None


def test_mute_unmute_via_commands(tmp_path):
    store = RuntimeStore(str(tmp_path / "store.json"))
    run, said = _runner(store)

    run("mute chola.com 4h")
    mutes = store.load_mutes()
    assert "chola.com" in mutes
    assert mutes["chola.com"] > time.time()      # future expiry

    run("mute PAPQ")                              # indefinite
    assert store.load_mutes()["PAPQ"] is None

    run("unmute chola.com")
    assert "chola.com" not in store.load_mutes()
    assert "PAPQ" in store.load_mutes()


def test_mute_bad_duration_is_rejected(tmp_path):
    store = RuntimeStore(str(tmp_path / "store.json"))
    run, said = _runner(store)
    run("mute chola.com forever")
    assert store.load_mutes() == {}              # not saved
    assert any("Duration must look like" in m for m in said)


def test_help_and_unknown(tmp_path):
    store = RuntimeStore(str(tmp_path / "store.json"))
    run, said = _runner(store)
    run("help")
    assert any("See what's going on" in m for m in said)
    run("frobnicate")
    assert any("Unknown command" in m for m in said)


def test_state_recovery_tracking(tmp_path):
    from sarvam_alerting.models import Finding, Severity

    st = StateStore(tmp_path / "state.db", cooldown_hours=6)
    f = Finding(detector="connectivity", severity=Severity.WARNING, campaign_id="C1",
                title="Connectivity dropped", detail="x", dedupe_key="C1:connectivity")
    st.mark_notified(f)
    active = st.active_findings()
    assert len(active) == 1 and active[0]["campaign_id"] == "C1"
    st.clear(f.fingerprint)
    assert st.active_findings() == []
    st.close()