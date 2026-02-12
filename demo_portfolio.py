"""
Demo script showing how the portfolio management works.

Shows:
- Starting with $100
- Position sizing based on risk (2% per trade)
- Tracking cash, positions, and P&L
"""

from engine.portfolio import Portfolio

def print_separator(char="="):
    print(char * 70)


def demo_portfolio():
    """Demonstrate portfolio mechanics."""
    print_separator()
    print("  PORTFOLIO DEMO - Starting with $100")
    print_separator()

    # Create portfolio with $100 starting capital
    portfolio = Portfolio.create(starting_capital_dollars=100.0)

    print(f"\n📊 Initial State:")
    print(f"   Starting Capital: ${portfolio.starting_capital_dollars:.2f}")
    print(f"   Current Cash:     ${portfolio.current_cash_dollars:.2f}")
    print(f"   Open Positions:   {len(portfolio.positions)}")

    print("\n" + "=" * 70)
    print("  SCENARIO: Trading with 2% risk per trade")
    print("=" * 70)

    # Example 1: Calculate position size for a 50¢ contract
    print("\n📈 Trade 1: YES price at 50¢")
    yes_price = 50
    contracts = portfolio.calculate_position_size(
        yes_price_cents=yes_price,
        max_contracts=20,
        risk_percent=2.0
    )
    print(f"   With 2% risk rule: {contracts} contracts")
    print(f"   Cost: {contracts} × 50¢ = ${contracts * yes_price / 100:.2f}")
    print(f"   Max profit if wins: {contracts} × (100¢ - 50¢) = ${contracts * 50 / 100:.2f}")

    # Open the position
    success = portfolio.open_position(
        ticker="KXHIGHNY-25JAN01-T65",
        city="NYC",
        bracket_label="65°F to <70°F",
        entry_price_cents=yes_price,
        contracts=contracts,
    )

    if success:
        print(f"   ✅ Position opened")
        print(f"   Cash remaining: ${portfolio.current_cash_dollars:.2f}")

    # Example 2: Another trade at 70¢
    print("\n📈 Trade 2: YES price at 70¢")
    yes_price = 70
    contracts = portfolio.calculate_position_size(
        yes_price_cents=yes_price,
        max_contracts=20,
        risk_percent=2.0
    )
    print(f"   With 2% risk rule: {contracts} contracts")
    print(f"   Cost: {contracts} × 70¢ = ${contracts * yes_price / 100:.2f}")

    success = portfolio.open_position(
        ticker="KXHIGHCHI-25JAN01-T60",
        city="Chicago",
        bracket_label="60°F to <65°F",
        entry_price_cents=yes_price,
        contracts=contracts,
    )

    if success:
        print(f"   ✅ Position opened")
        print(f"   Cash remaining: ${portfolio.current_cash_dollars:.2f}")

    # Show current state
    print("\n" + "=" * 70)
    print("  PORTFOLIO STATE (2 open positions)")
    print("=" * 70)
    summary = portfolio.get_summary()
    print(f"\n   Cash:              ${summary['current_cash']:.2f}")
    print(f"   Positions Value:   ${summary['positions_value']:.2f}")
    print(f"   Total Capital:     ${summary['total_capital']:.2f}")
    print(f"   Open Positions:    {summary['open_positions']}")

    # Settle positions
    print("\n" + "=" * 70)
    print("  SETTLING TRADES")
    print("=" * 70)

    # Trade 1 wins
    print("\n✅ Trade 1 WINS (NYC bracket hit)")
    pnl1 = portfolio.close_position("KXHIGHNY-25JAN01-T65", won=True)
    print(f"   P&L: ${pnl1 / 100:+.2f}")
    print(f"   Cash: ${portfolio.current_cash_dollars:.2f}")

    # Trade 2 loses
    print("\n❌ Trade 2 LOSES (Chicago bracket missed)")
    pnl2 = portfolio.close_position("KXHIGHCHI-25JAN01-T60", won=False)
    print(f"   P&L: ${pnl2 / 100:+.2f}")
    print(f"   Cash: ${portfolio.current_cash_dollars:.2f}")

    # Final summary
    print("\n" + "=" * 70)
    print("  FINAL RESULTS")
    print("=" * 70)

    summary = portfolio.get_summary()
    print(f"\n   Starting Capital:  ${summary['starting_capital']:.2f}")
    print(f"   Final Capital:     ${summary['total_capital']:.2f}")
    print(f"   Realized P&L:      ${summary['realized_pnl']:+.2f}")
    print(f"   ROI:               {summary['roi_percent']:+.1f}%")
    print(f"\n   Total Trades:      {summary['total_trades']}")
    print(f"   Wins:              {summary['winning_trades']}")
    print(f"   Losses:            {summary['losing_trades']}")
    print(f"   Win Rate:          {summary['win_rate']:.1f}%")

    print("\n" + "=" * 70)
    print(f"\n   {portfolio}\n")


if __name__ == "__main__":
    demo_portfolio()
