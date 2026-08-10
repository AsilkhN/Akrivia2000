"""Written commentary from Groq's free LLM API.

Groq exposes an OpenAI-compatible endpoint, so a plain HTTPS call is enough —
no extra SDK to keep in sync. The AI layer is strictly optional: if the key is
missing or the call fails, reports are still sent, just without commentary.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT_SECONDS = 45

SYSTEM_PROMPT = (
    "You explain stock market activity to a smart person who does not work in "
    "finance and does not know the jargon.\n"
    "Rules:\n"
    "- Plain English. If a technical term is unavoidable, define it in three or "
    "four words right after using it.\n"
    "- Be specific and concrete. Never pad with filler like 'markets were mixed' "
    "or 'investors are watching closely'.\n"
    "- Say plainly when a move has no known explanation instead of inventing one.\n"
    "- Do not give buy/sell/hold advice, price targets, or predictions.\n"
    "- No greetings, no sign-offs, no markdown headers, no bold. Plain sentences.\n"
    "- Stay strictly within the length limit you are given."
)


class AIClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def portfolio_comment(self, facts: str, headlines: str) -> str | None:
        """Three short lines tying the day's numbers together."""
        prompt = (
            "Here are today's numbers for the stocks this person follows.\n\n"
            f"{facts}\n\n"
            f"Recent headlines:\n{headlines or '(none available)'}\n\n"
            "Write at most 3 short sentences, 60 words total, covering only what "
            "actually matters here: the biggest mover and the likely reason, and "
            "anything the whole list has in common. One sentence per line, no bullets."
        )
        return await self._complete(prompt, max_tokens=220)

    async def ticker_comment(self, ticker: str, facts: str, headlines: str) -> str | None:
        """A short plain-English briefing on a single company."""
        prompt = (
            f"Company: {ticker}\n\n"
            f"{facts}\n\n"
            f"Recent headlines:\n{headlines or '(none available)'}\n\n"
            "Write about 100 words in three short paragraphs, separated by blank "
            "lines:\n"
            "1) What this company actually sells, and who pays for it.\n"
            "2) What its share price has done recently and the most likely reason.\n"
            "3) The one concrete thing that would move this stock next (an earnings "
            "date, a product launch, a customer decision).\n"
            "No bullets, no headers."
        )
        return await self._complete(prompt, max_tokens=400)

    async def scout_comment(self, period: str, facts: str, headlines: str) -> str | None:
        """The scouting verdict. Kept short, and told what not to be fooled by."""
        horizon = "week" if period == "weekly" else "day"
        prompt = (
            f"Uzbek Stock Exchange, past {horizon}. This is a small, thinly "
            "traded market: most large percentage moves happen on almost no "
            "money and mean nothing.\n\n"
            f"{facts}\n\n"
            f"Headlines from Uzbek business media:\n{headlines or '(none found)'}\n\n"
            "Write at most 4 short sentences, 80 words total:\n"
            "- what actually happened, judged by money traded rather than percentages\n"
            "- name at most two companies genuinely worth watching, and why\n"
            "- if nothing here is meaningful, say exactly that instead of "
            "manufacturing interest\n"
            "One sentence per line. No bullets, no headers."
        )
        return await self._complete(prompt, max_tokens=280)

    async def _complete(self, prompt: str, max_tokens: int) -> str | None:
        if not self.enabled:
            return None
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(GROQ_URL, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Groq returned %s: %s", exc.response.status_code, exc.response.text[:300]
            )
            return None
        except Exception as exc:  # noqa: BLE001 - commentary must never break a report
            logger.warning("Groq request failed: %s", exc)
            return None

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning("Unexpected Groq response shape: %s", str(data)[:300])
            return None

        return text.strip() or None
