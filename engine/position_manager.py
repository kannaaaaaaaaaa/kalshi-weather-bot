"""
Position lifecycle management for paper trading.

Handles opening positions, monitoring for exits, and closing positions
while integrating with Portfolio and Database.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, date
from typing import Optional

from config.settings import TRADING
from engine.portfolio import Portfolio
from storage.db import Database
from exchange.kalshi_client import KalshiClient, MarketPrice
from engine.trading_strategy import TradingStrategy, TradingSignal

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position lifecycle: opening, monitoring, closing."""

    def __init__(
        self,
        portfolio: Portfolio,
        db: Database,
        kalshi: KalshiClient,
        strategy: TradingStrategy,
    ):
        self.portfolio = portfolio
        self.db = db
        self.kalshi = kalshi
        self.strategy = strategy

    @staticmethod
    def _ticker_date(ticker: str) -> Optional[date]:
        """
        Parse the event date from a Kalshi ticker.
        Format: KXHIGHNY-26FEB17-T44  ->  2026-02-17
        """
        m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
        if not m:
            return None
        try:
            return datetime.strptime(f"{m.group(1)}{m.group(2)}{m.group(3)}", "%y%b%d").date()
        except ValueError:
            return None

    @staticmethod
    def _is_tradeable(ticker: str) -> bool:
        """
        Return True if the market is current or future — rejects expired markets.
        Kalshi sometimes opens next-day markets on the evening before, so we
        allow any date >= today rather than requiring an exact today match.
        """
        market_date = PositionManager._ticker_date(ticker)
        if market_date is None:
            return True  # Unknown date format — allow it
        return market_date >= datetime.now(timezone.utc).date()

    async def open_position(
        self,
        signal: TradingSignal,
    ) -> Optional[int]:
        """
        Open a new position based on a trading signal.

        Returns:
            Position ID if successful, None otherwise
        """
        side = "YES" if signal.action == "BUY_YES" else "NO"
        ticker = signal.bracket.ticker
        price = signal.price_cents
        contracts = signal.contracts

        # Reject expired markets (yesterday or older) — only trade current/future
        if not self._is_tradeable(ticker):
            logger.debug("SKIP: %s is an expired market", ticker)
            return None

        # Check position limits
        if not self._check_position_limits(signal.bracket.ticker):
            logger.info(
                "SKIP: Position limits exceeded for %s",
                ticker,
            )
            return None

        # Attempt to open in portfolio
        success = self.portfolio.open_position(
            ticker=ticker,
            city=signal.bracket.ticker.split("-")[0],  # Extract city from ticker
            bracket_label=signal.bracket.label,
            side=side,
            entry_price_cents=price,
            contracts=contracts,
        )

        if not success:
            logger.warning("Failed to open position: insufficient funds")
            return None

        # Record in database
        position_id = self.db.open_position(
            ticker=ticker,
            city=signal.bracket.ticker.split("-")[0],
            bracket_label=signal.bracket.label,
            side=side,
            entry_price_cents=price,
            contracts=contracts,
            portfolio_cash=self.portfolio.current_cash_cents,
            portfolio_capital=self.portfolio.total_capital_cents,
        )

        logger.info(
            "OPENED %s position: %s | %d @ %d¢ | Reason: %s",
            side,
            ticker,
            contracts,
            price,
            signal.reason,
        )

        return position_id

    async def close_position(
        self,
        position_id: int,
        exit_price_cents: int,
        exit_reason: str,
    ) -> bool:
        """
        Close a position and realize P&L.

        Returns:
            True if successful
        """
        # Get position from DB
        position = self.db.get_open_positions()
        position_dict = next((p for p in position if p["id"] == position_id), None)

        if not position_dict:
            logger.error(f"Position {position_id} not found")
            return False

        ticker = position_dict["ticker"]
        side = position_dict["side"]

        # Early exit: sell at current market price (not binary settlement)
        pnl = self.portfolio.close_position(ticker, exit_price_cents=exit_price_cents)

        if pnl is None:
            logger.error(f"Failed to close position {ticker} in portfolio")
            return False

        # Update database
        self.db.close_position(
            position_id=position_id,
            exit_price_cents=exit_price_cents,
            exit_reason=exit_reason,
        )

        logger.info(
            "CLOSED %s position: %s @ %d¢ | Reason: %s | P&L: %+d¢",
            side,
            ticker,
            exit_price_cents,
            exit_reason,
            pnl,
        )

        return True

    async def check_position_exits(self) -> list[tuple[int, str, int]]:
        """
        Check all open positions for exit conditions.

        Returns:
            List of (position_id, exit_reason, exit_price) tuples to close
        """
        exits = []
        open_positions = self.db.get_open_positions()

        for position in open_positions:
            ticker = position["ticker"]

            # If the market date has already passed, try to determine settlement.
            if not self._is_tradeable(ticker):
                # Try to fetch final price from Kalshi (settled markets show
                # yes_price=100 for the winner, 0 for losers).
                settlement_price = await self._get_settlement_price(position)
                logger.info(
                    "Market %s has expired, settling at %dc (%s side)",
                    ticker, settlement_price, position["side"],
                )
                exits.append((position["id"], "settlement", settlement_price))
                continue

            # Fetch current price
            price = await self.kalshi.fetch_market_price(ticker)
            if price is None:
                logger.warning("Could not fetch price for %s, skipping exit check", ticker)
                continue

            # Check exit conditions
            should_exit, reason = self.strategy.should_exit_position(position, price)

            if should_exit:
                # Get exit price based on side
                side = position["side"]
                exit_price = price.yes_price_cents if side == "YES" else price.no_price_cents
                exits.append((position["id"], reason, exit_price))

        return exits

    async def _get_settlement_price(self, position: dict) -> int:
        """
        Determine the settlement price for an expired position.

        For binary markets: winner pays 100c/contract, loser pays 0c.
        We try to fetch from Kalshi; if unavailable, conservatively assume loss.
        """
        ticker = position["ticker"]
        side = position["side"]

        try:
            price = await self.kalshi.fetch_market_price(ticker)
            if price is not None:
                # Settled markets show yes_price=100 (winner) or 0 (loser)
                if side == "YES":
                    return price.yes_price_cents
                else:
                    return price.no_price_cents
        except Exception:
            logger.warning("Could not fetch settlement for %s", ticker)

        # Conservative fallback: assume position lost (0 payout)
        logger.warning(
            "No settlement data for %s — assuming loss (0c payout)", ticker
        )
        return 0

    def _check_position_limits(self, ticker: str) -> bool:
        """
        Check if we can open a new position without violating limits.

        Returns:
            True if within limits
        """
        # Block re-entry on any ticker already traded (open or closed)
        # The positions table has a UNIQUE constraint on ticker, so we can't
        # insert a second row regardless of status.
        existing = self.db.get_position_by_ticker(ticker)
        if existing:
            logger.debug(
                "Position already exists for %s (status: %s)", ticker, existing["status"]
            )
            return False

        # Only count positions for current/future markets when checking limits.
        # Past-day positions are stale and will be closed by check_position_exits.
        all_open = self.db.get_open_positions()
        active_positions = [p for p in all_open if self._is_tradeable(p["ticker"])]

        if len(active_positions) >= TRADING.max_total_positions:
            logger.debug("Max total positions reached (%d)", len(active_positions))
            return False

        # Check per-city limit
        city = ticker.split("-")[0] if "-" in ticker else "unknown"
        city_positions = [p for p in active_positions if p["city"] == city]
        if len(city_positions) >= TRADING.max_positions_per_city:
            logger.debug("Max positions for %s reached (%d)", city, len(city_positions))
            return False

        # Check capital deployment limit
        deployed_pct = (
            self.portfolio.positions_value_cents / self.portfolio.total_capital_cents * 100
            if self.portfolio.total_capital_cents > 0
            else 0
        )
        if deployed_pct >= TRADING.max_deployed_capital_pct:
            logger.debug("Max deployed capital reached (%.1f%%)", deployed_pct)
            return False

        return True

    def load_portfolio_from_db(self) -> None:
        """
        Restore portfolio state from the database on bot startup/restart.

        Loads cash balance from the last portfolio snapshot, then re-adds
        open positions WITHOUT re-deducting cash (already deducted in the
        original run).
        """
        # Step 1: restore cash from last snapshot
        snap = self.db.conn.execute(
            "SELECT cash_cents FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if snap:
            self.portfolio.current_cash_cents = snap["cash_cents"]
            logger.info(
                "Restored portfolio cash from snapshot: %d¢ ($%.2f)",
                snap["cash_cents"],
                snap["cash_cents"] / 100,
            )
        else:
            logger.info("No portfolio snapshot found — starting with fresh capital")

        # Step 2: re-add open positions without deducting cash
        open_positions = self.db.get_open_positions()
        if not open_positions:
            logger.info("No open positions to restore")
            return

        logger.info("Restoring %d open positions from database", len(open_positions))
        for pos in open_positions:
            entry_time = datetime.fromisoformat(pos["entry_time"])
            self.portfolio.restore_position(
                ticker=pos["ticker"],
                city=pos["city"],
                bracket_label=pos["bracket_label"],
                side=pos["side"],
                entry_time=entry_time,
                entry_price_cents=pos["entry_price_cents"],
                contracts=pos["contracts"],
            )
            logger.debug(
                "Restored %s %s | %d @ %d¢",
                pos["side"],
                pos["ticker"],
                pos["contracts"],
                pos["entry_price_cents"],
            )
