"""All Alpaca API calls live here. No other module imports alpaca-py directly."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from . import config

log = logging.getLogger(__name__)


@dataclass
class AccountState:
    equity: float
    buying_power: float
    cash: float
    daily_pnl: float           # equity - last_equity (today's intraday change)


@dataclass
class OpenPosition:
    symbol: str                # option symbol of the LONG leg (identifier we track)
    underlying: str
    qty: int                   # positive for long spreads
    avg_entry_price: float     # per-contract debit paid (in dollars, not cents)
    current_value: float       # mark-to-market value of the spread
    unrealized_pnl: float
    expiration: date
    long_strike: float
    short_strike: float
    spread_type: str           # "bull_call" or "bear_put"


@dataclass
class OptionLeg:
    symbol: str
    underlying: str
    expiration: date
    strike: float
    right: str                 # "C" or "P"
    bid: float
    ask: float
    mid: float
    delta: float
    open_interest: int

    @property
    def bid_ask_pct(self) -> float:
        if self.mid <= 0:
            return 1.0
        return (self.ask - self.bid) / self.mid


class AlpacaClient:
    """Thin wrapper around alpaca-py for trading, options data, and stock bars."""

    def __init__(self) -> None:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY must be set")
        self._trading = TradingClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=config.ALPACA_PAPER
        )
        self._stock_data = StockHistoricalDataClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        )
        self._option_data = OptionHistoricalDataClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        )

    # --- account ---

    def account_state(self) -> AccountState:
        acct = self._trading.get_account()
        equity = float(acct.equity)
        last_equity = float(acct.last_equity)
        return AccountState(
            equity=equity,
            buying_power=float(acct.buying_power),
            cash=float(acct.cash),
            daily_pnl=equity - last_equity,
        )

    # --- positions ---

    def open_spread_positions(self) -> list[OpenPosition]:
        """Pair option legs from alpaca positions into spreads. Same-expiry, same-right pairs."""
        positions = self._trading.get_all_positions()
        # Group option positions by underlying and expiry
        legs: dict[tuple[str, str, str], list] = {}
        for p in positions:
            if getattr(p, "asset_class", "").lower() != "us_option":
                continue
            underlying = getattr(p, "symbol_underlying", None) or _parse_underlying(p.symbol)
            exp = _parse_expiry_from_occ(p.symbol)
            right = _parse_right_from_occ(p.symbol)
            legs.setdefault((underlying, exp.isoformat(), right), []).append(p)

        spreads: list[OpenPosition] = []
        for (und, exp_iso, right), group in legs.items():
            if len(group) != 2:
                continue
            longs = [p for p in group if float(p.qty) > 0]
            shorts = [p for p in group if float(p.qty) < 0]
            if len(longs) != 1 or len(shorts) != 1:
                continue
            long_leg, short_leg = longs[0], shorts[0]
            long_strike = _parse_strike_from_occ(long_leg.symbol)
            short_strike = _parse_strike_from_occ(short_leg.symbol)
            qty = int(abs(float(long_leg.qty)))
            entry_debit = (
                float(long_leg.avg_entry_price) - float(short_leg.avg_entry_price)
            )
            market_value = float(long_leg.market_value) + float(short_leg.market_value)
            unrealized = (
                float(long_leg.unrealized_pl) + float(short_leg.unrealized_pl)
            )
            spread_type = "bull_call" if right == "C" else "bear_put"
            spreads.append(OpenPosition(
                symbol=long_leg.symbol,
                underlying=und,
                qty=qty,
                avg_entry_price=entry_debit,
                current_value=market_value,
                unrealized_pnl=unrealized,
                expiration=date.fromisoformat(exp_iso),
                long_strike=long_strike,
                short_strike=short_strike,
                spread_type=spread_type,
            ))
        return spreads

    # --- stock bars (for the entry signal) ---

    def recent_bars(self, ticker: str, interval_minutes: int, lookback_bars: int) -> pd.DataFrame:
        """Fetch the most recent `lookback_bars` of `interval_minutes`-min bars for `ticker`.

        Uses the IEX feed: free tier of Alpaca paper accounts cannot query SIP data.
        IEX coverage is limited to the IEX exchange (~3% of volume) but is sufficient
        for trend-following signals on liquid ETFs like SPY/QQQ. Upgrade to Algo Trader
        Plus ($99/mo) to switch to feed=DataFeed.SIP for full consolidated tape.
        """
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if interval_minutes == 1:
            tf = TimeFrame(1, TimeFrameUnit.Minute)
        elif interval_minutes == 5:
            tf = TimeFrame(5, TimeFrameUnit.Minute)
        else:
            tf = TimeFrame(interval_minutes, TimeFrameUnit.Minute)

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        req = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=tf,
            start=start, end=end, feed=DataFeed.IEX,
        )
        resp = self._stock_data.get_stock_bars(req)
        df = resp.df
        if df is None or df.empty:
            return pd.DataFrame()
        if "symbol" in df.index.names:
            df = df.xs(ticker, level="symbol")
        return df.tail(lookback_bars + 50)

    # --- option chain ---

    def option_chain_with_greeks(
        self,
        underlying: str,
        right: str,                # "C" or "P"
        dte_min: int,
        dte_max: int,
    ) -> list[OptionLeg]:
        """Pull the chain for the requested side, joining snapshots (quotes+greeks) with
        trading-API contracts (open_interest). Alpaca's snapshot endpoint omits OI, so
        we overlay it from the trading API's option contracts endpoint."""
        from alpaca.data.requests import OptionChainRequest
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.trading.requests import GetOptionContractsRequest

        today = date.today()
        exp_lo = today + timedelta(days=dte_min)
        exp_hi = today + timedelta(days=dte_max)

        # 1. OI from the trading API (reference data; no market-data subscription needed).
        oi_req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date_gte=exp_lo,
            expiration_date_lte=exp_hi,
            type=ContractType.CALL if right == "C" else ContractType.PUT,
            status=AssetStatus.ACTIVE,
            limit=10000,
        )
        contracts_resp = self._trading.get_option_contracts(oi_req)
        oi_by_symbol = {
            c.symbol: int(c.open_interest or 0)
            for c in contracts_resp.option_contracts
        }

        # 2. Quotes + greeks from the data API snapshot endpoint.
        snap_req = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=exp_lo,
            expiration_date_lte=exp_hi,
            type="call" if right == "C" else "put",
        )
        snaps = self._option_data.get_option_chain(snap_req)

        # 3. Merge.
        legs: list[OptionLeg] = []
        for sym, snap in snaps.items():
            quote = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            if quote is None or greeks is None:
                continue
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0
            delta = float(getattr(greeks, "delta", 0) or 0)
            oi = oi_by_symbol.get(sym, 0)
            legs.append(OptionLeg(
                symbol=sym,
                underlying=underlying,
                expiration=_parse_expiry_from_occ(sym),
                strike=_parse_strike_from_occ(sym),
                right=right,
                bid=bid,
                ask=ask,
                mid=mid,
                delta=delta,
                open_interest=oi,
            ))
        legs.sort(key=lambda x: (x.expiration, x.strike))
        return legs

    # --- order submission ---

    def submit_spread_open(
        self,
        long_leg: OptionLeg,
        short_leg: OptionLeg,
        qty: int,
        debit_limit: float,
    ) -> str:
        """Open a vertical debit spread via a multi-leg limit order. Returns alpaca order id."""
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        legs = [
            OptionLegRequest(symbol=long_leg.symbol,
                             side=OrderSide.BUY, ratio_qty=1),
            OptionLegRequest(symbol=short_leg.symbol,
                             side=OrderSide.SELL, ratio_qty=1),
        ]
        req = LimitOrderRequest(
            qty=qty,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            type=OrderType.LIMIT,
            limit_price=round(debit_limit, 2),
            legs=legs,
        )
        resp = self._trading.submit_order(order_data=req)
        return str(resp.id)

    def submit_spread_close(self, position: OpenPosition, credit_limit: float) -> str:
        """Close an existing spread by reversing both legs. Returns alpaca order id."""
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        # The OpenPosition.symbol field holds the long leg; we need the short leg too.
        # Rebuild OCC symbols for both legs from the stored data.
        long_symbol = position.symbol
        short_symbol = _swap_strike_in_occ(long_symbol, position.short_strike)

        legs = [
            OptionLegRequest(symbol=long_symbol, side=OrderSide.SELL, ratio_qty=1),
            OptionLegRequest(symbol=short_symbol, side=OrderSide.BUY, ratio_qty=1),
        ]
        req = LimitOrderRequest(
            qty=position.qty,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            type=OrderType.LIMIT,
            limit_price=round(credit_limit, 2),
            legs=legs,
        )
        resp = self._trading.submit_order(order_data=req)
        return str(resp.id)


# --- OCC symbol helpers ---
# OCC format: <ROOT><YYMMDD><C|P><STRIKE*1000 padded to 8>
# The OCC standard pads ROOT to 6 chars with spaces, but Alpaca returns it unpadded
# (e.g. "SPY240614C00500000"). Parse from the right to handle both forms.

def _parse_strike_from_occ(occ: str) -> float:
    return int(occ[-8:]) / 1000.0


def _parse_right_from_occ(occ: str) -> str:
    return occ[-9]


def _parse_expiry_from_occ(occ: str) -> date:
    d = occ[-15:-9]
    return date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))


def _parse_underlying(occ: str) -> str:
    return occ[:-15].strip()


def _swap_strike_in_occ(occ: str, new_strike: float) -> str:
    """Replace the strike portion of an OCC symbol with a new strike."""
    return occ[:-8] + f"{int(round(new_strike * 1000)):08d}"
