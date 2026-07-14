"""Shared, mutable runtime store — the Slack-driven control plane.

Everything CS/QC needs to change day-to-day lives in ONE JSON document, addressed by a URI
(``SARVAM_ALERTING_SCOPE_URI``) so the Slack control app can write it and every ephemeral
DAG pod can read it:

  - ``file:///path/store.json`` or a bare path  → local file (dev)
  - ``s3://bucket/key.json``                     → S3 object (prod, shared across pods)

Document schema (all sections optional)::

    {
      "scope":    {"only_orgs": [...], "exclude_orgs": [...],
                   "include_patterns": [...], "exclude_patterns": [...]},
      "owners":   {"min_severity": "critical", "default": [...],
                   "org": {"<substr>": [...]}, "campaign": {"<substr>": [...]}},
      "expected": [ {"name": ..., "match_campaign_contains": ..., "variable": ...,
                     "allowed": [...]}  |  {..., "cohort_size": N} ],
      "mutes":    {"<campaign_substr>": <until_epoch_seconds> | null}   # null = indefinite
    }

Secrets (Metabase key, Slack tokens, S3 URIs) are deliberately NOT here — they stay in the
environment / K8s secret. Everything else is Slack-controllable, no file edits, no deploy.

Backward compatibility: an older *flat* scope file (scope fields at the top level) is read as
the ``scope`` section.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from urllib.parse import urlparse

from .config import Config, DiscoveryConfig

log = logging.getLogger("sarvam_alerting.scope")

SCOPE_ENV = "SARVAM_ALERTING_SCOPE_URI"
FIELDS = ("only_orgs", "exclude_orgs", "include_patterns", "exclude_patterns")
SECTIONS = ("scope", "owners", "expected", "mutes", "feedback")
_FEEDBACK_CAP = 200


class RuntimeStore:
    """Read/write the whole sectioned control document at a URI."""

    def __init__(self, uri: str):
        self.uri = uri

    # ---- raw IO -------------------------------------------------------------
    def _is_s3(self) -> bool:
        return self.uri.startswith("s3://")

    def _local_path(self) -> str:
        p = urlparse(self.uri)
        return p.path if p.scheme in ("file", "") else self.uri

    def _read_text(self) -> str:
        if self._is_s3():
            import boto3
            from botocore.exceptions import ClientError

            p = urlparse(self.uri)
            try:
                obj = boto3.client("s3").get_object(Bucket=p.netloc, Key=p.path.lstrip("/"))
                return obj["Body"].read().decode()
            except ClientError:
                return ""
        path = self._local_path()
        if not os.path.exists(path):
            return ""
        with open(path) as fh:
            return fh.read()

    def _write_text(self, text: str) -> None:
        if self._is_s3():
            import boto3

            p = urlparse(self.uri)
            boto3.client("s3").put_object(
                Bucket=p.netloc, Key=p.path.lstrip("/"), Body=text.encode()
            )
        else:
            path = self._local_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)

    # ---- whole-document ------------------------------------------------------
    def load_doc(self) -> dict:
        """Return a normalized ``{scope, owners, expected, mutes, feedback}`` document."""
        empty = {"scope": {}, "owners": {}, "expected": [], "mutes": {}, "feedback": []}
        try:
            raw = self._read_text()
            if not raw:
                return empty
            data = json.loads(raw)
        except Exception:
            log.exception("failed to load control store from %s", self.uri)
            return empty

        if not isinstance(data, dict):
            return empty
        # Back-compat: a flat scope file (no known sections) is the scope section.
        if not any(s in data for s in SECTIONS) and any(f in data for f in FIELDS):
            return {"scope": {k: list(data[k]) for k in FIELDS if k in data},
                    "owners": {}, "expected": [], "mutes": {}, "feedback": []}
        return {
            "scope": dict(data.get("scope", {})),
            "owners": dict(data.get("owners", {})),
            "expected": list(data.get("expected", [])),
            "mutes": dict(data.get("mutes", {})),
            "feedback": list(data.get("feedback", [])),
        }

    def save_doc(self, doc: dict) -> None:
        payload = {
            "scope": {k: list(doc.get("scope", {}).get(k, [])) for k in FIELDS if doc.get("scope", {}).get(k)},
            "owners": doc.get("owners", {}),
            "expected": doc.get("expected", []),
            "mutes": doc.get("mutes", {}),
            "feedback": doc.get("feedback", [])[-_FEEDBACK_CAP:],
        }
        self._write_text(json.dumps(payload, indent=2))

    # ---- section helpers (load/mutate/save, preserving other sections) ------
    def load_scope(self) -> dict:
        return {k: list(v) for k, v in self.load_doc()["scope"].items() if k in FIELDS}

    def save_scope(self, scope: dict) -> None:
        doc = self.load_doc()
        doc["scope"] = {k: list(scope.get(k, [])) for k in FIELDS if scope.get(k)}
        self.save_doc(doc)

    def load_owners(self) -> dict:
        return dict(self.load_doc()["owners"])

    def save_owners(self, owners: dict) -> None:
        doc = self.load_doc()
        doc["owners"] = owners
        self.save_doc(doc)

    def load_expected(self) -> list:
        return list(self.load_doc()["expected"])

    def save_expected(self, rules: list) -> None:
        doc = self.load_doc()
        doc["expected"] = rules
        self.save_doc(doc)

    def load_mutes(self) -> dict:
        return dict(self.load_doc()["mutes"])

    def save_mutes(self, mutes: dict) -> None:
        doc = self.load_doc()
        doc["mutes"] = mutes
        self.save_doc(doc)

    def load_feedback(self) -> list:
        return list(self.load_doc()["feedback"])

    def append_feedback(self, entry: dict) -> None:
        doc = self.load_doc()
        doc["feedback"] = (doc.get("feedback", []) + [entry])[-_FEEDBACK_CAP:]
        self.save_doc(doc)


class ScopeStore:
    """Backward-compatible scope-only view over :class:`RuntimeStore`."""

    def __init__(self, uri: str):
        self.uri = uri
        self._store = RuntimeStore(uri)

    def load(self) -> dict:
        return self._store.load_scope()

    def save(self, scope: dict) -> None:
        self._store.save_scope(scope)


def apply_scope(discovery: DiscoveryConfig, scope: dict) -> DiscoveryConfig:
    """Overlay a scope dict onto a DiscoveryConfig (only keys present are overridden)."""
    overrides = {k: tuple(scope[k]) for k in FIELDS if k in scope}
    return dataclasses.replace(discovery, **overrides) if overrides else discovery


def apply_owners(base: dict, stored: dict) -> dict:
    """Merge stored owner rules over the config's owners (stored wins per key)."""
    if not stored:
        return base
    merged = dict(base)
    for section in ("org", "campaign"):
        combined = dict(base.get(section, {}))
        combined.update(stored.get(section, {}))
        if combined:
            merged[section] = combined
    if "default" in stored:
        merged["default"] = stored["default"]
    if "min_severity" in stored:
        merged["min_severity"] = stored["min_severity"]
    return merged


