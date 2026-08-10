# Akrivia2000

A Telegram bot that sends one short daily report on the stocks you follow —
price, daily and weekly change, and a plain-English explanation written by an AI.

```
📊 Daily report · Fri, 07 Aug · market close

🇺🇸 US market
📉 IONQ — IonQ
$41.20  -6.2% day  -9.1% week
📈 ONTO — Onto Innovation
$123.46  +1.7% day  +3.4% week

🇺🇿 Uzbek exchange (UZSE)
📈 KVTS — Kvarts
12 500 UZS  +2.0% day  +4.1% week

Your US stocks on average: -2.2% today
Whole US market (SPY): +0.4%

🤖 What this means
IonQ fell hardest after a share sale that dilutes existing owners.
Onto rose with other chip-equipment makers on strong memory demand.
The list is concentrated in semiconductors, so these names tend to move together.
```

## Commands

| Command | What it does |
| --- | --- |
| `/add NVDA` | Follow a US company |
| `/add UZ:KVTS` | Follow an Uzbek (UZSE) company |
| `/remove NVDA` | Stop following it |
| `/list` | Everything you follow |
| `/now` | Send the report immediately |
| `/ai NVDA` | Longer plain-English briefing on one company |
| `/settime 09:00` | When the daily report arrives |
| `/settz Europe/Berlin` | Your timezone |
| `/pause`, `/resume` | Stop or restart daily reports |
| `/status` | Your current settings |
| `/help` | How it all works |

## Two exchanges, two data sources

US stocks come from Yahoo Finance (no key, no quota). Uzbek stocks come from a
**parse.bot** scraper, which is metered — a fixed number of requests per month.
The bot never mixes them: separate sections, separate currencies, separate
trading calendars, and the "on average" line covers US stocks only.

Three of the five parse.bot endpoints are used, picked by what each costs:

| Endpoint | Gives | Called |
| --- | --- | --- |
| quotes | the whole market in one response | once per trading day |
| securities | ticker → official company name | once per 30 days |
| detail | 20 sessions of history, day range, volume for one company | only by `/ai`, once per company per day |

The market-wide trade tape and the listings table add nothing the report needs,
so they are never called.

Spending stays low by design:

- **One paid request per trading day**, made only by the scheduled report, and
  cached in SQLite. `/now`, `/list` and `/add UZ:…` validation all read that
  copy for free.
- **History accumulates locally.** Each snapshot's closing prices are appended
  to a local table — filed under the day a share actually traded, not today, so
  a stock that sat still for a week does not get invented price moves. Day and
  week changes are computed from that table and cost nothing.
- **`/ai UZ:…` backfills 20 sessions in one request**, so the week figure is
  real immediately rather than after six days of snapshots.
- **A hard counter refuses to overspend**, and keeps `PARSEBOT_RESERVE` credits
  back so a burst of `/now` can never starve the daily report. `/status` shows
  what is left.

Around 22 trading days a month means roughly 22 of 200 requests used.

Two things about this exchange the code handles explicitly:

- **Commas are thousands separators.** `16,100` is sixteen thousand one hundred.
  Reading it as `16.1` would understate the price a thousandfold and corrupt
  every percentage, so the grouping pattern is matched, not guessed.
- **Trading is thin.** Many shares go days without a single trade. A quote whose
  last trade predates the newest session is labelled
  `⏳ no trades for 5 days — price is from 2026-08-05` rather than being shown
  as if it were current.

## Setup

**1. Get a Telegram bot token.** Message [@BotFather](https://t.me/BotFather),
send `/newbot`, follow the prompts, copy the token.

**2. Get a Groq API key** (free) at
[console.groq.com/keys](https://console.groq.com/keys). This powers the AI
commentary. Without it everything else still works.

Price data for US stocks needs no key — it comes from Yahoo Finance via
`yfinance`. For Uzbek stocks, add your **parse.bot** endpoint and key
(`PARSEBOT_API_URL`, `PARSEBOT_API_KEY`); leave them empty to run US-only.

**3. Configure and run.**

```bash
cp .env.example .env      # then fill in the two keys
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open your bot in Telegram and send `/start`.

### Running on a server

```bash
cp .env.example .env    # fill in the keys
docker compose up -d --build
docker compose logs -f
```

The bot uses long polling, so it needs no domain, no open port and no
certificate. Full instructions, backups and troubleshooting: **[DEPLOY.md](DEPLOY.md)**.

## How it works

```
main.py                      entry point, wiring, long polling
stockbot/
├── config.py                environment variables → validated Config
├── storage.py               SQLite: users, settings, watchlists
├── formatting.py            Telegram message rendering
├── report.py                combines prices + news + AI into one report
├── services/
│   ├── prices.py            Yahoo Finance, cached, one failing ticker is isolated
│   ├── uzse.py              parse.bot scraper: budget guard, day cache, parser
│   └── ai.py                Groq commentary (optional, never fatal)
└── handlers/
    ├── commands.py          /add, /remove, /now, /ai, …
    └── scheduler.py         the once-a-minute "who is due?" job
```

Design decisions worth knowing:

- **Reports describe the last closed trading session,** and the header says which
  one. A report at 09:00 in Europe cannot show "today" for US stocks — the US
  market has not opened yet.
- **No report on weekends and holidays.** The bot tracks which session it last
  sent and stays silent when there is nothing new, instead of resending
  Friday's numbers three times.
- **One scheduler tick per minute** decides who is due, rather than one timer per
  user. It survives restarts and catches up on a report missed while the bot was
  down, without ever sending twice for the same day.
- **Failures degrade, they don't cascade.** A broken ticker becomes one warning
  line; a Groq outage removes the commentary; neither stops the report.
- **Prices sit behind `PriceProvider`,** so swapping Yahoo for Finnhub or Twelve
  Data later means rewriting one file.

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest tests -q
```

Formatting, storage, scheduling and the parse.bot budget rules are covered and
run offline — no network, no API keys needed.
