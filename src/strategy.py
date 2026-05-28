"""Signal detection (1m + 5m trend agreement) and spread construction."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from . import config
from .alpaca_client import AlpacaClient, OptionLeg

log = logging.getLogger(__name__)


@dataclass
class Signal:
    underlying: str
    signal_type: str           # "bull" / "bear" / "none"
    signal_fired: bool
    reason: str


@dataclass
class ProposedSpread:
    underlying: str
    spread_type: str           # "bull_call" / "bear_put"
    long_leg: OptionLeg
    short_leg: OptionLeg
    debit_per_contract: float  # dollars (not per share)
    qty: int


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def compute_signal(underlying: str, alpaca: AlpacaClient) -> Signal:
    """Bull if 1m AND 5m both above their 9 & 21 EMAs (with 5m 9>21). Bear is symmetric."""
    bars_1m = alpaca.recent_bars(underlying, interval_minutes=1, lookback_bars=60)
    bars_5m = alpaca.recent_bars(underlying, interval_minutes=5, lookback_bars=60)

    if bars_1m.empty or bars_5m.empty or len(bars_5m) < 25:
        return Signal(underlying, "none", False, "insufficient bars")

    c1 = bars_1m["close"]
    c5 = bars_5m["close"]
    ema9_1 = _ema(c1, 9).iloc[-1]
    ema21_1 = _ema(c1, 21).iloc[-1]
    ema9_5 = _ema(c5, 9).iloc[-1]
    ema21_5 = _ema(c5, 21).iloc[-1]
    px_1 = float(c1.iloc[-1])
    px_5 = float(c5.iloc[-1])

    bull_1 = px_1 > ema9_1 and px_1 > ema21_1
    bull_5 = px_5 > ema9_5 and px_5 > ema21_5 and ema9_5 > ema21_5
    bear_1 = px_1 < ema9_1 and px_1 < ema21_1
    bear_5 = px_5 < ema9_5 and px_5 < ema21_5 and ema9_5 < ema21_5

    if bull_1 and bull_5:
        return Signal(underlying, "bull", True,
                      f"1m {px_1:.2f}>EMAs; 5m {px_5:.2f}>EMAs; 5m 9({ema9_5:.2f})>21({ema21_5:.2f})")
    if bear_1 and bear_5:
        return Signal(underlying, "bear", True,
                      f"1m {px_1:.2f}<EMAs; 5m {px_5:.2f}<EMAs; 5m 9({ema9_5:.2f})<21({ema21_5:.2f})")
    return Signal(underlying, "none", False,
                  f"no agreement (bull 1m={bull_1} 5m={bull_5}; bear 1m={bear_1} 5m={bear_5})")


def build_spread(
    underlying: str,
    direction: str,             # "bull" or "bear"
    alpaca: AlpacaClient,
    equity: float,
    buying_power: float,
) -> Optional[ProposedSpread]:
    """Find a spread whose legs satisfy every rule. Returns None if nothing qualifies."""
    right = "C" if direction == "bull" else "P"
    chain = alpaca.option_chain_with_greeks(
        underlying, right, dte_min=config.DTE_MIN, dte_max=config.DTE_MAX
    )
    if not chain:
        return None

    # Prefer the nearest expiry that has a valid spread.
    by_expiry: dict = {}
    for leg in chain:
        by_expiry.setdefault(leg.expiration, []).append(leg)

    for exp in sorted(by_expiry.keys()):
        legs = sorted(by_expiry[exp], key=lambda x: x.strike)
        candidates = _candidate_long_legs(legs, direction)
        for long_leg in candidates:
            short_leg = _pick_short_leg(legs, long_leg, direction)
            if short_leg is None:
                continue
            if not _leg_quality_ok(long_leg) or not _leg_quality_ok(short_leg):
                continue
            debit = (long_leg.mid - short_leg.mid) * 100.0  # per contract, dollars
            if direction == "bear":
                debit = (long_leg.mid - short_leg.mid) * 100.0  # same shape; long is higher strike put
            if not (config.DEBIT_MIN <= debit <= config.DEBIT_MAX):
                continue
            spread_type = "bull_call" if direction == "bull" else "bear_put"
            qty = _size_qty(debit, equity, buying_power)
            return ProposedSpread(
                underlying=underlying,
                spread_type=spread_type,
                long_leg=long_leg,
                short_leg=short_leg,
                debit_per_contract=debit,
                qty=qty,
            )
    return None


def _size_qty(debit_per_contract: float, equity: float, buying_power: float) -> int:
    """Pick a contract count that targets TARGET_PCT of equity, never exceeds MAX_PCT
    of equity or available buying power, and is at least 1."""
    if debit_per_contract <= 0:
        return 1
    target_dollars = equity * config.TARGET_PCT_OF_EQUITY_PER_POSITION
    max_dollars = min(
        equity * config.MAX_PCT_OF_EQUITY_PER_POSITION,
        buying_power,
    )
    target_qty = max(1, round(target_dollars / debit_per_contract))
    max_qty = max(1, int(max_dollars / debit_per_contract))
    return min(target_qty, max_qty)


def _candidate_long_legs(legs_sorted_by_strike: list[OptionLeg], direction: str) -> list[OptionLeg]:
    """Long-leg candidates: |delta| in [LONG_DELTA_MIN, LONG_DELTA_MAX]."""
    out = []
    for leg in legs_sorted_by_strike:
        d = abs(leg.delta)
        if config.LONG_DELTA_MIN <= d <= config.LONG_DELTA_MAX:
            out.append(leg)
    # Closer-to-ATM first (highest |delta| within band).
    out.sort(key=lambda l: -abs(l.delta))
    return out


def _pick_short_leg(
    legs_sorted_by_strike: list[OptionLeg],
    long_leg: OptionLeg,
    direction: str,
) -> Optional[OptionLeg]:
    """Short leg: 2 or 3 strikes further OTM than the long leg (same expiry)."""
    same_exp = [l for l in legs_sorted_by_strike if l.expiration == long_leg.expiration]
    same_exp.sort(key=lambda l: l.strike)
    try:
        idx = next(i for i, l in enumerate(same_exp) if l.symbol == long_leg.symbol)
    except StopIteration:
        return None
    if direction == "bull":
        # OTM = higher strike for calls
        candidates = same_exp[idx + config.SHORT_STRIKES_AWAY_MIN : idx + config.SHORT_STRIKES_AWAY_MAX + 1]
    else:
        # OTM = lower strike for puts
        lo = max(0, idx - config.SHORT_STRIKES_AWAY_MAX)
        hi = max(0, idx - config.SHORT_STRIKES_AWAY_MIN + 1)
        candidates = list(reversed(same_exp[lo:hi]))
    # Prefer the closer one (fewer strikes away)
    return candidates[0] if candidates else None


def _leg_quality_ok(leg: OptionLeg) -> bool:
    if leg.open_interest < config.MIN_OPEN_INTEREST:
        return False
    if leg.bid_ask_pct > config.MAX_BID_ASK_PCT_OF_MID:
        return False
    return True
