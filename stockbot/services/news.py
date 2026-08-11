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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# Per-company news, keyless. Google News indexes the financial press and takes
# an arbitrary query; Yahoo's feed host is separate from the API that
# rate-limits datacenter IPs, so it is worth trying as a second source.
DEFAULT_TICKER_FEEDS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
)

# A headline from last month cannot explain today's move, and offering one as
# though it could is worse than saying nothing.
MAX_HEADLINE_AGE_DAYS = 4
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
    published: datetime | None = None


class NewsProvider:
    def __init__(self, feeds: list[str], ticker_feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = [f for f in feeds if f]
        self._ticker_feeds = tuple(ticker_feeds or DEFAULT_TICKER_FEEDS)

    @property
    def enabled(self) -> bool:
        """Per-company lookups need no configuration, only a feed template."""
        return bool(self._feeds or self._ticker_feeds)

    async def fetch_for_ticker(
        self, ticker: str, name: str | None = None, limit: int = 3
    ) -> list[str]:
        """Recent headlines about one company. No API key, no quota.

        This replaced a paid news API: the RSS reader already existed for the
        Uzbek feeds, and Google News takes an arbitrary query, so per-company
        news costs nothing but a request.
        """
        subject = name if name and name.upper() != ticker.upper() else ticker
        query = quote_plus(f"{subject} stock")
        titles: list[str] = []

        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            for template in self._ticker_feeds:
                url = template.format(ticker=quote_plus(ticker), query=query)
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except Exception as exc:  # noqa: BLE001 - news is never fatal
                    logger.warning("news feed %s failed: %s", _source_name(url), exc)
                    continue

                for headline in parse_feed(response.text, _source_name(url)):
                    if not _is_recent(headline):
                        continue
                    title = _strip_source_suffix(headline.title)
                    if title and title not in titles:
                        titles.append(title)
                    if len(titles) >= limit:
                        return titles
        return titles

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
        headlines.append(
            Headline(
                title=title,
                source=source,
                link=_child_text(item, "link"),
                published=_parse_published(_child_text(item, "pubDate")
                                          or _child_text(item, "published")),
            )
        )
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


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_recent(headline: Headline) -> bool:
    """Undated headlines are kept; feeds vary, and a date is a bonus not a rule."""
    if headline.published is None:
        return True
    age = datetime.now(timezone.utc) - headline.published
    return age <= timedelta(days=MAX_HEADLINE_AGE_DAYS)


def _strip_source_suffix(title: str) -> str:
    """Google News appends " - Publisher"; the publisher is not the headline."""
    return re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()


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