def apply_expected(base: tuple, stored: list) -> tuple:
    """Merge stored expected-value rules with the config's (by ``name``, stored wins)."""
    if not stored:
        return base
    by_name: dict = {}
    for i, rule in enumerate([*base, *stored]):
        key = rule.get("name") or f"_rule_{i}"
        by_name[key] = rule
    return tuple(by_name.values())


def is_muted(campaign_id: str, mutes: dict, now: float) -> bool:
    """True if any active mute rule's key is a substring of the campaign id.

    A mute value is an expiry epoch (seconds) or ``None`` for an indefinite mute.
    """
    for key, until in mutes.items():
        if key and key in campaign_id and (until is None or float(until) > now):
            return True
    return False


def overlay_config(config: Config) -> Config:
    """Overlay the Slack-controlled runtime store (scope/owners/expected/mutes) onto config."""
    uri = os.environ.get(SCOPE_ENV, "").strip()
    if not uri:
        return config
    doc = RuntimeStore(uri).load_doc()
    scope, owners, expected, mutes = doc["scope"], doc["owners"], doc["expected"], doc["mutes"]
    if not (scope or owners or expected or mutes):
        return config
    return dataclasses.replace(
        config,
        discovery=apply_scope(config.discovery, scope) if scope else config.discovery,
        owners=apply_owners(config.owners, owners) if owners else config.owners,
        expected=apply_expected(config.expected, expected) if expected else config.expected,
        mutes=mutes or config.mutes,
    )
