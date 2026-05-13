"""All Supabase writes. Every cycle goes through here; no other module touches Supabase."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from supabase import Client, create_client

from . import config

log = logging.getLogger(__name__)


class SupabaseLogger:
    """Wraps the supabase-py client with bot-specific helpers."""

    def __init__(self) -> None:
        if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY must be set")
        self.client: Client = create_client(
            config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY
        )

    # --- writes ---

    def log_cycle(
        self,
        *,
        account_equity: float,
        buying_power: float,
        daily_pnl: float,
        positions_open: int,
        action_taken: str,
        notes: str = "",
    ) -> int:
        resp = self.client.table("options_cycles").insert({
            "account_equity": account_equity,
            "buying_power": buying_power,
            "daily_pnl": daily_pnl,
            "positions_open": positions_open,
            "action_taken": action_taken,
            "notes": notes,
        }).execute()
        return int(resp.data[0]["id"])

    def log_signal(
        self,
        *,
        cycle_id: int,
        underlying: str,
        signal_type: str,
        signal_fired: bool,
        reason: str,
    ) -> None:
        self.client.table("options_signals").insert({
            "cycle_id": cycle_id,
            "underlying": underlying,
            "signal_type": signal_type,
            "signal_fired": signal_fired,
            "reason": reason,
        }).execute()

    def log_order(
        self,
        *,
        cycle_id: int,
        alpaca_order_id: str,
        underlying: str,
        spread_type: str,
        long_strike: float,
        short_strike: float,
        expiration: date,
        debit_paid: float,
        status: str,
        filled_at: Optional[datetime] = None,
    ) -> None:
        self.client.table("options_orders").insert({
            "cycle_id": cycle_id,
            "alpaca_order_id": alpaca_order_id,
            "underlying": underlying,
            "spread_type": spread_type,
            "long_strike": long_strike,
            "short_strike": short_strike,
            "expiration": expiration.isoformat(),
            "debit_paid": debit_paid,
            "status": status,
            "filled_at": filled_at.isoformat() if filled_at else None,
        }).execute()

    def log_rejection(
        self,
        *,
        cycle_id: int,
        attempted_action: str,
        rejection_reason: str,
    ) -> None:
        self.client.table("options_rejections").insert({
            "cycle_id": cycle_id,
            "attempted_action": attempted_action,
            "rejection_reason": rejection_reason,
        }).execute()

    def log_position(
        self,
        *,
        cycle_id: int,
        underlying: str,
        debit_paid: float,
        current_value: float,
        unrealized_pnl: float,
        dte_remaining: int,
    ) -> None:
        self.client.table("options_positions").insert({
            "cycle_id": cycle_id,
            "underlying": underlying,
            "debit_paid": debit_paid,
            "current_value": current_value,
            "unrealized_pnl": unrealized_pnl,
            "dte_remaining": dte_remaining,
        }).execute()

    # --- reads (for daily caps) ---

    def todays_entries(self) -> int:
        """Count of orders submitted today, used for the daily entry cap."""
        today_iso = datetime.now(timezone.utc).date().isoformat()
        resp = (
            self.client.table("options_orders")
            .select("id", count="exact")
            .gte("timestamp", today_iso)
            .execute()
        )
        return int(resp.count or 0)

    def todays_realized_pnl(self) -> float:
        """Sum of realized P/L from positions closed today.

        Realized P/L is the difference between the closing order's net credit and the
        opening order's debit, both stored on options_orders rows. For the paper-phase
        bot we approximate this by summing debit_paid on rows with status='closed' today
        (closes log a negative debit_paid representing the credit received).
        """
        today_iso = datetime.now(timezone.utc).date().isoformat()
        resp = (
            self.client.table("options_orders")
            .select("debit_paid,status")
            .gte("timestamp", today_iso)
            .execute()
        )
        # Pair opens with their corresponding closes by underlying+date is post-MVP;
        # for now, sum the closes' net effect. The cycle row also records daily_pnl
        # which is the authoritative number queried from Alpaca each cycle.
        total = 0.0
        for row in resp.data or []:
            if row.get("status") == "closed":
                total += float(row.get("debit_paid") or 0)
        return total
