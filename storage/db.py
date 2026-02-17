"""
SQLite storage for observations, signals, and paper trades.

Append-only design — we never update or delete records. Everything
is timestamped for post-hoc analysis of latency and edge viability.
"""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from data.metar_client import TemperatureReading
from engine.bracket_tracker import BracketCrossing, Confidence

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_icao TEXT NOT NULL,
    observation_time TEXT NOT NULL,     -- ISO 8601 UTC
    fetch_time TEXT NOT NULL,           -- When we received it
    temp_celsius REAL NOT NULL,
    temp_fahrenheit REAL NOT NULL,
    celsius_precision_tenths INTEGER NOT NULL,  -- 1=tenths, 0=whole
    raw_metar TEXT
);

CREATE TABLE IF NOT EXISTS bracket_crossings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    station_icao TEXT NOT NULL,
    observation_time TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    old_bracket_index INTEGER,          -- NULL for first reading
    old_bracket_label TEXT,
    new_bracket_index INTEGER NOT NULL,
    new_bracket_label TEXT NOT NULL,
    observed_temp_f REAL NOT NULL,
    daily_max_f REAL NOT NULL,
    confidence TEXT NOT NULL,           -- 'high' or 'low'
    latency_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crossing_id INTEGER NOT NULL,
    trade_time TEXT NOT NULL,
    city TEXT NOT NULL,
    bracket_index INTEGER NOT NULL,
    bracket_label TEXT NOT NULL,
    action TEXT NOT NULL,               -- 'BUY_YES' or 'SKIP'
    skip_reason TEXT,                   -- Why we skipped (if action=SKIP)
    market_yes_price_cents INTEGER,     -- Kalshi YES price at signal time
    market_no_price_cents INTEGER,
    market_volume INTEGER,
    position_size INTEGER,              -- Number of contracts
    entry_cost_cents INTEGER,           -- position_size * yes_price
    potential_profit_cents INTEGER,     -- position_size * (100 - yes_price)
    cash_before_cents INTEGER,          -- Portfolio cash before trade
    cash_after_cents INTEGER,           -- Portfolio cash after trade
    FOREIGN KEY (crossing_id) REFERENCES bracket_crossings(id)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time TEXT NOT NULL,
    cash_cents INTEGER NOT NULL,
    positions_value_cents INTEGER NOT NULL,
    total_capital_cents INTEGER NOT NULL,
    realized_pnl_cents INTEGER NOT NULL,
    total_trades INTEGER NOT NULL,
    winning_trades INTEGER NOT NULL,
    losing_trades INTEGER NOT NULL,
    open_positions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL,
    bracket_label TEXT NOT NULL,
    side TEXT NOT NULL,                 -- 'YES' or 'NO'
    entry_time TEXT NOT NULL,
    entry_price_cents INTEGER NOT NULL,
    contracts INTEGER NOT NULL,
    entry_cost_cents INTEGER NOT NULL,

    -- Exit fields (NULL while open)
    exit_time TEXT,
    exit_price_cents INTEGER,
    exit_proceeds_cents INTEGER,
    exit_reason TEXT,                   -- 'take_profit', 'stop_loss', 'market_close', 'settlement'
    realized_pnl_cents INTEGER,

    status TEXT NOT NULL DEFAULT 'open',  -- 'open' or 'closed'

    -- Portfolio state at entry
    portfolio_cash_before INTEGER,
    portfolio_capital_before INTEGER
);

CREATE TABLE IF NOT EXISTS trade_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_time TEXT NOT NULL,
    city TEXT NOT NULL,
    ticker TEXT NOT NULL,
    bracket_label TEXT NOT NULL,
    current_temp_f REAL NOT NULL,
    bracket_distance_f REAL NOT NULL,   -- Distance from current temp to bracket
    time_of_day_local TEXT NOT NULL,

    -- Market data
    yes_price_cents INTEGER NOT NULL,
    no_price_cents INTEGER NOT NULL,

    -- Decision
    action TEXT NOT NULL,               -- 'BUY_YES', 'BUY_NO', 'SKIP', 'HOLD'
    skip_reason TEXT,

    -- If action taken
    position_id INTEGER REFERENCES positions(id)
);

CREATE INDEX IF NOT EXISTS idx_obs_station_time
    ON observations(station_icao, observation_time);

