"""Finding dedupe/cooldown store (SQLite, stdlib only).

Ensures the ``watch`` job alerts on *new* problems and does not re-spam the same
finding every run. A finding is re-alertable once ``cooldown_hours`` have elapsed.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import Finding


class StateStore:
    def __init__(self, path: Path, cooldown_hours: int):
        self._cooldown_seconds = cooldown_hours * 3600
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS findings (
                fingerprint   TEXT PRIMARY KEY,
                campaign_id   TEXT,
                detector      TEXT,
                title         TEXT,
                last_notified REAL
            )
            """
        )
        self._conn.commit()

    def is_new(self, finding: Finding, now: float | None = None) -> bool:
        """True if this finding has never been notified, or the cooldown elapsed."""
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT last_notified FROM findings WHERE fingerprint = ?",
            (finding.fingerprint,),
        ).fetchone()
        if row is None:
            return True
        return (now - float(row[0])) >= self._cooldown_seconds

    def mark_notified(self, finding: Finding, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._conn.execute(
            """
            INSERT INTO findings (fingerprint, campaign_id, detector, title, last_notified)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET last_notified = excluded.last_notified
            """,
            (finding.fingerprint, finding.campaign_id, finding.detector, finding.title, now),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
