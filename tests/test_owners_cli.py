"""CLI tests for `sarvam-alerting owners` (view/add/remove), writing a temp store."""

from __future__ import annotations

from typer.testing import CliRunner

from sarvam_alerting.cli import app
from sarvam_alerting.scope import RuntimeStore

runner = CliRunner()


def test_owners_add_infers_org_and_persists(tmp_path):
    store = str(tmp_path / "store.json")
    res = runner.invoke(app, ["owners", "add", "chola.com", "U0ANMOL", "U0PRIYA", "--store", store])
    assert res.exit_code == 0
    owners = RuntimeStore(store).load_owners()
    assert owners["org"]["chola.com"] == ["U0ANMOL", "U0PRIYA"]  # dotted key => org, sorted


def test_owners_add_campaign_and_remove_one(tmp_path):
    store = str(tmp_path / "store.json")
    runner.invoke(app, ["owners", "add", "PAPQ", "U0A", "U0B", "--kind", "campaign", "--store", store])
    res = runner.invoke(app, ["owners", "remove", "PAPQ", "U0A", "--store", store])
    assert res.exit_code == 0
    owners = RuntimeStore(store).load_owners()
    assert owners["campaign"]["PAPQ"] == ["U0B"]


def test_owners_remove_whole_key(tmp_path):
    store = str(tmp_path / "store.json")
    runner.invoke(app, ["owners", "add", "idfc.com", "U0X", "--store", store])
    runner.invoke(app, ["owners", "remove", "idfc.com", "--store", store])
    owners = RuntimeStore(store).load_owners()
    assert "idfc.com" not in owners.get("org", {})


def test_owners_add_rejects_plain_names(tmp_path):
    store = str(tmp_path / "store.json")
    res = runner.invoke(app, ["owners", "add", "chola.com", "anmol", "--store", store])
    assert res.exit_code == 2  # no valid Slack ids


def test_owners_list_empty(tmp_path):
    res = runner.invoke(app, ["owners", "list", "--store", str(tmp_path / "store.json")])
    assert res.exit_code == 0
    assert "no engagement owners" in res.stdout.lower()


def test_owners_min_validates_severity(tmp_path):
    store = str(tmp_path / "store.json")
    assert runner.invoke(app, ["owners", "min", "bogus", "--store", store]).exit_code == 2
    assert runner.invoke(app, ["owners", "min", "warning", "--store", store]).exit_code == 0
    assert RuntimeStore(store).load_owners()["min_severity"] == "warning"
