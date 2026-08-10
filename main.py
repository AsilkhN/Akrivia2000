"""Entry point: wire up storage, services and Telegram handlers, then poll."""

from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters

from stockbot.config import ConfigError, load_config
from stockbot.handlers import commands, scheduler
from stockbot.report import ReportBuilder
from stockbot.services.ai import AIClient
from stockbot.services.prices import PriceProvider
from stockbot.services.twelvedata import TwelveDataProvider
from stockbot.services.news import NewsProvider
from stockbot.services.uzse import UzseProvider
from stockbot.scout import Scout
from stockbot.storage import Storage

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("now", "Send the report right now"),
    BotCommand("list", "Companies you follow"),
    BotCommand("add", "Follow a company: /add NVDA"),
    BotCommand("remove", "Stop following: /remove NVDA"),
    BotCommand("ai", "Plain-English briefing: /ai NVDA"),
    BotCommand("scout", "What is happening on the Uzbek exchange"),
    BotCommand("settime", "Report time: /settime 09:00"),
    BotCommand("settz", "Your timezone: /settz Europe/Berlin"),
    BotCommand("status", "Your current settings"),
    BotCommand("pause", "Pause daily reports"),
    BotCommand("resume", "Resume daily reports"),
    BotCommand("help", "How this bot works"),
]


def build_application() -> Application:
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
    # httpx logs every Telegram poll at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    storage = Storage(config.database_path)
    if config.use_twelvedata:
        if not config.twelvedata_api_key:
            raise ConfigError(
                "PRICE_PROVIDER=twelvedata but TWELVEDATA_API_KEY is not set."
            )
        prices = TwelveDataProvider(config.twelvedata_api_key)
        logger.info("prices: Twelve Data")
    else:
        prices = PriceProvider()
        logger.info(
            "prices: Yahoo Finance. If quotes fail with HTTP 429, this host's IP "
            "is rate-limited — set TWELVEDATA_API_KEY to switch providers."
        )
    ai = AIClient(config.groq_api_key, config.groq_model)
    uzse = UzseProvider(
        storage,
        quotes_url=config.parsebot_quotes_url,
        securities_url=config.parsebot_securities_url,
        detail_url=config.parsebot_detail_url,
        trades_url=config.parsebot_trades_url,
        listings_url=config.parsebot_listings_url,
        api_key=config.parsebot_api_key,
        monthly_limit=config.parsebot_monthly_limit,
        reserve=config.parsebot_reserve,
        auth_header=config.parsebot_auth_header,
        auth_scheme=config.parsebot_auth_scheme,
        method=config.parsebot_method,
    )
    news = NewsProvider(config.news_feeds)
    scout = Scout(storage, uzse)
    reports = ReportBuilder(
        storage, prices, ai, config.benchmark_ticker, uzse, scout=scout, news=news
    )

    if not config.ai_enabled:
        logger.warning("GROQ_API_KEY is not set — reports will have no AI commentary.")
    if config.uzse_enabled:
        if config.parsebot_used_this_month:
            uzse.seed_credits_used(config.parsebot_used_this_month)
        logger.info(
            "parse.bot enabled: %s of %s credits left this month",
            uzse.credits_remaining(),
            config.parsebot_monthly_limit,
        )

    application = ApplicationBuilder().token(config.telegram_token).post_init(_post_init).build()
    application.bot_data.update(
        {
            "config": config,
            "storage": storage,
            "prices": prices,
            "reports": reports,
            "uzse": uzse,
        }
    )

    handlers = {
        "start": commands.start,
        "help": commands.help_command,
        "add": commands.add,
        "remove": commands.remove,
        "list": commands.list_tickers,
        "now": commands.now,
        "ai": commands.ai_briefing,
        "scout": commands.scout,
        "settime": commands.settime,
        "settz": commands.settz,
        "pause": commands.pause,
        "resume": commands.resume,
        "status": commands.status,
    }
    for name, callback in handlers.items():
        application.add_handler(CommandHandler(name, callback))
    application.add_handler(MessageHandler(filters.COMMAND, commands.unknown))
    application.add_error_handler(_on_error)

    application.job_queue.run_repeating(
        scheduler.daily_tick, interval=scheduler.TICK_SECONDS, first=10, name="daily_tick"
    )
    return application


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("bot is up and polling")


async def _on_error(update: object, context) -> None:
    logger.exception("unhandled error while processing %s", update, exc_info=context.error)


def main() -> None:
    try:
        application = build_application()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
