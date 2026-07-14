"""Metabase client: run native (ClickHouse) SQL via the dataset endpoint.

We deliberately use the native-query dataset API rather than saved questions so
detectors are self-contained and portable. All aggregation happens server-side
in ClickHouse; we only pull small result sets back.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import MetabaseConfig


class MetabaseError(Exception):
    pass


def sql_str(value: str) -> str:
    """Escape a Python string for safe inlining into a single-quoted SQL literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class MetabaseClient:
    def __init__(self, cfg: MetabaseConfig):
        self._cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.base_url,
            headers={"x-api-key": cfg.api_key, "Content-Type": "application/json"},
            timeout=cfg.timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MetabaseClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def table(self) -> str:
        """Fully-qualified, backtick-quoted facts table for ClickHouse."""
        return f"`{self._cfg.schema}`.{self._cfg.facts_table}"

    def analytics_table(self, name: str) -> str:
        """Fully-qualified, backtick-quoted ClickHouse analytics table (e.g. InteractionMessages)."""
        return f"`{self._cfg.schema}`.{name}"

    def scheduling_table(self, name: str) -> str:
        """Fully-qualified Postgres scheduling table (e.g. public.campaigns)."""
        return f"{self._cfg.scheduling_schema}.{name}"

    def evals_table(self, name: str) -> str:
        """Fully-qualified Postgres evals table (e.g. public.post_call_eval_results)."""
        return f"{self._cfg.evals_schema}.{name}"

    @property
    def scheduling_db(self) -> int:
        return self._cfg.scheduling_database_id

    @property
    def evals_db(self) -> int:
        return self._cfg.evals_database_id

    def query(
        self, sql: str, *, database_id: int | None = None, retries: int = 1
    ) -> list[dict[str, Any]]:
        """Run a native SQL query and return rows as dicts keyed by column name.

        ``database_id`` defaults to the ClickHouse facts DB; pass
        ``scheduling_db`` to hit the Postgres scheduling database.
        """
        payload = {
            "database": database_id if database_id is not None else self._cfg.database_id,
            "type": "native",
            "native": {"query": sql},
        }
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.post("/dataset", json=payload)
            except httpx.HTTPError as exc:  # network/timeout
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code >= 500:
                last_exc = MetabaseError(f"Metabase {resp.status_code}: {resp.text[:300]}")
                time.sleep(1.5 * (attempt + 1))
                continue
            # The dataset endpoint returns 200 or 202 (with the full body) on success.
            if resp.status_code not in (200, 202):
                raise MetabaseError(f"Metabase {resp.status_code}: {resp.text[:500]}")

            body = resp.json()
            if body.get("status") == "failed" or body.get("error"):
                raise MetabaseError(f"Query failed: {body.get('error')}\nSQL: {sql[:500]}")

            data = body.get("data", {})
            cols = [c["name"] for c in data.get("cols", [])]
            return [dict(zip(cols, row)) for row in data.get("rows", [])]

        raise MetabaseError(f"Metabase request failed after retries: {last_exc}")
