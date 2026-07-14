"""Minimal Azure OpenAI chat client (stdlib + httpx), fully config-driven.

Any Azure deployment works -- the model is defined entirely by its ``url``
(which embeds the deployment name + api-version) plus an API key from the env.
Supports an optional stronger ``fallback`` model for borderline / hard calls.
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

log = logging.getLogger("sarvam_alerting.llm")


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        name: str,
        url: str,
        api_key: str,
        timeout_seconds: int = 60,
        fallback: "LLMClient | None" = None,
    ):
        self.name = name
        self._url = url
        self._api_key = api_key
        self._timeout = timeout_seconds
        self.fallback = fallback

    @classmethod
    def from_config(cls, cfg: dict | None) -> "LLMClient | None":
        """Build a client (with optional nested fallback) from an [llm] config dict."""
        if not cfg or not cfg.get("enabled", True):
            return None
        key = os.environ.get(cfg.get("api_key_env", "AZURE_OPENAI_API_KEY"), "").strip()
        if not key:
            raise LLMError(
                f"LLM api key env {cfg.get('api_key_env', 'AZURE_OPENAI_API_KEY')!r} is not set."
            )
        fb_cfg = cfg.get("fallback")
        fallback = None
        if fb_cfg:
            fb_key = os.environ.get(fb_cfg.get("api_key_env", cfg.get("api_key_env", "AZURE_OPENAI_API_KEY")), "").strip()
            if fb_key:
                fallback = cls(
                    name=fb_cfg.get("name", "fallback"),
                    url=fb_cfg["url"],
                    api_key=fb_key,
                    timeout_seconds=int(fb_cfg.get("timeout_seconds", cfg.get("timeout_seconds", 60))),
                )
        return cls(
            name=cfg.get("name", "primary"),
            url=cfg["url"],
            api_key=key,
            timeout_seconds=int(cfg.get("timeout_seconds", 60)),
            fallback=fallback,
        )

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        retries: int = 2,
    ) -> str:
        """Return the assistant message content for a chat completion.

        Adapts to model parameter differences automatically: newer models (e.g. gpt-5)
        require ``max_completion_tokens`` instead of ``max_tokens`` and reject non-default
        ``temperature``. We retry once against each of those 400s without burning a retry.
        """
        use_mct = False       # max_completion_tokens vs max_tokens
        include_temp = True

        def build() -> dict:
            b: dict = {"messages": messages}
            b["max_completion_tokens" if use_mct else "max_tokens"] = max_tokens
            if include_temp:
                b["temperature"] = temperature
            if json_mode:
                b["response_format"] = {"type": "json_object"}
            return b

        last_exc: Exception | None = None
        attempt = 0
        while attempt <= retries:
            try:
                resp = httpx.post(
                    self._url,
                    headers={"Content-Type": "application/json", "api-key": self._api_key},
                    json=build(),
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                attempt += 1
                time.sleep(1.5 * attempt)
                continue

            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]

            text = resp.text[:300]
            # Parameter-compatibility shims (don't count as a retry).
            if resp.status_code == 400 and "max_completion_tokens" in text and not use_mct:
                use_mct = True
                continue
            if resp.status_code == 400 and "temperature" in text and include_temp:
                include_temp = False
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = LLMError(f"{self.name} {resp.status_code}: {text}")
                attempt += 1
                time.sleep(2.0 * attempt)
                continue
            raise LLMError(f"{self.name} {resp.status_code}: {text}")

        raise LLMError(f"{self.name} failed after retries: {last_exc}")

    def json(self, system: str, user: str, *, max_tokens: int = 1500) -> dict:
        """Chat in JSON mode and parse the result; try the fallback model on failure."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            raw = self.chat(messages, max_tokens=max_tokens, json_mode=True)
            return json.loads(raw)
        except (LLMError, json.JSONDecodeError, KeyError) as exc:
            if self.fallback is not None:
                log.warning("primary LLM %s failed (%s); trying fallback %s", self.name, exc, self.fallback.name)
                raw = self.fallback.chat(messages, max_tokens=max_tokens, json_mode=True)
                return json.loads(raw)
            raise
