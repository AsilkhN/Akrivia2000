# Running Akrivia2000 in production

The bot uses **long polling**, so it needs no domain, no open port, no reverse
proxy and no TLS certificate. Any always-on Linux box with Docker works — a
€4/month VPS is plenty.

## 1. Get the code onto the server

```bash
ssh you@your-server
git clone https://github.com/AsilkhN/Akrivia2000.git
cd Akrivia2000
```

## 2. Create `.env`

```bash
cp .env.example .env
nano .env
```

Minimum to start:

| Variable | Where to get it |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
| `DEFAULT_TIMEZONE` | `Asia/Tashkent` |

For the Uzbek exchange, add the parse.bot endpoints (see `.env.example` for
which is which) and:

```
PARSEBOT_USED_THIS_MONTH=9     # so the counter starts at your real 191 of 200
```

Lock the file down — it holds your keys:

```bash
chmod 600 .env
```

## 3. Start it

```bash
docker compose up -d --build
docker compose logs -f
```

You should see `bot is up and polling`. Open Telegram and send `/start`.

Ctrl-C stops following the logs; it does not stop the bot.

## 4. Check it is healthy

```bash
docker compose ps                 # should say "running"
docker compose logs --tail=50
```

In Telegram, `/status` shows your settings and, if UZSE is configured, how many
parse.bot credits are left this month.

## Day-to-day

| Task | Command |
| --- | --- |
| View logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Update to latest code | `git pull && docker compose up -d --build` |
| Change a setting | edit `.env`, then `docker compose up -d` |
| Shell inside | `docker compose exec bot sh` |

Editing `.env` requires a restart — the file is read once at startup.

## The three things that actually matter

**1. Never delete `./data`.** It holds your watchlists, the parse.bot credit
counter, and the UZSE price history that the weekly figures are built from.
That history accumulates one trading day at a time and cannot be re-fetched
retroactively — losing it means starting the weekly numbers over. The compose
file mounts it as a volume; keep it that way.

Back it up. SQLite has a safe online backup that works while the bot runs:

```bash
docker compose exec bot python -c \
  "import sqlite3,os; s=sqlite3.connect(os.environ.get('DATABASE_PATH','data/stockbot.db')); \
   d=sqlite3.connect('data/backup.db'); s.backup(d); d.close(); print('ok')"
```

Copying the `.db` file with `cp` while the bot is writing can produce a corrupt
copy; the command above cannot.

**2. Set `HEARTBEAT_URL`.** Create a free check at
[healthchecks.io](https://healthchecks.io) and paste its ping URL. The bot pings
it after every successful daily report, so you get emailed if reports stop.
Without it a dead bot and a quiet market look identical — you would not notice
for days.

**3. Watch the parse.bot budget.** `/status` shows credits remaining. Normal use
is around 40 of 200 a month. If that number falls faster than expected,
something is calling more than it should.

## Time zones

`.env` sets the default for new users; `/settz Asia/Tashkent` sets yours. The
container does not need its own clock configured — everything is computed in
your zone from UTC. (The image installs the `tzdata` package deliberately:
slim Python images ship without a timezone database, and `Asia/Tashkent` would
otherwise fail to resolve.)

## If something goes wrong

**`Configuration error: TELEGRAM_BOT_TOKEN is not set`** — `.env` is missing or
in the wrong directory. It must sit next to `docker-compose.yml`.

**`Conflict: terminated by other getUpdates request`** — the same token is
running twice. Telegram allows one poller per bot. Find the other instance:
`docker ps -a | grep akrivia`.

**Reports never arrive** — check `/status`. If "Reports" says *paused*, the bot
paused you after Telegram reported you had blocked it; `/resume` fixes it. If
the time is wrong, check `/settz`.

**UZSE data missing** — `/status` shows credits and whether today's snapshot is
cached. Zero credits means the monthly limit is spent; it resets next month.

**Container restarts in a loop** — `docker compose logs --tail=100`. Almost
always a bad value in `.env`; the bot validates config at startup and says which
variable is wrong.

## Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

For an always-on process, a systemd unit works — set `WorkingDirectory` to the
repo, `EnvironmentFile` to `.env`, and `Restart=always`.
