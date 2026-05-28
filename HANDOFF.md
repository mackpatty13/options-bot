# Handoff — 2026-05-28

## TL;DR
The bot was **running correctly the whole time** — it just couldn't build a tradeable
spread, so it never entered. Root cause found and fixed: the `DEBIT_MIN` floor was
structurally unreachable on SPY/QQQ's current $1-wide strikes. Change is in the working
tree, verified end-to-end against the live chain, **not yet committed**, and first real
orders will appear next session.

## Reported symptom
"It doesn't do anything; it didn't make any trades today at all."

## What was actually happening
- Task Scheduler ran the bot every 30 min today (cycles 21–30). No crashes, Alpaca and
  Supabase both healthy (equity ~$49,947, BP ~$99,893).
- Signals fired in most cycles (SPY/QQQ trended up).
- Every entry attempt was rejected by `build_spread` → logged in `options_rejections`
  as **"no qualifying spread in chain"** (100% rejection rate).

## Root cause
SPY (~$754) and QQQ now trade **$1-wide strikes** near the money. With
`SHORT_STRIKES_AWAY = 2–3`, each constructed spread is only $2–3 wide and costs
~$80–170/contract — but `DEBIT_MIN` was **$300**, which a $2–3-wide spread can never
reach (debit is bounded by width × 100). So every candidate died at the debit check.

Diagnostic method (repeatable): query `options_rejections` for the day, then walk the
live chain through each `build_spread` filter to find which one zeroes out. The
per-expiry funnel showed valid long/short pairs producing debits of $79–$118, all below
the $300 floor.

## Fix applied
`src/config.py`: `DEBIT_MIN` 300.0 → **80.0** (added explanatory comment).
The validator's debit check (`validator.py` #7) reads the same constant, so the builder
and the guardrail stayed consistent — no second edit needed.

### Verification (live chain, end-to-end through validator)
| Underlying | Spread | Exp | Debit/ct | Qty | Total | Validator |
|---|---|---|---|---|---|---|
| SPY | 755/757 | 2026-06-05 | $112 | 22 | $2,464 | OK |
| QQQ | 736/738 | 2026-06-05 | $91 | 27 | $2,457 | OK |

Each sizes to ~5% of equity (`TARGET_PCT_OF_EQUITY_PER_POSITION`), well under the 10%
hard cap and available buying power.

## Current state
- Working tree has **uncommitted** changes in `src/config.py`, `src/main.py`,
  `src/strategy.py` (the latter two are the equity-aware position-sizing work that was
  already in progress; `_size_qty` targets 5% of equity).
- `DRY_RUN = False` (live paper trading). The scheduled task runs the working tree, so
  the fix is already live for the next run.

## Next steps / what to watch
1. **Next trading session (entry window 10:00–15:00 ET):** when a signal fires *and* the
   Claude gatekeeper approves, an order should submit. Confirm via `options_orders` in
   Supabase or `logs/options_bot.log` (look for `action=entry`).
2. **Commit when ready** — not yet committed; commits are user-driven.
3. **Known trade-off:** $2-wide spreads on $1 strikes have modest risk/reward
   (~$112 risk to make ~$88). Fine to run; if you later want fewer/wider/higher-conviction
   spreads, raise `SHORT_STRIKES_AWAY_MIN/MAX` (~5–7) and the debit floor together —
   but check far-OTM short-leg OI ≥ 500 first, as it thins out.

## Files touched this session
- `src/config.py` — `DEBIT_MIN` 300 → 80 (+ comment)
- `HANDOFF.md` — this document
- memory: `debit-floor-vs-strike-width.md` + `MEMORY.md` index entry
