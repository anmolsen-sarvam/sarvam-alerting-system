"""Samvaad (Sarvam Apps API) client -- v2 enrichment stub.

The Samvaad helper (``~/bin/sarvam-helpers.sh``) authenticates via a local token
server that scrapes a ``platform_id`` cookie from Chrome. That is laptop-bound and
does not work on a headless VM, so v1 intentionally relies on Metabase only.

This stub is where v2 enrichment would live -- e.g. fetching the *expected* cohort
size, campaign/agent display names, or an agent's configured default variable
values, to compare against what Metabase shows was actually used.
"""

from __future__ import annotations

import subprocess


class SamvaadClient:
    """Thin wrapper around the `samvaad` shell helper. Not used by v1 detectors."""

    def __init__(self, helper_path: str = "~/bin/sarvam-helpers.sh"):
        self._helper_path = helper_path

    def token_available(self) -> bool:
        """Return True if the local token server currently has a valid token."""
        try:
            out = subprocess.run(
                ["bash", "-lc", "curl -sf http://localhost:9877/status"],
                capture_output=True, text=True, timeout=5,
            )
            return '"ok": true' in out.stdout or '"ok":true' in out.stdout
        except Exception:
            return False
