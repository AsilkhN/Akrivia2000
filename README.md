# Akrivia2000

A Telegram bot that sends one short daily report on the stocks you follow —
price, daily and weekly change, and a plain-English explanation written by an AI.

```
📊 Daily report · Fri, 07 Aug · market close

📉 IONQ — IonQ
$41.20  -6.2% day  -9.1% week
📈 ONTO — Onto Innovation
$123.46  +1.7% day  +3.4% week

Your list on average: -1.1% today
Whole US market (SPY): +0.4%

🤖 What this means
IonQ fell hardest after a share sale that dilutes existing owners.
Onto rose with other chip-equipment makers on strong memory demand.
The list is concentrated in semiconductors, so these names tend to move together.
```

## Commands

| Command | What it does |
| --- | --- |
| `/add NVDA` | Follow a company |
| `/remove NVDA` | Stop following it |
| `/list` | Everything you follow |
| `/now` | Send the report immediately |
| `/ai NVDA` | Longer plain-English briefing on one company |
| `/settime 09:00` | When the daily report arrives |
| `/settz Europe/Berlin` | Your timezone |
| `/pause`, `/resume` | Stop or restart daily reports |
| `/status` | Your current settings |
| `/help` | How it all works |

## Setup

**1. Get a Telegram bot token.** Message [@BotFather](https://t.me/BotFather),
send `/newbot`, follow the prompts, copy the token.

**2. Get a Groq API key** (free) at
[console.groq.com/keys](https://console.groq.com/keys). This powers the AI
commentary. Without it everything else still works.

Price data needs no key — it comes from Yahoo Finance via `yfinance`.

**3. Configure and run.**

```bash
cp .env.example .env      # then fill in the two keys
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open your bot in Telegram and send `/start`.

### Running on a server

With Docker:

```bash
docker build -t akrivia2000 .
docker run -d --restart=unless-stopped \
  --env-file .env -v "$PWD/data:/app/data" --name akrivia2000 akrivia2000
```

Or as a systemd service — any always-on process works. The bot uses long
polling, so it needs no public address, no domain and no HTTPS certificate.

Two things matter in production:

- **`DATABASE_PATH` must be on persistent storage.** It holds the watchlists.
  Free hosts with an ephemeral filesystem will wipe it on every deploy.
- **Set `HEARTBEAT_URL`** to a free [healthchecks.io](https://healthchecks.io)
  check. It is pinged after every successful report, so you get told if the
  bot ever goes quiet — otherwise a stopped bot looks exactly like a quiet day.

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
pip install pytest
python -m pytest tests -q
```

Formatting, storage and the scheduling logic are covered and run offline — no
network, no API keys needed.
