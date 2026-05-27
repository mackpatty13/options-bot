# options-bot

A ~$50,000 paper-account options bot. Trades **SPY and QQQ vertical debit spreads only**, runs on a GitHub Actions cron every 30 minutes during market hours, logs every decision to Supabase, and uses Claude as the gatekeeper for the final enter/exit/hold call. Indicators and signals are computed deterministically in Python; Claude does not see raw price data and does not invent trades.

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
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper account (Level 3 options, ~$50K) |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Supabase project for logging |
| `ANTHROPIC_API_KEY` | Claude API key (used only for the decision step) |
| `DRY_RUN` | `true` to log decisions without submitting orders. Set to `false` for live paper trading. |
| `DAILY_TRADE_CAP` | Max new entries per day. Sanity throttle only; account is above the $25K PDT threshold so PDT does not apply. Default `10`. |

## Supabase setup

1. Open the Supabase project: **https://kfklyktxmfycowkbdoqz.supabase.co** (this lives in a separate Supabase account from the other Patrick projects, so it is not visible from MCP tools authenticated against the shared org).
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
| Variable | `DAILY_TRADE_CAP` (integer; default `10`) |

The workflow at `.github/workflows/trading_cycle.yml` runs every 30 min from 14:00-20:30 UTC Mon-Fri, and is also `workflow_dispatch` so you can trigger a cycle manually from the Actions tab.

**Cron reliability:** GitHub Actions scheduled cron drops or delays runs under load. Observed 6 of ~14 expected runs on 2026-05-14, with several minutes of drift on the ones that did fire. The entry window logic in `main.py` enforces 10:00-15:00 ET internally, so late-firing cycles just become `hold`s rather than misfiring. If dropped runs become a real problem, migrate to a dedicated scheduler (small VPS, Cloudflare Worker cron) rather than trying to harden GH Actions.

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

Diagnosis tree for an `options_cycles` row with no resulting trade:

| `action_taken` | What it means | Where to look next |
|---|---|---|
| `no_signal` | Neither SPY nor QQQ fired this cycle | `options_signals.reason` for the EMA diagnostics |
| `halt` | Daily realized P/L is below -$2,000 | Wait for the next trading day |
| `hold` (signal fired, no rejection row) | Bot reached the entry path, built a spread, asked Claude, and **Claude returned `hold`**. This is the most common silent path — Claude is the gatekeeper and is conservative when context doesn't fit (e.g., account too small relative to debit, ambiguous signals, etc.) | GitHub Actions run log for the `claude decision: ...` line |
| `hold` (signal fired, rejection row present, reason starts `no qualifying spread`) | Signal fired but the option chain had nothing that satisfied OI, bid-ask, debit-range, and delta-band rules together. Common when IEX-feed greeks/OI are sparse | `options_rejections.rejection_reason` and chain inspection |
| `hold` (signal fired, rejection row present, validator reason) | Claude said `enter` but the validator blocked. The reason text states which rule | `options_rejections.rejection_reason` |
| `entry` / `exit` | The bot did act | `options_orders` for the submitted order; Alpaca for fill status |

A cycle row whose `notes` is literally `'initial; will be updated'` means the cycle crashed mid-flight — the placeholder set at `main.py:98` was never overwritten by the final update at `main.py:289`. Check the GH Actions run log for the traceback.

A bot pre-launch checklist (in order of "things that have actually bitten us"):

1. Alpaca paper account is funded **at or above $20K** (validator floor) and ideally near $50K (the design point — see `MIN_EQUITY`, `MAX_PCT_OF_EQUITY_PER_POSITION`, `DEBIT_MIN` interactions; a $500 account is mathematically incompatible with the rules).
2. `DRY_RUN` repo variable is `false` for actual submission. `true` is fine for validation but produces only `DRY enter ...` log lines, never orders.
3. `DAILY_TRADE_CAP` is set conservatively (3-5) until the full pipeline has been observed end-to-end. The cap of 50 is fine once trusted, but at $300-$2,000 debit per entry it can deploy most of a $50K BP in a single day.
4. Run one in-window cycle (10:00-15:00 ET) and confirm an `options_orders` row appears with `status='submitted'` before walking away.

## Going live

1. Get a string of green dry-run cycles (`DRY_RUN=true`) with no errors and a few entries logged.
2. Flip `DRY_RUN` to `false`.
3. Watch the first live cycle. The first real order goes through `submit_spread_open` and writes to `options_orders` with `status='submitted'`.

The bot **never** submits market orders. If a limit doesn't fill, it expires at end of day and the bot retries next cycle.
