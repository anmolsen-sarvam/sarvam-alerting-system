"""Finding dedupe/cooldown store + heartbeat (SQLite, stdlib only).

Ensures the ``watch`` job alerts on *new* problems and does not re-spam the same
finding every run. A finding is re-alertable once ``cooldown_hours`` have elapsed.
Also tracks a run heartbeat (for the dead-man's switch) and escalation state.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import Finding

_HEARTBEAT_KEY = "last_scan"


class StateStore:
    def __init__(self, path: Path, cooldown_hours: int):
        self._cooldown_seconds = cooldown_hours * 3600
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS findings (
                fingerprint    TEXT PRIMARY KEY,
                campaign_id    TEXT,
                detector       TEXT,
                title          TEXT,
                last_notified  REAL
            )
            """
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL)"
        )
        # Additive migrations for DBs created by older versions.
        self._ensure_columns(
            "findings",
            {
                "org_id": "TEXT DEFAULT ''",
                "severity": "TEXT DEFAULT 'warning'",
                "first_notified": "REAL",
                "escalated": "INTEGER DEFAULT 0",
            },
        )
        self._conn.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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
            INSERT INTO findings
                (fingerprint, campaign_id, detector, title, last_notified,
                 org_id, severity, first_notified, escalated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(fingerprint) DO UPDATE SET last_notified = excluded.last_notified
            """,
            (finding.fingerprint, finding.campaign_id, finding.detector, finding.title,
             now, finding.org_id, finding.severity.value, now),
        )
        self._conn.commit()

    def active_findings(self, now: float | None = None) -> list[dict]:
        """Findings still within their cooldown window — i.e. considered currently 'open'.

        Used to detect *recoveries*: anything open last run but absent this run has cleared.
        """
        now = time.time() if now is None else now
        cutoff = now - self._cooldown_seconds
        rows = self._conn.execute(
            "SELECT fingerprint, campaign_id, detector, title, org_id, severity, "
            "first_notified, escalated FROM findings WHERE last_notified >= ?",
            (cutoff,),
        ).fetchall()
        return [
            {"fingerprint": r[0], "campaign_id": r[1], "detector": r[2], "title": r[3],
             "org_id": r[4] or "", "severity": r[5] or "warning",
             "first_notified": r[6], "escalated": bool(r[7])}
            for r in rows
        ]

    def escalation_candidates(self, escalate_after_seconds: int, now: float | None = None) -> list[dict]:
        """Critical findings first seen >= escalate_after ago, still open, not yet escalated."""
        now = time.time() if now is None else now
        cutoff = now - self._cooldown_seconds
        age_cutoff = now - escalate_after_seconds
        rows = self._conn.execute(
            "SELECT fingerprint, campaign_id, detector, title, org_id, severity, first_notified "
            "FROM findings WHERE last_notified >= ? AND severity = 'critical' "
            "AND escalated = 0 AND first_notified <= ?",
            (cutoff, age_cutoff),
        ).fetchall()
        return [
            {"fingerprint": r[0], "campaign_id": r[1], "detector": r[2], "title": r[3],
             "org_id": r[4] or "", "severity": r[5] or "critical", "first_notified": r[6]}
            for r in rows
        ]

    def mark_escalated(self, fingerprint: str) -> None:
        self._conn.execute(
            "UPDATE findings SET escalated = 1 WHERE fingerprint = ?", (fingerprint,)
        )
        self._conn.commit()

    def clear(self, fingerprint: str) -> None:
        """Forget a finding (e.g. once it has recovered) so it can alert fresh next time."""
        self._conn.execute("DELETE FROM findings WHERE fingerprint = ?", (fingerprint,))
        self._conn.commit()

    # ---- heartbeat (dead-man's switch) --------------------------------------
    def beat(self, now: float | None = None) -> None:
        """Record a successful scan timestamp."""
        now = time.time() if now is None else now
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_HEARTBEAT_KEY, now),
        )
        self._conn.commit()

    def last_beat(self) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_HEARTBEAT_KEY,)
        ).fetchone()
        return float(row[0]) if row else None

    def close(self) -> None:
        self._conn.close()
