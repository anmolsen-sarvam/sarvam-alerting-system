"""Shared, mutable monitoring scope.

The scope (which orgs / campaign patterns are monitored) needs to be changeable at runtime
by non-deploy actors (e.g. the Slack control app) and read by every ephemeral DAG pod. So
it lives in a shared store — a JSON document addressed by a URI:

  - ``file:///path/scope.json`` or a bare path  → local file (dev)
  - ``s3://bucket/key.json``                     → S3 object (prod, shared across pods)

Schema (all keys optional lists of strings):
  {"only_orgs": [...], "exclude_orgs": [...], "include_patterns": [...], "exclude_patterns": [...]}
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


class ScopeStore:
    def __init__(self, uri: str):
        self.uri = uri

    def _is_s3(self) -> bool:
        return self.uri.startswith("s3://")

    def _local_path(self) -> str:
        p = urlparse(self.uri)
        return p.path if p.scheme in ("file", "") else self.uri

    def load(self) -> dict:
        """Return the scope dict, or {} if not set / unreadable."""
        try:
            if self._is_s3():
                import boto3
                from botocore.exceptions import ClientError

                p = urlparse(self.uri)
                try:
                    obj = boto3.client("s3").get_object(Bucket=p.netloc, Key=p.path.lstrip("/"))
                    raw = obj["Body"].read().decode()
                except ClientError:
                    return {}
            else:
                path = self._local_path()
                if not os.path.exists(path):
                    return {}
                raw = open(path).read()
            data = json.loads(raw or "{}")
            return {k: list(data.get(k, [])) for k in FIELDS if k in data}
        except Exception:
            log.exception("failed to load scope from %s", self.uri)
            return {}

    def save(self, scope: dict) -> None:
        payload = json.dumps({k: list(scope.get(k, [])) for k in FIELDS}, indent=2)
        if self._is_s3():
            import boto3

            p = urlparse(self.uri)
            boto3.client("s3").put_object(
                Bucket=p.netloc, Key=p.path.lstrip("/"), Body=payload.encode()
            )
        else:
            path = self._local_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as fh:
                fh.write(payload)


def apply_scope(discovery: DiscoveryConfig, scope: dict) -> DiscoveryConfig:
    """Overlay a scope dict onto a DiscoveryConfig (only keys present are overridden)."""
    overrides = {k: tuple(scope[k]) for k in FIELDS if k in scope}
    return dataclasses.replace(discovery, **overrides) if overrides else discovery


def overlay_config(config: Config) -> Config:
    """If the scope-store env is set, overlay its scope onto the config's discovery."""
    uri = os.environ.get(SCOPE_ENV, "").strip()
    if not uri:
        return config
    scope = ScopeStore(uri).load()
    if not scope:
        return config
    return dataclasses.replace(config, discovery=apply_scope(config.discovery, scope))
