"""Semantic value-sanity detector — catches *bad-looking* variable values.

`variable_collapse` is statistical: it flags a per-contact variable collapsing to ONE value
versus its baseline. That misses two things this detector catches, on the readable (NOT
encrypted) ``on_start_agent_variables``:

  - **un-rendered template tokens** — a value like ``{{customer_name}}`` / ``%first_name%`` /
    ``<name>`` that shipped because the cohort mapping didn't render.
  - **placeholder / default words** — ``"Customer"``, ``"N/A"``, ``"test"``, ``"null"`` …
    populated for a whole slice of calls even if the column is not strictly single-valued.

Heuristics are deterministic and free (they run every scan). An optional LLM layer
(``use_llm``) adjudicates the *ambiguous* ones — values that aren't obviously placeholders
but look implausible for the field (a name that's a number, a date that isn't a date). The
LLM is one JSON call per campaign, so it stays cheap.
"""

from __future__ import annotations

import json
import logging
import re

from ..clients.llm import LLMClient, LLMError
from ..clients.scheduling import required_variables
from ..models import Finding, Severity
from .base import Detector, DetectorContext

log = logging.getLogger("sarvam_alerting.detectors.value_sanity")

# Values that are placeholders/defaults regardless of what the variable means.
_PLACEHOLDER_WORDS = {
    "customer", "customer name", "name", "your name", "full name", "first name",
    "firstname", "last name", "lastname", "fname", "lname", "user", "username",
    "n/a", "na", "none", "null", "nil", "nan", "undefined", "unknown", "tbd", "tba",
    "test", "testing", "test name", "xyz", "abc", "dummy", "sample", "default",
    "example", "placeholder", "value", "string", "-", "--", ".",
}

# Un-rendered template tokens: {{x}}, {x}, %x%, <x>, ${x}, $x, [[x]], [x]
_TEMPLATE_RE = re.compile(
    r"\{\{.*?\}\}|\{[a-zA-Z0-9_.\s]+\}|%[a-zA-Z0-9_]+%|<[a-zA-Z0-9_]+>"
    r"|\$\{[a-zA-Z0-9_]+\}|\$[a-zA-Z_][a-zA-Z0-9_]*|\[\[.*?\]\]|\[[a-zA-Z0-9_]+\]"
)


def heuristic_reason(value: str) -> str | None:
    """Return why a value looks bad (template/placeholder), or None if it looks fine."""
    v = (value or "").strip()
    if not v:
        return None
    if _TEMPLATE_RE.search(v):
        return "un-rendered template token"
    if v.lower() in _PLACEHOLDER_WORDS:
        return "placeholder/default value"
    return None


