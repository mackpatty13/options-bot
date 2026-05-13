"""Entry point: runs ONE decision cycle. GitHub Actions invokes `python -m src.main` on cron.

Cycle order:
  1. Snapshot account state (equity, BP, daily P/L, positions)
  2. Compute SPY/QQQ signals (deterministic, in Python)
  3. Manage existing positions: profit target / stop / time stop / EOD close
  4. If hours/state allow new entries, build a candidate spread for the strongest signal
  5. Ask Claude (gatekeeper) for the decision
  6. validate_order() — fail closed; rejections are logged
  7. Submit limit order if validator allowed
  8. Always write at least one row to options_cycles
"""
from __future__ import annotations

import logging
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

from . import config
from .alpaca_client import AlpacaClient, OpenPosition
from .claude_client import ClaudeClient
from .strategy import ProposedSpread, Signal, build_spread, compute_signal
from .supabase_logger import SupabaseLogger
from .validator import AccountSnapshot, OrderProposal, validate_order


log = logging.getLogger("options_bot")

ET = pytz.timezone("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def in_entry_window(now: datetime) -> bool:
    t = now.time()
    return config.ENTRY_WINDOW_START <= t <= config.ENTRY_WINDOW_END


def past_eod_flatten(now: datetime) -> bool:
    return now.time() >= config.EOD_FLATTEN_AFTER


# --- exit decisions ---

def position_should_exit(pos: OpenPosition, now: datetime) -> Optional[str]:
    """Return a reason string if the position needs to be closed, else None."""
    if past_eod_flatten(now):
        return "EOD flatten (>= 3:45 PM ET)"
    dte = (pos.expiration - now.date()).days
    if dte <= config.TIME_STOP_DTE:
        return f"time stop ({dte} DTE)"
    if pos.avg_entry_price <= 0:
        return None
    pnl_pct = pos.unrealized_pnl / (pos.avg_entry_price * pos.qty * 100.0)
    if pnl_pct >= config.PROFIT_TARGET_PCT:
        return f"profit target hit ({pnl_pct*100:.1f}% >= {config.PROFIT_TARGET_PCT*100:.0f}%)"
    if pnl_pct <= config.STOP_LOSS_PCT:
        return f"stop loss hit ({pnl_pct*100:.1f}% <= {config.STOP_LOSS_PCT*100:.0f}%)"
    return None


# --- cycle ---

def run_one_cycle() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("=== options-bot cycle start | dry_run=%s | daily_cap=%s ===",
             config.DRY_RUN, config.DAILY_TRADE_CAP)

    sb = SupabaseLogger()
    alpaca = AlpacaClient()

    # 1. Account state
    state = alpaca.account_state()
    positions = alpaca.open_spread_positions()
    todays_entries = sb.todays_entries()

    # Halt check
    halted = state.daily_pnl <= config.DAILY_LOSS_HALT

    now = now_et()
    log.info("account: equity=$%.2f bp=$%.2f dPL=$%.2f positions=%d entries_today=%d halted=%s",
             state.equity, state.buying_power, state.daily_pnl,
             len(positions), todays_entries, halted)

    # 2. Compute signals for each ticker (always; logged regardless of action)
    signals = [compute_signal(t, alpaca) for t in config.TICKERS]

    # 3. Build cycle row; updated with action_taken below
    cycle_id = sb.log_cycle(
        account_equity=state.equity,
        buying_power=state.buying_power,
        daily_pnl=state.daily_pnl,
        positions_open=len(positions),
        action_taken="hold",   # placeholder; updated at end
        notes="initial; will be updated",
    )
    for s in signals:
        sb.log_signal(cycle_id=cycle_id, underlying=s.underlying,
                      signal_type=s.signal_type, signal_fired=s.signal_fired,
                      reason=s.reason)
    for p in positions:
        dte = (p.expiration - now.date()).days
        sb.log_position(cycle_id=cycle_id, underlying=p.underlying,
                        debit_paid=p.avg_entry_price * 100.0,
                        current_value=p.current_value,
                        unrealized_pnl=p.unrealized_pnl,
                        dte_remaining=dte)

    # 4. Manage exits first.
    actions_taken: list[str] = []
    for pos in positions:
        reason = position_should_exit(pos, now)
        if reason is None:
            continue
        log.info("exit %s: %s", pos.symbol, reason)
        # Use a conservative limit: mid-1 cent for the credit (we want to get out).
        credit_limit = max(0.01, (pos.current_value / max(pos.qty, 1) / 100.0) - 0.01)
        if config.DRY_RUN:
            actions_taken.append(f"DRY exit {pos.underlying}: {reason}")
            continue
        try:
            order_id = alpaca.submit_spread_close(pos, credit_limit=credit_limit)
            sb.log_order(
                cycle_id=cycle_id,
                alpaca_order_id=order_id,
                underlying=pos.underlying,
                spread_type=pos.spread_type,
                long_strike=pos.long_strike,
                short_strike=pos.short_strike,
                expiration=pos.expiration,
                debit_paid=-credit_limit * 100.0,   # negative = credit received on close
                status="closed",
                filled_at=None,
            )
            actions_taken.append(f"exit {pos.underlying}: {reason}")
        except Exception as e:
            log.error("close failed for %s: %s", pos.symbol, e)
            sb.log_rejection(cycle_id=cycle_id,
                             attempted_action=f"exit {pos.underlying}",
                             rejection_reason=f"alpaca error: {e}")

    # 5. Entry path — only inside the entry window, not halted, not at caps.
    proposal: Optional[OrderProposal] = None
    proposed_spread: Optional[ProposedSpread] = None
    proposed_signal: Optional[Signal] = None

    if (in_entry_window(now) and not halted
            and len(positions) < config.MAX_CONCURRENT_POSITIONS
            and todays_entries < config.DAILY_TRADE_CAP):

        # Pick the underlying with a fired signal and no existing position.
        held = {p.underlying for p in positions}
        eligible = [s for s in signals if s.signal_fired and s.underlying not in held]
        # Prefer SPY over QQQ on ties (arbitrary but deterministic).
        eligible.sort(key=lambda s: (config.TICKERS.index(s.underlying)))

        for sig in eligible:
            spread = build_spread(sig.underlying, sig.signal_type, alpaca)
            if spread is None:
                sb.log_rejection(cycle_id=cycle_id,
                                 attempted_action=f"enter {sig.underlying} {sig.signal_type}",
                                 rejection_reason="no qualifying spread in chain")
                continue
            proposal = OrderProposal(
                action="enter",
                underlying=spread.underlying,
                spread_type=spread.spread_type,
                long_strike=spread.long_leg.strike,
                short_strike=spread.short_leg.strike,
                expiration=spread.long_leg.expiration,
                debit=spread.debit_per_contract,
                qty=spread.qty,
                long_delta=abs(spread.long_leg.delta),
                long_open_interest=spread.long_leg.open_interest,
                short_open_interest=spread.short_leg.open_interest,
                long_bid_ask_pct=spread.long_leg.bid_ask_pct,
                short_bid_ask_pct=spread.short_leg.bid_ask_pct,
                order_type="limit",
            )
            proposed_spread = spread
            proposed_signal = sig
            break

    # 6. Ask Claude.
    if proposal is not None:
        claude = ClaudeClient()
        decision = claude.decide(cycle_context={
            "now_et": now.isoformat(),
            "account": {
                "equity": state.equity,
                "buying_power": state.buying_power,
                "daily_pnl": state.daily_pnl,
                "open_positions": len(positions),
                "todays_entries": todays_entries,
                "daily_trade_cap": config.DAILY_TRADE_CAP,
            },
            "signals": [
                {"underlying": s.underlying, "type": s.signal_type,
                 "fired": s.signal_fired, "reason": s.reason}
                for s in signals
            ],
            "proposed_spread": {
                "underlying": proposal.underlying,
                "spread_type": proposal.spread_type,
                "long_strike": proposal.long_strike,
                "short_strike": proposal.short_strike,
                "expiration": proposal.expiration.isoformat(),
                "debit": proposal.debit,
                "long_delta": proposal.long_delta,
            },
            "open_positions": [
                {"symbol": p.symbol, "underlying": p.underlying,
                 "dte": (p.expiration - now.date()).days,
                 "unrealized_pnl": p.unrealized_pnl}
                for p in positions
            ],
        })
        log.info("claude decision: %s", decision)

        if decision.get("action") == "enter":
            account_snap = AccountSnapshot(
                equity=state.equity,
                buying_power=state.buying_power,
                daily_realized_pnl=state.daily_pnl,
                open_positions=len(positions),
                todays_entry_count=todays_entries,
            )
            allowed, reason = validate_order(proposal, account_snap, today=now.date())
            if not allowed:
                log.warning("validator BLOCKED: %s", reason)
                sb.log_rejection(
                    cycle_id=cycle_id,
                    attempted_action=f"enter {proposal.underlying} {proposal.spread_type}",
                    rejection_reason=reason,
                )
            else:
                if config.DRY_RUN:
                    log.info("DRY enter would submit: %s", proposal)
                    actions_taken.append(f"DRY enter {proposal.underlying} {proposal.spread_type}")
                else:
                    assert proposed_spread is not None
                    debit_limit = proposal.debit / 100.0  # per-share limit
                    try:
                        order_id = alpaca.submit_spread_open(
                            long_leg=proposed_spread.long_leg,
                            short_leg=proposed_spread.short_leg,
                            qty=proposal.qty,
                            debit_limit=debit_limit,
                        )
                        sb.log_order(
                            cycle_id=cycle_id,
                            alpaca_order_id=order_id,
                            underlying=proposal.underlying,
                            spread_type=proposal.spread_type,
                            long_strike=proposal.long_strike,
                            short_strike=proposal.short_strike,
                            expiration=proposal.expiration,
                            debit_paid=proposal.debit,
                            status="submitted",
                        )
                        actions_taken.append(f"enter {proposal.underlying} {proposal.spread_type}")
                    except Exception as e:
                        log.error("submit failed: %s", e)
                        sb.log_rejection(
                            cycle_id=cycle_id,
                            attempted_action=f"enter {proposal.underlying} {proposal.spread_type}",
                            rejection_reason=f"alpaca error: {e}")
        else:
            log.info("claude chose: %s", decision.get("action"))

    # 7. Update cycle row with final action.
    final_action = (
        "halt" if halted
        else "exit" if any(a.startswith("exit") or a.startswith("DRY exit") for a in actions_taken)
        else "entry" if any(a.startswith("enter") or a.startswith("DRY enter") for a in actions_taken)
        else "no_signal" if not any(s.signal_fired for s in signals)
        else "hold"
    )
    notes = "; ".join(actions_taken) if actions_taken else "no actions"
    sb.client.table("options_cycles").update({
        "action_taken": final_action,
        "notes": notes,
    }).eq("id", cycle_id).execute()
    log.info("cycle complete: action=%s notes=%s", final_action, notes)


if __name__ == "__main__":
    try:
        run_one_cycle()
    except Exception:
        log.exception("cycle crashed")
        sys.exit(1)
