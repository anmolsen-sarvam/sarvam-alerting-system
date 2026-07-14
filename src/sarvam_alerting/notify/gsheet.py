"""Google Sheet sink -- auto-fills the Master QC sheet with findings.

Appends one row per finding to a worksheet, so the manual "fill findings into the Master
QC sheet" step (from the QC checklist) happens automatically.

Requires the optional `gspread` package and a Google service-account JSON whose email has
edit access to the sheet. Config:

    [[notify]]
    type = "gsheet"
    streams = ["alerts"]
    sheet_id = "1AbC...the spreadsheet id..."
    worksheet = "QC Log"
    creds_env = "GOOGLE_APPLICATION_CREDENTIALS"   # path to the service-account JSON
"""

from __future__ import annotations

import datetime as _dt
import os

import gspread

from ..models import Finding, Severity
from ..owners import OwnerResolver
from .base import Notifier


class GSheetNotifier(Notifier):
    HEADER = [
        "timestamp_utc", "org_id", "campaign_id", "detector",
        "severity", "title", "detail", "interaction_ids",
    ]

    def __init__(
        self,
        min_severity: Severity,
        options: dict,
        streams: tuple[str, ...] = ("alerts",),
        links: dict | None = None,
        owners: OwnerResolver | None = None,
    ):
        super().__init__(min_severity, streams, links, owners)
        self._sheet_id = options.get("sheet_id")
        if not self._sheet_id:
            raise ValueError("gsheet notifier: 'sheet_id' is required.")
        self._worksheet = options.get("worksheet", "QC Log")
        creds_env = options.get("creds_env", "GOOGLE_APPLICATION_CREDENTIALS")
        self._creds_path = os.environ.get(creds_env, "").strip()
        if not self._creds_path:
            raise ValueError(f"gsheet notifier: env {creds_env!r} (service-account JSON path) is not set.")

    def _ws(self):
        gc = gspread.service_account(filename=self._creds_path)
        sh = gc.open_by_key(self._sheet_id)
        try:
            ws = sh.worksheet(self._worksheet)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=self._worksheet, rows=1000, cols=len(self.HEADER))
            ws.append_row(self.HEADER)
        return ws

    def _emit(self, findings: list[Finding], meta: dict) -> None:
        if not findings:
            return
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        rows = [
            [
                now, f.org_id, f.campaign_id, f.detector, f.severity.value,
                f.title, f.detail, ", ".join(f.interaction_ids),
            ]
            for f in findings
        ]
        self._ws().append_rows(rows, value_input_option="RAW")
