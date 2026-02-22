"""
Portfolio management for paper trading.

Tracks:
- Starting capital
- Current cash balance
- Open positions
- Realized P&L
- Position sizing based on available capital
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open paper trading position."""

    ticker: str
    city: str
    bracket_label: str
    side: str  # 'YES' or 'NO'
    entry_time: datetime
    entry_price_cents: int  # Price paid per contract
    contracts: int

    @property
    def cost_cents(self) -> int:
        """Total cost of position."""
        return self.entry_price_cents * self.contracts

    @property
    def max_profit_cents(self) -> int:
        """Max profit if position wins."""
        return (100 - self.entry_price_cents) * self.contracts

    def settle(self, won: bool) -> int:
        """
        Settle the position.

        Args:
            won: True if the position won, False if it lost

        Returns:
            Profit/loss in cents (positive = profit, negative = loss)
        """
        if self.side == "YES":
            if won:
                # YES wins: receive $1 per contract
                payout = 100 * self.contracts
                return payout - self.cost_cents
            else:
                # YES loses: position expires worthless
                return -self.cost_cents
        else:  # NO position
            if won:
                # NO wins: receive $1 per contract
                payout = 100 * self.contracts
                return payout - self.cost_cents
            else:
                # NO loses: position expires worthless
                return -self.cost_cents


