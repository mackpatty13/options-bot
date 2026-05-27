"""Per-rule failure-case tests for validate_order.

Each test holds every input constant except the field we're checking — so if a test
fails after a code change, we know exactly which rule regressed.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import config
from src.validator import AccountSnapshot, OrderProposal, validate_order


TODAY = date(2026, 5, 12)


def _ok_order(**overrides) -> OrderProposal:
    base = dict(
        action="enter",
        underlying="SPY",
        spread_type="bull_call",
        long_strike=500.0,
        short_strike=503.0,
        expiration=TODAY + timedelta(days=10),
        debit=1000.0,
        qty=1,
        long_delta=0.50,
        long_open_interest=2000,
        short_open_interest=1500,
        long_bid_ask_pct=0.04,
        short_bid_ask_pct=0.05,
        order_type="limit",
    )
    base.update(overrides)
    return OrderProposal(**base)


def _ok_account(**overrides) -> AccountSnapshot:
    base = dict(
        equity=50000.0,
        buying_power=40000.0,
        daily_realized_pnl=0.0,
        open_positions=0,
        todays_entry_count=0,
    )
    base.update(overrides)
    return AccountSnapshot(**base)


def test_happy_path_allows():
    ok, reason = validate_order(_ok_order(), _ok_account(), today=TODAY)
    assert ok, reason


def test_rule_1_equity_floor_blocks():
    ok, reason = validate_order(_ok_order(), _ok_account(equity=15000.0), today=TODAY)
    assert not ok and "equity" in reason


def test_rule_2_buying_power_blocks():
    ok, reason = validate_order(_ok_order(debit=1500.0, qty=2),
                                 _ok_account(buying_power=2500.0), today=TODAY)
    assert not ok and "buying power" in reason


def test_rule_3_concurrent_positions_blocks():
    ok, reason = validate_order(_ok_order(),
                                 _ok_account(open_positions=config.MAX_CONCURRENT_POSITIONS),
                                 today=TODAY)
    assert not ok and "open positions" in reason


def test_rule_4_daily_entry_cap_blocks():
    ok, reason = validate_order(_ok_order(),
                                 _ok_account(todays_entry_count=config.DAILY_TRADE_CAP),
                                 today=TODAY)
    assert not ok and "daily cap" in reason


def test_rule_5_loss_halt_blocks():
    ok, reason = validate_order(_ok_order(),
                                 _ok_account(daily_realized_pnl=config.DAILY_LOSS_HALT),
                                 today=TODAY)
    assert not ok and "halt" in reason


def test_rule_6_position_size_blocks():
    # 10% of $50K = $5,000; qty=6 * debit=$1,000 = $6,000 > $5,000
    ok, reason = validate_order(_ok_order(qty=6), _ok_account(), today=TODAY)
    assert not ok and "% of equity" in reason


def test_rule_7_debit_too_low_blocks():
    ok, reason = validate_order(_ok_order(debit=200.0), _ok_account(), today=TODAY)
    assert not ok and "min" in reason


def test_rule_7_debit_too_high_blocks():
    # Equity large enough that rule 6 (position size) doesn't fire first.
    ok, reason = validate_order(_ok_order(debit=2500.0),
                                 _ok_account(equity=200000.0, buying_power=100000.0),
                                 today=TODAY)
    assert not ok and "max" in reason


def test_rule_8_long_oi_blocks():
    ok, reason = validate_order(_ok_order(long_open_interest=100),
                                 _ok_account(), today=TODAY)
    assert not ok and "long OI" in reason


def test_rule_8_short_oi_blocks():
    ok, reason = validate_order(_ok_order(short_open_interest=100),
                                 _ok_account(), today=TODAY)
    assert not ok and "short OI" in reason


def test_rule_9_long_bid_ask_blocks():
    ok, reason = validate_order(_ok_order(long_bid_ask_pct=0.20),
                                 _ok_account(), today=TODAY)
    assert not ok and "long bid-ask" in reason


def test_rule_9_short_bid_ask_blocks():
    ok, reason = validate_order(_ok_order(short_bid_ask_pct=0.20),
                                 _ok_account(), today=TODAY)
    assert not ok and "short bid-ask" in reason


def test_rule_10_market_order_blocks():
    ok, reason = validate_order(_ok_order(order_type="market"),
                                 _ok_account(), today=TODAY)
    assert not ok and "limit" in reason


def test_rule_11_dte_too_low_blocks():
    ok, reason = validate_order(_ok_order(expiration=TODAY + timedelta(days=3)),
                                 _ok_account(), today=TODAY)
    assert not ok and "DTE" in reason


def test_rule_11_dte_too_high_blocks():
    ok, reason = validate_order(_ok_order(expiration=TODAY + timedelta(days=20)),
                                 _ok_account(), today=TODAY)
    assert not ok and "DTE" in reason


def test_rule_12_delta_too_low_blocks():
    ok, reason = validate_order(_ok_order(long_delta=0.30),
                                 _ok_account(), today=TODAY)
    assert not ok and "long delta" in reason


def test_rule_12_delta_too_high_blocks():
    ok, reason = validate_order(_ok_order(long_delta=0.65),
                                 _ok_account(), today=TODAY)
    assert not ok and "long delta" in reason


def test_exit_action_skips_entry_only_rules():
    """Exits must not be blocked by entry-only rules (debit range, OI, BB pct, DTE, delta)."""
    proposal = _ok_order(action="exit", debit=10.0, long_open_interest=0,
                         long_bid_ask_pct=0.50, expiration=TODAY + timedelta(days=1),
                         long_delta=0.10)
    ok, reason = validate_order(proposal, _ok_account(), today=TODAY)
    assert ok, reason
