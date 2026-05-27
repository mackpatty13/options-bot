# Options Bot Trading Rules

This document defines the rules a ~$50,000 paper-account options bot operates under. It is read by the bot every cycle and provided to the Claude decision step as context. **Claude is a gatekeeper, not a signal source.** Indicators and signals are computed deterministically in Python; Claude only chooses among `enter`, `exit`, or `hold` given the situation. Every limit named in these rules is also enforced in code by `validate_order()`.

## 1. Universe

1.1. Underlyings traded: **SPY and QQQ only.** No other tickers.
1.2. Instruments: **vertical debit spreads only.** No naked options, credit spreads, iron condors, or single-leg.
1.3. Directional bias: a *bull call spread* (long lower call, short higher call) when bullish; a *bear put spread* (long higher put, short lower put) when bearish.

## 2. Hours of Operation

2.1. Entry window: **10:00 AM - 3:00 PM ET.** No new positions before 10:00 (skip opening volatility) or after 3:00 (skip closing volatility).
2.2. Management window: positions are evaluated for exit every cycle, including outside the entry window.
2.3. Forced EOD close: any open position is closed if cycle time is **after 3:45 PM ET.**

## 3. Entry Signal

3.1. Compute on each cycle, for both SPY and QQQ:
    - 1-minute trend: price above both 9 EMA and 21 EMA -> bullish; below both -> bearish.
    - 5-minute trend: price above 9 EMA and 21 EMA AND 9 EMA > 21 EMA -> bullish; symmetric for bearish.
3.2. Bull signal fires when **both 1-min and 5-min are bullish.**
3.3. Bear signal fires when **both 1-min and 5-min are bearish.**
3.4. No new position is opened in an underlying where a position already exists.

## 4. Spread Construction

4.1. Expiration: **7-14 calendar days to expiry (DTE).** Prefer 7-10.
4.2. Long leg delta: **0.40 to 0.55** (absolute value).
4.3. Short leg: **2 to 3 strikes further OTM** than the long leg.
4.4. Both legs must have **open interest greater than 500.**
4.5. Both legs must have **bid-ask spread less than 8% of mid.**
4.6. Total debit (net premium paid): **between $300 and $2,000.**

## 5. Exits

5.1. Profit target: close at **+30% of debit paid.**
5.2. Stop loss: close at **-50% of debit paid.**
5.3. Time stop: close at **1 DTE** regardless of P/L.
5.4. EOD stop: close any open position **after 3:45 PM ET.**

## 6. Account Limits (enforced by validate_order)

6.1. Account equity must be **at least $20,000.**
6.2. Buying power must cover the debit at order time and be **at least $2,000.**
6.3. Maximum **10 concurrent positions** across the whole account.
6.4. Maximum **10 trade entries per day** (configured via `DAILY_TRADE_CAP`). Account is above the $25K PDT threshold, so this is a sanity throttle, not a regulatory cap.
6.5. Daily loss halt at **-$2,000** realized P/L. No new entries below this threshold. Existing positions are still managed normally.
6.6. Position size must be **less than 10% of account equity.**
6.7. **Limit orders only**, never market orders.

## 7. Decision Protocol for Claude

7.1. Each cycle, Claude receives: account state, today's signals, current open positions, and these rules.
7.2. Claude returns exactly one JSON object: `{"action": "enter", "underlying": "SPY|QQQ", "direction": "bull|bear"}`, `{"action": "exit", "position_id": "..."}`, or `{"action": "hold"}`.
7.3. Claude is asked to decide whether acting on a fired signal is appropriate given full context. Claude does not invent trades that no signal supports.
7.4. The decision is then passed to `validate_order()`. If validation fails, the trade is **not executed** and a row is written to `options_rejections`.

## 8. Logging Discipline

8.1. Every cycle writes one row to `options_cycles` with `action_taken`.
8.2. Every signal evaluation writes to `options_signals`.
8.3. Every submitted order writes to `options_orders`.
8.4. Every blocked order writes to `options_rejections`.
8.5. Open position snapshots are written to `options_positions` each cycle.
8.6. The `options_rejections` table is the **first place to look** when the bot is not trading.