CREATE INDEX IF NOT EXISTS idx_crossings_city_time
    ON bracket_crossings(city, signal_time);

CREATE INDEX IF NOT EXISTS idx_portfolio_time
    ON portfolio_snapshots(snapshot_time);

CREATE INDEX IF NOT EXISTS idx_positions_status
    ON positions(status);

CREATE INDEX IF NOT EXISTS idx_positions_ticker
    ON positions(ticker);

CREATE INDEX IF NOT EXISTS idx_positions_city_status
    ON positions(city, status);

CREATE INDEX IF NOT EXISTS idx_eval_time
    ON trade_evaluations(evaluation_time);

CREATE INDEX IF NOT EXISTS idx_eval_city
    ON trade_evaluations(city);
"""


class Database:
    """Simple SQLite wrapper for the weather bot."""

    def __init__(self, db_path: str = "data/weather_bot.db"):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        """Initialize the database and create tables if needed."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        logger.info("Database initialized at %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def record_observation(self, reading: TemperatureReading) -> int:
        """Store a METAR observation. Returns the row ID."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO observations
               (station_icao, observation_time, fetch_time,
                temp_celsius, temp_fahrenheit, celsius_precision_tenths, raw_metar)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                reading.station_icao,
                reading.observation_time.isoformat(),
                now,
                reading.temp_celsius,
                reading.temp_fahrenheit,
                1 if reading.celsius_precision_tenths else 0,
                reading.raw_metar,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def record_crossing(self, crossing: BracketCrossing) -> int:
        """Store a bracket crossing signal. Returns the row ID."""
        cursor = self.conn.execute(
            """INSERT INTO bracket_crossings
               (city, station_icao, observation_time, signal_time,
                old_bracket_index, old_bracket_label,
                new_bracket_index, new_bracket_label,
                observed_temp_f, daily_max_f, confidence, latency_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                crossing.city,
                crossing.station_icao,
                crossing.observation_time.isoformat(),
                crossing.signal_time.isoformat(),
                crossing.old_bracket.index if crossing.old_bracket else None,
                crossing.old_bracket.label if crossing.old_bracket else None,
                crossing.new_bracket.index,
                crossing.new_bracket.label,
                crossing.observed_temp_f,
                crossing.daily_max_f,
                crossing.confidence.value,
                crossing.latency_seconds,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def record_paper_trade(
        self,
        crossing_id: int,
        crossing: BracketCrossing,
        action: str,
        skip_reason: Optional[str] = None,
        market_yes_price_cents: Optional[int] = None,
        market_no_price_cents: Optional[int] = None,
        market_volume: Optional[int] = None,
        position_size: int = 0,
        cash_before_cents: Optional[int] = None,
        cash_after_cents: Optional[int] = None,
    ) -> int:
        """Store a paper trade (or skip decision). Returns the row ID."""
        entry_cost = (
            position_size * market_yes_price_cents
            if market_yes_price_cents and position_size
            else None
        )
        potential_profit = (
            position_size * (100 - market_yes_price_cents)
            if market_yes_price_cents and position_size
            else None
        )

        cursor = self.conn.execute(
            """INSERT INTO paper_trades
               (crossing_id, trade_time, city, bracket_index, bracket_label,
                action, skip_reason,
                market_yes_price_cents, market_no_price_cents, market_volume,
                position_size, entry_cost_cents, potential_profit_cents,
                cash_before_cents, cash_after_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                crossing_id,
                datetime.now(timezone.utc).isoformat(),
                crossing.city,
                crossing.new_bracket.index,
                crossing.new_bracket.label,
                action,
                skip_reason,
                market_yes_price_cents,
                market_no_price_cents,
                market_volume,
                position_size,
                entry_cost,
                potential_profit,
                cash_before_cents,
                cash_after_cents,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_recent_crossings(self, limit: int = 20) -> list[dict]:
        """Get recent bracket crossings for monitoring."""
        rows = self.conn.execute(
            """SELECT * FROM bracket_crossings
               ORDER BY signal_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_summary(self, target_date: str) -> list[dict]:
        """Get all observations for a given date (ISO format)."""
        rows = self.conn.execute(
            """SELECT station_icao, COUNT(*) as obs_count,
                      MAX(temp_fahrenheit) as max_temp_f,
                      MIN(temp_fahrenheit) as min_temp_f
               FROM observations
               WHERE observation_time LIKE ?
               GROUP BY station_icao""",
            (f"{target_date}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_position(
        self,
        ticker: str,
        city: str,
        bracket_label: str,
        side: str,
        entry_price_cents: int,
        contracts: int,
        portfolio_cash: int,
        portfolio_capital: int,
    ) -> int:
        """
        Record a new open position.
        Returns position ID.
        """
        entry_cost = entry_price_cents * contracts
        cursor = self.conn.execute(
            """INSERT INTO positions
               (ticker, city, bracket_label, side, entry_time, entry_price_cents,
                contracts, entry_cost_cents, portfolio_cash_before,
                portfolio_capital_before, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (
                ticker,
                city,
                bracket_label,
                side,
                datetime.now(timezone.utc).isoformat(),
                entry_price_cents,
                contracts,
                entry_cost,
                portfolio_cash,
                portfolio_capital,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def close_position(
        self,
        position_id: int,
        exit_price_cents: int,
        exit_reason: str,
    ) -> None:
        """Update a position to closed status with exit details."""
        # Get position details to calculate proceeds
        row = self.conn.execute(
            "SELECT contracts, side FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()

        if row is None:
            logger.error(f"Position {position_id} not found")
            return

        contracts = row["contracts"]
        side = row["side"]

        # Calculate proceeds based on side
        if side == "YES":
            # YES position: payout is exit_price * contracts
            exit_proceeds = exit_price_cents * contracts
        else:  # NO position
            # NO position: bought NO at entry_price, exit at (100 - yes_price)
            # If we sell NO, we get the current NO price
            exit_proceeds = exit_price_cents * contracts

        # Calculate realized P&L
        entry_cost = self.conn.execute(
            "SELECT entry_cost_cents FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()["entry_cost_cents"]
        realized_pnl = exit_proceeds - entry_cost

        self.conn.execute(
            """UPDATE positions
               SET exit_time = ?, exit_price_cents = ?, exit_proceeds_cents = ?,
                   exit_reason = ?, realized_pnl_cents = ?, status = 'closed'
               WHERE id = ?""",
            (
                datetime.now(timezone.utc).isoformat(),
                exit_price_cents,
                exit_proceeds,
                exit_reason,
                realized_pnl,
                position_id,
            ),
        )
        self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        rows = self.conn.execute(
            """SELECT * FROM positions WHERE status = 'open'
               ORDER BY entry_time DESC""",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_position_by_ticker(self, ticker: str) -> Optional[dict]:
        """Get position by ticker (for checking if position exists)."""
        row = self.conn.execute(
            "SELECT * FROM positions WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return dict(row) if row else None

    def record_trade_evaluation(
        self,
        city: str,
        ticker: str,
        bracket_label: str,
        current_temp_f: float,
        bracket_distance_f: float,
        time_of_day_local: str,
        yes_price_cents: int,
        no_price_cents: int,
        action: str,
        skip_reason: Optional[str] = None,
        position_id: Optional[int] = None,
    ) -> int:
        """Log a trade evaluation decision."""
        cursor = self.conn.execute(
            """INSERT INTO trade_evaluations
               (evaluation_time, city, ticker, bracket_label, current_temp_f,
                bracket_distance_f, time_of_day_local, yes_price_cents,
                no_price_cents, action, skip_reason, position_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                city,
                ticker,
                bracket_label,
                current_temp_f,
                bracket_distance_f,
                time_of_day_local,
                yes_price_cents,
                no_price_cents,
                action,
                skip_reason,
                position_id,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def record_portfolio_snapshot(self, portfolio) -> int:
        """Save current portfolio state."""
        from engine.portfolio import Portfolio

        summary = portfolio.get_summary()
        cursor = self.conn.execute(
            """INSERT INTO portfolio_snapshots
               (snapshot_time, cash_cents, positions_value_cents, total_capital_cents,
                realized_pnl_cents, total_trades, winning_trades, losing_trades,
                open_positions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                portfolio.current_cash_cents,
                portfolio.positions_value_cents,
                portfolio.total_capital_cents,
                portfolio.realized_pnl_cents,
                portfolio.total_trades,
                portfolio.winning_trades,
                portfolio.losing_trades,
                len(portfolio.positions),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid
