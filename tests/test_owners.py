"""Tests for engagement-owner tagging (OwnerResolver)."""

from __future__ import annotations

from sarvam_alerting.models import Severity
from sarvam_alerting.owners import OwnerResolver, _mention, parse_ids


def test_mention_formats():
    assert _mention("U0123ABCD") == "<@U0123ABCD>"
    assert _mention("W0123ABCD") == "<@W0123ABCD>"
    assert _mention("S0123TEAM") == "<!subteam^S0123TEAM>"
    assert _mention("here") == "<!here>"
    assert _mention("@channel") == "<!channel>"
    assert _mention("<@U9>") == "<@U9>"           # already formatted, passthrough
    assert _mention("anmol") == ""                # plain name never pings
    assert _mention("") == ""


def test_campaign_rule_wins_over_org():
    r = OwnerResolver(
        {
            "min_severity": "warning",
            "org": {"chola.com": ["U0ORG"]},
            "campaign": {"PAPQ": ["U0CAMP"]},
        }
    )
    # A PAPQ campaign in chola matches both; campaign rule is listed first.
    got = r.mentions_for("chola.com", "PAPQ-chola-123", Severity.WARNING.rank)
    assert got[0] == "<@U0CAMP>"
    assert "<@U0ORG>" in got


def test_default_fallback_when_no_rule_matches():
    r = OwnerResolver({"min_severity": "warning", "default": ["U0DEF"]})
    assert r.mentions_for("idfc.com", "X-1", Severity.WARNING.rank) == ["<@U0DEF>"]


def test_severity_gate_suppresses_below_threshold():
    r = OwnerResolver({"min_severity": "critical", "org": {"chola.com": ["U0ORG"]}})
    assert r.mentions_for("chola.com", "c1", Severity.WARNING.rank) == []
    assert r.mentions_for("chola.com", "c1", Severity.CRITICAL.rank) == ["<@U0ORG>"]


def test_dedupe_preserves_order():
    r = OwnerResolver(
        {
            "min_severity": "info",
            "org": {"chola": ["U0A", "U0B"]},
            "campaign": {"PAPQ": ["U0A"]},
        }
    )
    got = r.mentions_for("chola.com", "PAPQ-1", Severity.INFO.rank)
    assert got == ["<@U0A>", "<@U0B>"]


def test_empty_config_is_unconfigured_and_silent():
    r = OwnerResolver({})
    assert r.configured is False
    assert r.line_for("chola.com", "c1", Severity.CRITICAL.rank) == ""


def test_line_for_renders_mention_line():
    r = OwnerResolver({"min_severity": "critical", "org": {"chola": ["U0A"]}})
    line = r.line_for("chola.com", "c1", Severity.CRITICAL.rank)
    assert "<@U0A>" in line


def test_parse_ids_handles_slack_and_raw_forms():
    got = parse_ids(["<@U012|anmol>", "U345", "@here", "<!subteam^S9>", "S077", "notanid"])
    assert got == ["U012", "U345", "here", "S9", "S077"]
    assert parse_ids(["random", "name"]) == []       # plain names dropped
