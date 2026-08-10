"""Configuration loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

# Uzbek business media with public RSS. Free, no key, best-effort: a feed that
# is down, moved or reshaped simply yields no headlines and the report still
# goes out. kursiv.media runs WordPress, so its investments section exposes a
# feed at the category URL plus /feed/.
DEFAULT_NEWS_FEEDS = ",".join(
    [
        "https://uz.kursiv.media/category/investments/feed/",
        "https://uz.kursiv.media/feed/",
        "https://www.gazeta.uz/uz/rss/",
        "https://www.spot.uz/uz/rss/",
        "https://www.uzdaily.uz/ru/rss",
    ]
)


class ConfigError(RuntimeError):
    """Raised when the environment is missing something the bot cannot run without."""


def _split_tickers(raw: str) -> list[str]:
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


@dataclass(frozen=True)
class Config:
    telegram_token: str
    groq_api_key: str
    groq_model: str
    default_tickers: list[str] = field(default_factory=list)
    default_digest_time: str = "09:00"
    default_timezone: str = "UTC"
    benchmark_ticker: str = "SPY"
    database_path: str = "data/stockbot.db"
    max_tickers_per_user: int = 25
    log_level: str = "INFO"
    heartbeat_url: str = ""
    parsebot_quotes_url: str = ""
    parsebot_securities_url: str = ""
    parsebot_detail_url: str = ""
    parsebot_trades_url: str = ""
    parsebot_listings_url: str = ""
    parsebot_api_key: str = ""
    parsebot_auth_header: str = "Authorization"
    parsebot_auth_scheme: str = "Bearer"
    parsebot_method: str = "GET"
    parsebot_monthly_limit: int = 200
    parsebot_reserve: int = 40
    parsebot_used_this_month: int = 0
    twelvedata_api_key: str = ""
    price_provider: str = "auto"
    scout_enabled: bool = True
    scout_time: str = "18:30"
    news_feeds: list[str] = field(default_factory=list)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def use_twelvedata(self) -> bool:
        """Yahoo blocks datacenter IPs, so a keyed provider wins when available."""
        if self.price_provider == "yahoo":
            return False
        if self.price_provider == "twelvedata":
            return True
        return bool(self.twelvedata_api_key)  # 'auto'

    @property
    def uzse_enabled(self) -> bool:
        return bool(self.parsebot_quotes_url and self.parsebot_api_key)


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    tz_name = os.getenv("DEFAULT_TIMEZONE", "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"DEFAULT_TIMEZONE '{tz_name}' is not a valid timezone.") from exc

    digest_time = os.getenv("DEFAULT_DIGEST_TIME", "09:00").strip() or "09:00"
    if not is_valid_hhmm(digest_time):
        raise ConfigError(f"DEFAULT_DIGEST_TIME '{digest_time}' must look like 09:00.")

    scout_time = os.getenv("SCOUT_TIME", "18:30").strip() or "18:30"
    if not is_valid_hhmm(scout_time):
        raise ConfigError(f"SCOUT_TIME '{scout_time}' must look like 18:30.")

    return Config(
        telegram_token=token,
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
        default_tickers=_split_tickers(os.getenv("DEFAULT_TICKERS", "")),
        default_digest_time=digest_time,
        default_timezone=tz_name,
        benchmark_ticker=os.getenv("BENCHMARK_TICKER", "SPY").strip().upper(),
        database_path=os.getenv("DATABASE_PATH", "data/stockbot.db").strip(),
        max_tickers_per_user=int(os.getenv("MAX_TICKERS_PER_USER", "25")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        heartbeat_url=os.getenv("HEARTBEAT_URL", "").strip(),
        parsebot_quotes_url=os.getenv("PARSEBOT_QUOTES_URL", "").strip(),
        parsebot_securities_url=os.getenv("PARSEBOT_SECURITIES_URL", "").strip(),
        parsebot_detail_url=os.getenv("PARSEBOT_DETAIL_URL", "").strip(),
        parsebot_trades_url=os.getenv("PARSEBOT_TRADES_URL", "").strip(),
        parsebot_listings_url=os.getenv("PARSEBOT_LISTINGS_URL", "").strip(),
        parsebot_api_key=os.getenv("PARSEBOT_API_KEY", "").strip(),
        parsebot_auth_header=os.getenv("PARSEBOT_AUTH_HEADER", "Authorization").strip(),
        parsebot_auth_scheme=os.getenv("PARSEBOT_AUTH_SCHEME", "Bearer").strip(),
        parsebot_method=os.getenv("PARSEBOT_METHOD", "GET").strip(),
        parsebot_monthly_limit=int(os.getenv("PARSEBOT_MONTHLY_LIMIT", "200")),
        parsebot_reserve=int(os.getenv("PARSEBOT_RESERVE", "40")),
        parsebot_used_this_month=int(os.getenv("PARSEBOT_USED_THIS_MONTH", "0")),
        twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY", "").strip(),
        price_provider=os.getenv("PRICE_PROVIDER", "auto").strip().lower(),
        scout_enabled=os.getenv("SCOUT_ENABLED", "true").strip().lower() != "false",
        scout_time=scout_time,
        news_feeds=[f.strip() for f in os.getenv("NEWS_FEEDS", DEFAULT_NEWS_FEEDS).split(",") if f.strip()],
    )


def is_valid_hhmm(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59