class ValueSanityDetector(Detector):
    name = "value_sanity"

    def _cohort_inputs(self, ctx: DetectorContext) -> set[str]:
        """The variables mapped in from the cohort (the per-contact *inputs*).

        Placeholder words like "na" are legitimate for agent-collected *output* slots
        (disposition, is_qualified, …) that start empty and fill during the call. Only a
        cohort *input* showing a placeholder is a mapping bug, so we gate on this set.
        Empty set if the cohort transformation can't be resolved.
        """
        sql = f"""
        SELECT app_id, app_version, count() AS c
        FROM {ctx.metabase.table}
        WHERE campaign_id = {ctx.campaign_literal}
          AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
          AND is_deleted = 0
          AND is_debug_call = 0
        GROUP BY app_id, app_version
        ORDER BY c DESC
        LIMIT 1
        """
        try:
            rows = ctx.metabase.query(sql)
            if not rows:
                return set()
            app_id = str(rows[0].get("app_id") or "")
            try:
                app_version = int(rows[0].get("app_version"))
            except (TypeError, ValueError):
                app_version = None
            return set(required_variables(ctx.metabase, app_id, app_version))
        except Exception:
            log.debug("cohort-input lookup failed", exc_info=True)
            return set()

    def _value_rows(self, ctx: DetectorContext, max_per_var: int) -> list[dict]:
        """Top distinct values per variable in the current window, with per-key totals."""
        sql = f"""
        SELECT key, val, c, sum(c) OVER (PARTITION BY key) AS total
        FROM (
            SELECT key, val, count() AS c
            FROM (
                SELECT on_start_agent_variables AS m
                FROM {ctx.metabase.table}
                WHERE campaign_id = {ctx.campaign_literal}
                  AND created_at_timestamp >= now() - INTERVAL {ctx.current_hours} HOUR
                  AND is_deleted = 0
                  AND is_debug_call = 0
                  AND length(on_start_agent_variables) > 0
            )
            ARRAY JOIN mapKeys(m) AS key, mapValues(m) AS val
            WHERE val != ''
            GROUP BY key, val
        )
        ORDER BY key, c DESC
        LIMIT {max_per_var} BY key
        """
        return ctx.metabase.query(sql)

    def _llm_adjudicate(
        self, ctx: DetectorContext, by_key: dict[str, list[tuple[str, int]]],
        already: set[str], llm_max_vars: int, values_per_var: int,
    ) -> list[tuple[str, str, str]]:
        """Ask the LLM which remaining values look implausible. Returns (key, value, reason)."""
        try:
            llm = LLMClient.from_config(ctx.config.llm)
        except LLMError:
            return []
        if llm is None:
            return []
        payload = {
            k: [{"value": v, "count": c} for v, c in vals[:values_per_var]]
            for k, vals in by_key.items()
            if k not in already
        }
        if not payload:
            return []
        payload = dict(list(payload.items())[:llm_max_vars])
        system = (
            "You audit variable values from automated voice calls for data-mapping bugs. "
            "For each variable you get its most common values with call counts. Identify "
            "values that are NOT real per-contact data: placeholders/defaults (e.g. "
            "'Customer', 'N/A', 'test'), un-rendered template tokens (e.g. '{{name}}'), or "
            "values clearly implausible FOR THAT FIELD (a name that is a number, a due_date "
            "that is not a date, an amount that is text). Be conservative: real names, "
            "amounts, dates, cities, product/loan names are all fine — do not flag them. "
            'Return ONLY JSON: {"suspicious":[{"variable":"..","value":"..","reason":".."}]}'
        )
        try:
            result = llm.json(system, json.dumps(payload, ensure_ascii=False), max_tokens=700)
        except Exception:
            log.debug("value_sanity LLM adjudication failed", exc_info=True)
            return []
        out: list[tuple[str, str, str]] = []
        for item in result.get("suspicious", []) or []:
            k, v = str(item.get("variable", "")), str(item.get("value", ""))
            reason = str(item.get("reason", "implausible value")).strip()
            if k in by_key and any(v == vv for vv, _ in by_key[k]):
                out.append((k, v, reason))
        return out

    def run(self, ctx: DetectorContext) -> list[Finding]:
        min_calls = int(self.opt("min_calls", 100))
        min_share = float(self.opt("min_share", 0.10))
        max_per_var = int(self.opt("max_distinct_per_var", 20))
        use_llm = bool(self.opt("use_llm", False))
        use_cohort_hints = bool(self.opt("use_cohort_hints", True))
        llm_max_vars = int(self.opt("llm_max_vars", 15))
        values_per_var = int(self.opt("llm_values_per_var", 8))

        rows = self._value_rows(ctx, max_per_var)
        if not rows:
            return []

        # Group values per variable; track the per-key total (calls with that var set).
        by_key: dict[str, list[tuple[str, int]]] = {}
        totals: dict[str, int] = {}
        for r in rows:
            key = str(r["key"])
            by_key.setdefault(key, []).append((str(r["val"]), int(r["c"])))
            totals[key] = int(r.get("total") or 0)

        findings: list[Finding] = []
        flagged: set[str] = set()

        def add(key: str, value: str, count: int, reason: str, via: str) -> None:
            total = totals.get(key, 0)
            if total < min_calls:
                return
            share = count / total if total else 0.0
            if share < min_share:
                return
            flagged.add(key)
            sev = Severity.CRITICAL if share >= 0.5 else Severity.WARNING
            findings.append(
                Finding(
                    detector=self.name,
                    severity=sev,
                    campaign_id=ctx.campaign.campaign_id,
                    title=f"Suspicious value for `{key}`",
                    detail=(
                        f"{share:.0%} of calls ({count:,}/{total:,}) in the last "
                        f"{ctx.current_hours}h have `{key}` = \"{value}\" — {reason} "
                        f"({via}). Looks like the cohort value isn't being mapped in."
                    ),
                    metrics={
                        "variable": key, "value": value, "share": round(share, 4),
                        "count": count, "total": total, "reason": reason, "source": via,
                    },
                    dedupe_key=f"{ctx.campaign.campaign_id}:value_sanity:{key}",
                )
            )

        # First pass: split by kind. Template tokens are unambiguous bugs anywhere. Bare
        # placeholder words ("na", "customer") are only a bug on cohort *inputs* — output
        # slots legitimately start blank/na — so those are held pending the input check.
        template_hits: dict[str, tuple[str, int]] = {}
        placeholder_hits: dict[str, tuple[str, int]] = {}
        for key, vals in by_key.items():
            for value, count in vals:
                reason = heuristic_reason(value)
                if reason == "un-rendered template token":
                    if key not in template_hits or count > template_hits[key][1]:
                        template_hits[key] = (value, count)
                elif reason == "placeholder/default value":
                    if key not in placeholder_hits or count > placeholder_hits[key][1]:
                        placeholder_hits[key] = (value, count)

        for key, (value, count) in template_hits.items():
            add(key, value, count, "un-rendered template token", "rule")

        # Only resolve cohort inputs if we actually need them (placeholder candidates or LLM).
        inputs: set[str] = set()
        if use_cohort_hints and (placeholder_hits or use_llm):
            inputs = self._cohort_inputs(ctx)

        for key, (value, count) in placeholder_hits.items():
            if key in flagged:
                continue
            # With hints, restrict to cohort inputs; without hints, skip (avoid output-slot noise).
            if use_cohort_hints and key not in inputs:
                continue
            add(key, value, count, "placeholder/default value", "rule")

        # Optional LLM layer for ambiguous/implausible values on cohort inputs.
        if use_llm:
            candidates = {k: v for k, v in by_key.items()
                          if not use_cohort_hints or k in inputs}
            for key, value, reason in self._llm_adjudicate(
                ctx, candidates, flagged, llm_max_vars, values_per_var
            ):
                if key in flagged:
                    continue
                count = next((c for v, c in by_key[key] if v == value), 0)
                add(key, value, count, reason, "LLM")

        return findings
