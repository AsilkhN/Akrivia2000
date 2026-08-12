"""Commentary from Google Gemini, with Search grounding.

Groq is fast and free but answers only from what it was trained on, so when no
headline reaches the bot a move goes unexplained. Gemini can be given the
Google Search tool, which lets it go and look — the difference between "no
known explanation" and "fell after announcing a share offering".

The prompts are inherited from `AIClient` unchanged, so both providers say the
same kind of thing in the same voice; only the transport differs.

Grounding is not a licence to speculate: when the model does cite sources, the
domains are appended to the commentary so a claim can be checked rather than
taken on faith.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from .ai import SYSTEM_PROMPT, AIClient

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT_SECONDS = 60
MAX_SOURCES = 3


class GeminiClient(AIClient):
    def __init__(self, api_key: str, model: str, use_search: bool = True) -> None:
        super().__init__(api_key, model)
        self._use_search = use_search

    async def _complete(self, prompt: str, max_tokens: int) -> str | None:
        if not self.enabled:
            return None

        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens},
        }
        if self._use_search:
            payload["tools"] = [{"google_search": {}}]

        url = f"{API_ROOT}/{self._model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url, json=payload, headers={"x-goog-api-key": self._api_key}
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Gemini returned %s: %s", exc.response.status_code, exc.response.text[:300]
            )
            return None
        except Exception as exc:  # noqa: BLE001 - commentary must never break a report
            logger.warning("Gemini request failed: %s", exc)
            return None

        return _extract(data)


def _extract(data: object) -> str | None:
    """Pull the text out, and name the sources the model actually used."""
    if not isinstance(data, dict):
        return None
    if "error" in data:
        logger.warning("Gemini error: %s", str(data["error"])[:300])
        return None

    candidates = data.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        logger.warning("Unexpected Gemini response shape: %s", str(data)[:300])
        return None

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    ).strip()
    if not text:
        return None

    sources = _sources(candidate)
    if sources:
        text += "\n\nSources: " + ", ".join(sources)
    return text


def _sources(candidate: dict) -> list[str]:
    """Domains behind the grounded claims, so the reader can check them."""
    metadata = candidate.get("groundingMetadata")
    if not isinstance(metadata, dict):
        return []

    domains: list[str] = []
    for chunk in metadata.get("groundingChunks") or []:
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if not isinstance(web, dict):
            continue
        domain = _domain(web.get("title") or web.get("uri") or "")
        if domain and domain not in domains:
            domains.append(domain)
        if len(domains) >= MAX_SOURCES:
            break
    return domains


def _domain(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        host = urlparse(value).netloc
        return host.removeprefix("www.")
    # Grounding titles are usually already a bare domain.
    return value.removeprefix("www.")
