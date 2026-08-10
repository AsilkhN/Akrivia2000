"""Uzbek business news, matched to listed companies.

No news API covers UZSE, so this reads public RSS feeds from Uzbek business
media and matches headlines to companies by name. It costs nothing — no key, no
parse.bot credits — and it is strictly best-effort: a feed that is down, slow or
reshaped simply yields no headlines, and the scout still goes out.

Matching is by keyword rather than anything clever. The exchange gives us the
official name of every listed company, so "Kvarts" or "O'zbektelekom" appearing
in a headline is a strong enough signal. Keywords shorter than five characters
are dropped, because short ones match half the language.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_ITEMS_PER_FEED = 40
MIN_KEYWORD_LENGTH = 5

# Legal-form words and generic nouns that appear in nearly every company name
# on this exchange and would match nearly every headline.
STOPWORDS = {
    "aksiyadorlik", "jamiyati", "jamiyat", "kompaniyasi", "kompani", "banki",
    "bank", "tijorat", "xususiy", "mchj", "atb", "aitb", "atib", "xab", "ilk",
    "eisk", "sug'urta", "sugurta", "tashkiloti", "tashkilot", "mikromoliya",
    "mikrokredit", "korxonasi", "qo'shma", "chet", "kapitali", "ishtirokidagi",
    "respublikasi", "o'zbekiston", "uzbekistan", "milliy", "davlat",
}


@dataclass
class Headline:
    title: str
    source: str
    link: str | None = None


class NewsProvider:
    def __init__(self, feeds: list[str]) -> None:
        self._feeds = [f for f in feeds if f]

    @property
    def enabled(self) -> bool:
        return bool(self._feeds)

    async def fetch(self) -> list[Headline]:
        """Read every configured feed. A broken feed is skipped, never fatal."""
        headlines: list[Headline] = []
        if not self.enabled:
            return headlines

        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            for url in self._feeds:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    headlines.extend(parse_feed(response.text, _source_name(url)))
                except Exception as exc:  # noqa: BLE001 - news must never break a report
                    logger.warning("news feed %s failed: %s", url, exc)
        return headlines


def parse_feed(xml: str, source: str) -> list[Headline]:
    """Read titles out of RSS or Atom without a third-party dependency."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        logger.warning("news feed %s is not valid XML", source)
        return []

    headlines: list[Headline] = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = _child_text(item, "title")
        if not title:
            continue
        headlines.append(Headline(title=title, source=source, link=_child_text(item, "link")))
        if len(headlines) >= MAX_ITEMS_PER_FEED:
            break
    return headlines


def _child_text(element, name: str) -> str | None:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            text = (child.text or "").strip()
            if not text and name == "link":
                text = (child.attrib.get("href") or "").strip()
            return text or None
    return None


def _source_name(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")


def keywords_for(ticker: str, name: str | None) -> list[str]:
    """Distinctive words from a company name, usable as headline search terms."""
    if not name:
        return []
    cleaned = _normalise(name)
    words = [w for w in re.split(r"[^\w']+", cleaned) if w]
    return [
        word
        for word in words
        if len(word) >= MIN_KEYWORD_LENGTH and word not in STOPWORDS
    ]


def match_headlines(
    headlines: list[Headline], companies: dict[str, str | None], limit: int = 2
) -> dict[str, list[str]]:
    """ticker → headlines that mention it, for the companies given."""
    if not headlines:
        return {}

    normalised = [(_normalise(h.title), h) for h in headlines]
    matches: dict[str, list[str]] = {}
    for ticker, name in companies.items():
        for keyword in keywords_for(ticker, name):
            for text, headline in normalised:
                if keyword in text:
                    found = matches.setdefault(ticker, [])
                    if headline.title not in found:
                        found.append(headline.title)
                    if len(found) >= limit:
                        break
            if ticker in matches and len(matches[ticker]) >= limit:
                break
    return matches


def _normalise(text: str) -> str:
    """Lowercase and flatten the apostrophes Uzbek names are written with."""
    lowered = text.lower()
    for variant in ("ʻ", "'", "‘", "’", "`"):
        lowered = lowered.replace(variant, "'")
    return lowered
