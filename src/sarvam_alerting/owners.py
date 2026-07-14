"""Map a finding's org / campaign to the engagement owners who should be paged.

This turns an alert from a passive channel post into an *action*: the person who
owns that client gets an actual Slack mention, so a silent regression lands in
someone's notifications instead of scrolling past in a channel nobody watches.

Owners are configured in ``[owners]`` (see config.example.toml). Matching is by
substring on ``campaign_id`` first (most specific), then ``org_id``, then a
``default`` fallback. Tagging is severity-gated (``min_severity``, default
``critical``) so owners are only paged on the serious stuff, not every warning.

Only real Slack IDs ping someone: user ids (``U…``/``W…``), user groups
(``S…`` -> ``<!subteam^…>``), and the ``here``/``channel``/``everyone`` keywords.
Plain display names like ``@anmol`` are intentionally ignored because they do NOT
generate a notification in Slack.
"""

from __future__ import annotations

import re

from .models import Severity

_KEYWORDS = {"here", "channel", "everyone"}


def _mention(raw: str) -> str:
    """Return a Slack mention string for a configured id, or '' if it won't ping."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("<") and raw.endswith(">"):
        return raw  # already a formatted mention, pass through
    if raw.startswith("@"):
        raw = raw[1:]
    if raw in _KEYWORDS:
        return f"<!{raw}>"
    if raw[:1] == "S" and raw[1:].isalnum():
        return f"<!subteam^{raw}>"
    if raw[:1] in ("U", "W") and raw[1:].isalnum():
        return f"<@{raw}>"
    return ""  # plain name -> ignore (does not notify)


def parse_ids(tokens: list[str]) -> list[str]:
    """Extract normalized Slack ids from mention tokens or raw ids.

    Accepts ``<@U123>``, ``<@U123|name>``, ``<!subteam^S1>``, ``<!here>``, ``@here``,
    and bare ``U123`` / ``S1`` / ``here``. Returns ids like ``U123`` / ``S1`` / ``here``.
    Plain display names (no id) are dropped — they cannot be @-mentioned reliably.
    """
    ids: list[str] = []
    for tok in tokens:
        m = re.match(r"^<@([UW][A-Z0-9]+)(?:\|[^>]*)?>$", tok)
        if m:
            ids.append(m.group(1))
            continue
        m = re.match(r"^<!subteam\^([A-Z0-9]+)(?:\|[^>]*)?>$", tok)
        if m:
            ids.append(m.group(1))
            continue
        m = re.match(r"^<!(here|channel|everyone)>$", tok)
        if m:
            ids.append(m.group(1))
            continue
        bare = tok.lstrip("@")
        if bare in _KEYWORDS or re.match(r"^[UWS][A-Z0-9]+$", bare):
            ids.append(bare)
    return ids


def format_mention(raw: str) -> str:
    """Public wrapper around the internal mention formatter (for display/reuse)."""
    return _mention(raw)


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]  # type: ignore[union-attr]


class OwnerResolver:
    """Resolve Slack mentions for a finding from ``[owners]`` config rules."""

    def __init__(self, config: dict | None):
        config = config or {}
        self._org = {k: _as_list(v) for k, v in dict(config.get("org", {})).items()}
        self._campaign = {
            k: _as_list(v) for k, v in dict(config.get("campaign", {})).items()
        }
        self._default = _as_list(config.get("default"))
        try:
            self._min_rank = Severity(config.get("min_severity", "critical")).rank
        except ValueError:
            self._min_rank = Severity.CRITICAL.rank

    @property
    def configured(self) -> bool:
        return bool(self._org or self._campaign or self._default)

    def mentions_for(
        self, org_id: str, campaign_id: str, severity_rank: int
    ) -> list[str]:
        """Ordered, de-duplicated mentions for a campaign at a given severity."""
        if severity_rank < self._min_rank:
            return []
        raw: list[str] = []
        for key, vals in self._campaign.items():
            if key and key in campaign_id:
                raw += vals
        for key, vals in self._org.items():
            if key and key in org_id:
                raw += vals
        if not raw:
            raw = list(self._default)
        seen: set[str] = set()
        out: list[str] = []
        for candidate in raw:
            mention = _mention(candidate)
            if mention and mention not in seen:
                seen.add(mention)
                out.append(mention)
        return out

    def line_for(self, org_id: str, campaign_id: str, severity_rank: int) -> str:
        """A ready-to-render mrkdwn line tagging the owners, or '' if none apply."""
        mentions = self.mentions_for(org_id, campaign_id, severity_rank)
        if not mentions:
            return ""
        return ":point_right: " + " ".join(mentions) + " — please take a look"
