# options-bot

A $500 paper-account options bot. Trades **SPY and QQQ vertical debit spreads only**, runs on a GitHub Actions cron every 30 minutes during market hours, logs every decision to Supabase, and uses Claude as the gatekeeper for the final enter/exit/hold call. Indicators and signals are computed deterministically in Python; Claude does not see raw price data and does not invent trades.

The validator (`src/validator.py`) is the most important file in the codebase. Every limit lives there.

## Local setup (Windows Git Bash)

```bash
cd "C:\Users\pmcle\OneDrive\Desktop\Claude Projects\options-bot"
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
# Then edit .env to add real keys.
```

Required environment variables (live in `.env` locally, in GitHub Secrets in CI):

| Name | What it is |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper account (Level 3 options, $500) |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Supabase project for logging |
| `ANTHROPIC_API_KEY` | Claude API key (used only for the decision step) |
| `DRY_RUN` | `true` to log decisions without submitting orders. **Leave true until you have green test cycles.** |
| `DAILY_TRADE_CAP` | `1` until June 4, 2026 (PDT sunset); then flip to `3`. |

## Supabase setup

1. Open the Supabase project (the same one shared with other Patrick projects).
2. SQL Editor -> paste the contents of `schema.sql` -> Run.
3. Confirm five tables: `options_cycles`, `options_signals`, `options_orders`, `options_rejections`, `options_positions`.

## GitHub setup

In the repo Settings -> Secrets and variables -> Actions, add:

| Type | Name |
|---|---|
| Secret | `ALPACA_API_KEY` |
| Secret | `ALPACA_SECRET_KEY` |
| Secret | `SUPABASE_URL` |
| Secret | `SUPABASE_SERVICE_ROLE_KEY` |
| Secret | `ANTHROPIC_API_KEY` |
| Variable | `DRY_RUN` (`true` or `false`) |
| Variable | `DAILY_TRADE_CAP` (`1` or `3`) |

The workflow at `.github/workflows/trading_cycle.yml` runs every 30 min from 14:00-20:30 UTC Mon-Fri, and is also `workflow_dispatch` so you can trigger a cycle manually from the Actions tab.

## Running a single cycle manually

```bash
python -m src.main
```

You should see log lines for the account snapshot, the SPY/QQQ signals, and the cycle's final action. A row appears in `options_cycles` no matter what; `options_rejections` is where to look if the bot didn't trade.

## Running tests

```bash
pytest tests/ -v
```

The validator's failure-case tests are the canonical reference for what gets blocked.

## Debugging "why isn't it trading?"

Run these in the Supabase SQL Editor:

```sql
-- Recent cycles, newest first
select id, timestamp, action_taken, notes, daily_pnl, positions_open
from options_cycles order by id desc limit 20;

-- Why was the most recent attempt blocked?
select * from options_rejections order by id desc limit 10;

-- Today's signals
select underlying, signal_type, signal_fired, reason, timestamp
from options_signals where timestamp::date = current_date
order by id desc;

-- Open positions snapshot from the latest cycle
select * from options_positions
where cycle_id = (select max(id) from options_cycles);
```

If `options_cycles.action_taken` is `no_signal` -> no entry signal fired this cycle.
If it's `halt` -> daily P/L is below -$40, no new entries until tomorrow.
If `options_rejections` has rows -> a trade was proposed but the validator blocked it; the `rejection_reason` says why.

## Going live

1. Get a string of green dry-run cycles (`DRY_RUN=true`) with no errors and a few entries logged.
2. Flip `DRY_RUN` to `false`.
3. Watch the first live cycle. The first real order goes through `submit_spread_open` and writes to `options_orders` with `status='submitted'`.

The bot **never** submits market orders. If a limit doesn't fill, it expires at end of day and the bot retries next cycle.
