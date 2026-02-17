"""
Position lifecycle management for paper trading.

Handles opening positions, monitoring for exits, and closing positions
while integrating with Portfolio and Database.
"""

from __future__ import annotations

import logging
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

        # Close in portfolio
        # For YES: won if price reached 100, lost if expired worthless
        # For now, we're just simulating by selling at current price
        won = exit_price_cents >= 90  # Simplified - real logic would check actual settlement

        pnl = self.portfolio.close_position(ticker, won=won)

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

    def _check_position_limits(self, ticker: str) -> bool:
        """
        Check if we can open a new position without violating limits.

        Returns:
            True if within limits
        """
        # Check if position already exists
        existing = self.db.get_position_by_ticker(ticker)
        if existing and existing["status"] == "open":
            logger.debug("Position already exists for %s", ticker)
            return False

        # Check total position limit
        open_positions = self.db.get_open_positions()
        if len(open_positions) >= TRADING.max_total_positions:
            logger.debug("Max total positions reached (%d)", len(open_positions))
            return False

        # Check per-city limit
        city = ticker.split("-")[0] if "-" in ticker else "unknown"
        city_positions = [p for p in open_positions if p["city"] == city]
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
        Load open positions from database and restore portfolio state.

        Call this on bot startup to recover from restarts.
        """
        open_positions = self.db.get_open_positions()

        if not open_positions:
            logger.info("No open positions to restore")
            return

        logger.info("Restoring %d open positions from database", len(open_positions))

        for pos in open_positions:
            # Add position to portfolio
            success = self.portfolio.open_position(
                ticker=pos["ticker"],
                city=pos["city"],
                bracket_label=pos["bracket_label"],
                side=pos["side"],
                entry_price_cents=pos["entry_price_cents"],
                contracts=pos["contracts"],
            )

            if success:
                logger.debug(
                    "Restored %s position: %s | %d @ %d¢",
                    pos["side"],
                    pos["ticker"],
                    pos["contracts"],
                    pos["entry_price_cents"],
                )
            else:
                logger.error("Failed to restore position %s", pos["ticker"])
