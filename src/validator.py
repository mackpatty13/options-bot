"""Hard guardrails. Every trade routes through validate_order() BEFORE it hits Alpaca.

Claude is NOT trusted to enforce these limits. The validator is the last line of defense.
Every check here is also documented in TRADING_RULES.md so Claude sees them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import config


@dataclass
class OrderProposal:
    """Concrete-enough representation of an order to validate it."""
    action: str                       # "enter" or "exit"
    underlying: str
    spread_type: str                  # "bull_call" / "bear_put"
    long_strike: float
    short_strike: float
    expiration: date
    debit: float                      # dollars (per contract)
    qty: int
    long_delta: float                 # |delta| of long leg
    long_open_interest: int
    short_open_interest: int
    long_bid_ask_pct: float           # 0-1
    short_bid_ask_pct: float          # 0-1
    order_type: str = "limit"         # "limit" only


@dataclass
class AccountSnapshot:
    equity: float
    buying_power: float
    daily_realized_pnl: float
    open_positions: int
    todays_entry_count: int


def validate_order(
    order: OrderProposal,
    account: AccountSnapshot,
    today: Optional[date] = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). reason is a human-readable explanation regardless of outcome."""
    today = today or date.today()

    # 1. Equity floor.
    if account.equity < config.MIN_EQUITY:
        return False, f"equity ${account.equity:.2f} < ${config.MIN_EQUITY:.0f} floor"

    # 2. Buying power must cover the debit.
    total_debit = order.debit * order.qty
    if order.action == "enter" and account.buying_power < total_debit:
        return False, f"buying power ${account.buying_power:.2f} < debit ${total_debit:.2f}"

    # 3. Concurrent position cap.
    if order.action == "enter" and account.open_positions >= config.MAX_CONCURRENT_POSITIONS:
        return False, (
            f"already {account.open_positions} open positions "
            f">= cap {config.MAX_CONCURRENT_POSITIONS}"
        )

    # 4. Daily entry cap.
    if order.action == "enter" and account.todays_entry_count >= config.DAILY_TRADE_CAP:
        return False, (
            f"today's entries {account.todays_entry_count} >= "
            f"daily cap {config.DAILY_TRADE_CAP}"
        )

    # 5. Daily loss halt.
    if order.action == "enter" and account.daily_realized_pnl <= config.DAILY_LOSS_HALT:
        return False, (
            f"daily P/L ${account.daily_realized_pnl:.2f} "
            f"<= halt ${config.DAILY_LOSS_HALT:.2f}; no new entries"
        )

    # 6. Position size as fraction of equity.
    if order.action == "enter":
        max_position_dollars = account.equity * config.MAX_PCT_OF_EQUITY_PER_POSITION
        if total_debit > max_position_dollars:
            return False, (
                f"position ${total_debit:.2f} > "
                f"{config.MAX_PCT_OF_EQUITY_PER_POSITION*100:.0f}% of equity "
                f"(${max_position_dollars:.2f})"
            )

    # 7. Debit range.
    if order.action == "enter":
        if order.debit < config.DEBIT_MIN:
            return False, f"debit ${order.debit:.2f} < min ${config.DEBIT_MIN:.0f}"
        if order.debit > config.DEBIT_MAX:
            return False, f"debit ${order.debit:.2f} > max ${config.DEBIT_MAX:.0f}"

    # 8. Open interest gate.
    if order.action == "enter":
        if order.long_open_interest < config.MIN_OPEN_INTEREST:
            return False, (
                f"long OI {order.long_open_interest} < min {config.MIN_OPEN_INTEREST}"
            )
        if order.short_open_interest < config.MIN_OPEN_INTEREST:
            return False, (
                f"short OI {order.short_open_interest} < min {config.MIN_OPEN_INTEREST}"
            )

    # 9. Bid-ask spread quality.
    if order.action == "enter":
        if order.long_bid_ask_pct > config.MAX_BID_ASK_PCT_OF_MID:
            return False, (
                f"long bid-ask {order.long_bid_ask_pct*100:.1f}% > "
                f"max {config.MAX_BID_ASK_PCT_OF_MID*100:.1f}%"
            )
        if order.short_bid_ask_pct > config.MAX_BID_ASK_PCT_OF_MID:
            return False, (
                f"short bid-ask {order.short_bid_ask_pct*100:.1f}% > "
                f"max {config.MAX_BID_ASK_PCT_OF_MID*100:.1f}%"
            )

    # 10. Limit orders only.
    if order.order_type != "limit":
        return False, f"order_type '{order.order_type}' is not 'limit'; market orders forbidden"

    # 11. DTE window.
    if order.action == "enter":
        dte = (order.expiration - today).days
        if dte < config.DTE_MIN or dte > config.DTE_MAX:
            return False, (
                f"DTE {dte} not in [{config.DTE_MIN}, {config.DTE_MAX}]"
            )

    # 12. Long-leg delta band.
    if order.action == "enter":
        if order.long_delta < config.LONG_DELTA_MIN or order.long_delta > config.LONG_DELTA_MAX:
            return False, (
                f"long delta {order.long_delta:.2f} not in "
                f"[{config.LONG_DELTA_MIN:.2f}, {config.LONG_DELTA_MAX:.2f}]"
            )

    return True, "ok"
