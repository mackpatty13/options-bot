-- Options bot tables. Run this once in the Supabase SQL Editor.
-- Prefix `options_` avoids collision with other projects sharing this Supabase instance.

CREATE TABLE IF NOT EXISTS options_cycles (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_equity NUMERIC,
    buying_power NUMERIC,
    daily_pnl NUMERIC,
    positions_open INTEGER,
    action_taken TEXT CHECK (action_taken IN ('entry','exit','hold','halt','no_signal','error')),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_options_cycles_ts ON options_cycles(timestamp DESC);

CREATE TABLE IF NOT EXISTS options_signals (
    id BIGSERIAL PRIMARY KEY,
    cycle_id BIGINT REFERENCES options_cycles(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    underlying TEXT NOT NULL,
    signal_type TEXT,           -- "bull" / "bear" / "none"
    signal_fired BOOLEAN NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_options_signals_cycle ON options_signals(cycle_id);

CREATE TABLE IF NOT EXISTS options_orders (
    id BIGSERIAL PRIMARY KEY,
    cycle_id BIGINT REFERENCES options_cycles(id) ON DELETE SET NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alpaca_order_id TEXT,
    underlying TEXT NOT NULL,
    spread_type TEXT,           -- "bull_call" / "bear_put"
    long_strike NUMERIC,
    short_strike NUMERIC,
    expiration DATE,
    debit_paid NUMERIC,
    status TEXT,                -- "submitted" / "filled" / "canceled" / "rejected" / "closed"
    filled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_options_orders_status ON options_orders(status);

CREATE TABLE IF NOT EXISTS options_rejections (
    id BIGSERIAL PRIMARY KEY,
    cycle_id BIGINT REFERENCES options_cycles(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempted_action TEXT,
    rejection_reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_options_rejections_ts ON options_rejections(timestamp DESC);

CREATE TABLE IF NOT EXISTS options_positions (
    id BIGSERIAL PRIMARY KEY,
    cycle_id BIGINT REFERENCES options_cycles(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    underlying TEXT NOT NULL,
    debit_paid NUMERIC,
    current_value NUMERIC,
    unrealized_pnl NUMERIC,
    dte_remaining INTEGER
);
CREATE INDEX IF NOT EXISTS ix_options_positions_cycle ON options_positions(cycle_id);