@dataclass
class Portfolio:
    """
    Paper trading portfolio tracker.

    Manages capital, positions, and P&L.
    """

    starting_capital_cents: int
    current_cash_cents: int
    positions: dict[str, Position] = field(default_factory=dict)  # ticker -> Position
    realized_pnl_cents: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    @classmethod
    def create(cls, starting_capital_dollars: float = 100.0) -> Portfolio:
        """Create a new portfolio with starting capital."""
        capital_cents = int(starting_capital_dollars * 100)
        return cls(
            starting_capital_cents=capital_cents,
            current_cash_cents=capital_cents,
        )

    @property
    def starting_capital_dollars(self) -> float:
        return self.starting_capital_cents / 100

    @property
    def current_cash_dollars(self) -> float:
        return self.current_cash_cents / 100

    @property
    def positions_value_cents(self) -> int:
        """Total cost of all open positions."""
        return sum(p.cost_cents for p in self.positions.values())

    @property
    def total_capital_cents(self) -> int:
        """Current cash + positions value."""
        return self.current_cash_cents + self.positions_value_cents

    @property
    def unrealized_max_profit_cents(self) -> int:
        """Maximum potential profit from open positions if all win."""
        return sum(p.max_profit_cents for p in self.positions.values())

    @property
    def total_pnl_cents(self) -> int:
        """Total P&L = realized P&L (from settled trades)."""
        return self.realized_pnl_cents

    @property
    def win_rate(self) -> float:
        """Percentage of winning trades."""
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100

    def can_afford(self, cost_cents: int) -> bool:
        """Check if portfolio has enough cash for a trade."""
        return self.current_cash_cents >= cost_cents

    def calculate_position_size(
        self,
        yes_price_cents: int,
        max_contracts: int = 10,
        risk_percent: float = 2.0,
    ) -> int:
        """
        Calculate how many contracts to buy based on capital and risk.

        Args:
            yes_price_cents: Current YES price in cents
            max_contracts: Maximum contracts per trade
            risk_percent: Max % of capital to risk per trade (default 2%)

        Returns:
            Number of contracts to buy
        """
        # Calculate max risk in cents
        max_risk_cents = int(self.total_capital_cents * (risk_percent / 100))

        # Risk per contract = entry price (what we could lose)
        risk_per_contract = yes_price_cents

        # Calculate contracts based on risk
        contracts_by_risk = max_risk_cents // risk_per_contract if risk_per_contract > 0 else 0

        # Also check what we can afford with current cash
        contracts_by_cash = self.current_cash_cents // yes_price_cents if yes_price_cents > 0 else 0

        # Take the minimum of: risk-based, cash-based, and max contracts
        contracts = min(contracts_by_risk, contracts_by_cash, max_contracts)

        return max(0, contracts)  # Never negative

    def restore_position(
        self,
        ticker: str,
        city: str,
        bracket_label: str,
        side: str,
        entry_time: datetime,
        entry_price_cents: int,
        contracts: int,
    ) -> None:
        """
        Re-add a position to tracking after a restart WITHOUT deducting cash.
        Cash was already deducted when the position was originally opened.
        Only call this during portfolio restore from the database.
        """
        position = Position(
            ticker=ticker,
            city=city,
            bracket_label=bracket_label,
            side=side,
            entry_time=entry_time,
            entry_price_cents=entry_price_cents,
            contracts=contracts,
        )
        self.positions[ticker] = position

    def open_position(
        self,
        ticker: str,
        city: str,
        bracket_label: str,
        side: str,
        entry_price_cents: int,
        contracts: int,
    ) -> bool:
        """
        Open a new position.

        Args:
            side: 'YES' or 'NO'

        Returns:
            True if position opened successfully, False if insufficient funds
        """
        cost = entry_price_cents * contracts

        if not self.can_afford(cost):
            logger.warning(
                "Insufficient funds: need %d¢, have %d¢",
                cost,
                self.current_cash_cents,
            )
            return False

        # Deduct cash
        self.current_cash_cents -= cost

        # Add position
        position = Position(
            ticker=ticker,
            city=city,
            bracket_label=bracket_label,
            side=side,
            entry_time=datetime.now(timezone.utc),
            entry_price_cents=entry_price_cents,
            contracts=contracts,
        )
        self.positions[ticker] = position

        logger.info(
            "OPEN: %s %s | %d @ %d¢ = %d¢ | Cash: %d¢ -> %d¢",
            side,
            ticker,
            contracts,
            entry_price_cents,
            cost,
            self.current_cash_cents + cost,
            self.current_cash_cents,
        )

        return True

    def close_position(
        self,
        ticker: str,
        exit_price_cents: Optional[int] = None,
        won: Optional[bool] = None,
    ) -> Optional[int]:
        """
        Close a position and realize P&L.

        For early exits (take-profit / stop-loss), pass exit_price_cents — proceeds
        are calculated as exit_price * contracts so the portfolio cash reflects the
        actual sale price, not a binary settlement.

        For settlement, pass won=True/False — pays out $1/contract on win, $0 on loss.

        Args:
            ticker: Position ticker to close
            exit_price_cents: Actual sale price per contract (early exit)
            won: Settlement outcome (True = win, False = loss)

        Returns:
            Realized P&L in cents, or None if position not found
        """
        if ticker not in self.positions:
            logger.warning("Position not found: %s", ticker)
            return None

        position = self.positions[ticker]

        if exit_price_cents is not None:
            # Early exit at a specific price — use actual proceeds
            proceeds = exit_price_cents * position.contracts
            pnl = proceeds - position.cost_cents
            self.current_cash_cents += proceeds
        elif won is not None:
            # Settlement — binary outcome
            pnl = position.settle(won)
            if won:
                self.current_cash_cents += position.contracts * 100
        else:
            logger.warning("close_position called with neither exit_price_cents nor won")
            return None

        # Update stats
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        self.realized_pnl_cents += pnl

        # Remove position
        del self.positions[ticker]

        logger.info(
            "CLOSE: %s | %s | P&L: %+d¢ | Cash: %d¢",
            ticker,
            "WIN" if won else "LOSS",
            pnl,
            self.current_cash_cents,
        )

        return pnl

    def get_summary(self) -> dict:
        """Get portfolio summary for display."""
        return {
            'starting_capital': self.starting_capital_dollars,
            'current_cash': self.current_cash_dollars,
            'positions_value': self.positions_value_cents / 100,
            'total_capital': self.total_capital_cents / 100,
            'realized_pnl': self.realized_pnl_cents / 100,
            'unrealized_max_profit': self.unrealized_max_profit_cents / 100,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': self.win_rate,
            'open_positions': len(self.positions),
            'roi_percent': (self.total_pnl_cents / self.starting_capital_cents * 100)
                          if self.starting_capital_cents > 0 else 0,
        }

    def __str__(self) -> str:
        summary = self.get_summary()
        return (
            f"Portfolio(capital=${summary['total_capital']:.2f}, "
            f"cash=${summary['current_cash']:.2f}, "
            f"positions={summary['open_positions']}, "
            f"P&L=${summary['realized_pnl']:+.2f}, "
            f"ROI={summary['roi_percent']:+.1f}%)"
        )
