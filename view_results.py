"""
View paper trading results from the weather bot database.

Usage:
    python view_results.py                    # Show all-time summary
    python view_results.py --today            # Show today's results
    python view_results.py --week             # Show last 7 days
    python view_results.py --detailed         # Show all trades with details
"""

import sqlite3
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from collections import defaultdict


class ResultsViewer:
    """View and analyze paper trading results."""

    def __init__(self, db_path: str = "data/weather_bot.db"):
        self.db_path = db_path
        if not Path(db_path).exists():
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def get_summary(self, start_date: Optional[str] = None) -> dict:
        """Get summary statistics for paper trades."""
        date_filter = ""
        params = []

        if start_date:
            date_filter = "WHERE trade_time >= ?"
            params.append(start_date)

        # Overall stats
        overall = self.conn.execute(
            f"""SELECT
                COUNT(*) as total_decisions,
                SUM(CASE WHEN action = 'BUY_YES' THEN 1 ELSE 0 END) as total_buys,
                SUM(CASE WHEN action = 'SKIP' THEN 1 ELSE 0 END) as total_skips,
                SUM(CASE WHEN action = 'BUY_YES' THEN entry_cost_cents ELSE 0 END) as total_cost_cents,
                SUM(CASE WHEN action = 'BUY_YES' THEN potential_profit_cents ELSE 0 END) as total_potential_profit,
                SUM(CASE WHEN action = 'BUY_YES' THEN position_size ELSE 0 END) as total_contracts,
                AVG(CASE WHEN action = 'BUY_YES' THEN market_yes_price_cents END) as avg_entry_price
            FROM paper_trades
            {date_filter}""",
            params
        ).fetchone()

        # Per-city breakdown
        by_city = self.conn.execute(
            f"""SELECT
                city,
                COUNT(*) as decisions,
                SUM(CASE WHEN action = 'BUY_YES' THEN 1 ELSE 0 END) as buys,
                SUM(CASE WHEN action = 'BUY_YES' THEN potential_profit_cents ELSE 0 END) as potential_profit
            FROM paper_trades
            {date_filter}
            GROUP BY city
            ORDER BY buys DESC""",
            params
        ).fetchall()

        # Skip reasons breakdown
        skip_reasons = self.conn.execute(
            f"""SELECT
                skip_reason,
                COUNT(*) as count
            FROM paper_trades
            WHERE action = 'SKIP' {' AND trade_time >= ?' if start_date else ''}
            GROUP BY skip_reason
            ORDER BY count DESC""",
            params if start_date else []
        ).fetchall()

        return {
            'overall': dict(overall),
            'by_city': [dict(row) for row in by_city],
            'skip_reasons': [dict(row) for row in skip_reasons]
        }

    def get_all_trades(self, start_date: Optional[str] = None) -> list[dict]:
        """Get all paper trades with full details."""
        date_filter = ""
        params = []

        if start_date:
            date_filter = "WHERE pt.trade_time >= ?"
            params.append(start_date)

        trades = self.conn.execute(
            f"""SELECT
                pt.trade_time,
                pt.city,
                pt.bracket_label,
                pt.action,
                pt.skip_reason,
                pt.market_yes_price_cents,
                pt.position_size,
                pt.entry_cost_cents,
                pt.potential_profit_cents,
                bc.observed_temp_f,
                bc.daily_max_f,
                bc.confidence,
                bc.latency_seconds
            FROM paper_trades pt
            JOIN bracket_crossings bc ON pt.crossing_id = bc.id
            {date_filter}
            ORDER BY pt.trade_time DESC""",
            params
        ).fetchall()

        return [dict(row) for row in trades]

    def get_crossings_summary(self, start_date: Optional[str] = None) -> dict:
        """Get bracket crossing statistics."""
        date_filter = ""
        params = []

        if start_date:
            date_filter = "WHERE signal_time >= ?"
            params.append(start_date)

        stats = self.conn.execute(
            f"""SELECT
                COUNT(*) as total_crossings,
                AVG(latency_seconds) as avg_latency,
                MIN(latency_seconds) as min_latency,
                MAX(latency_seconds) as max_latency,
                SUM(CASE WHEN confidence = 'high' THEN 1 ELSE 0 END) as high_confidence,
                SUM(CASE WHEN confidence = 'low' THEN 1 ELSE 0 END) as low_confidence
            FROM bracket_crossings
            {date_filter}""",
            params
        ).fetchone()

        return dict(stats)

    def close(self):
        self.conn.close()


def print_separator(char="=", length=80):
    print(char * length)


def print_summary(viewer: ResultsViewer, start_date: Optional[str] = None, title: str = "PAPER TRADING SUMMARY"):
    """Print formatted summary of results."""
    print_separator()
    print(f"  {title}")
    print_separator()

    summary = viewer.get_summary(start_date)
    overall = summary['overall']

    # Overall statistics
    print("\n📊 OVERALL PERFORMANCE")
    print_separator("-")
    print(f"  Total Decisions:        {overall['total_decisions']}")
    print(f"  Trades Executed:        {overall['total_buys']} ({overall['total_buys']/max(overall['total_decisions'],1)*100:.1f}%)")
    print(f"  Trades Skipped:         {overall['total_skips']} ({overall['total_skips']/max(overall['total_decisions'],1)*100:.1f}%)")

    if overall['total_buys'] > 0:
        total_cost = overall['total_cost_cents'] / 100
        total_potential = overall['total_potential_profit'] / 100
        avg_price = overall['avg_entry_price']

        print(f"\n💰 FINANCIALS (if all trades won)")
        print_separator("-")
        print(f"  Total Contracts:        {overall['total_contracts']}")
        print(f"  Total Investment:       ${total_cost:.2f}")
        print(f"  Potential Profit:       ${total_potential:.2f}")
        print(f"  Potential ROI:          {(total_potential/max(total_cost,0.01))*100:.1f}%")
        print(f"  Average Entry Price:    {avg_price:.0f}¢")
        print(f"  Average Max Profit:     ${total_potential/overall['total_buys']:.2f} per trade")

    # Per-city breakdown
    if summary['by_city']:
        print(f"\n🌆 BY CITY")
        print_separator("-")
        print(f"  {'City':<15} {'Decisions':<12} {'Buys':<10} {'Potential $':<15}")
        print_separator("-")
        for city in summary['by_city']:
            potential = (city['potential_profit'] or 0) / 100
            print(f"  {city['city']:<15} {city['decisions']:<12} {city['buys']:<10} ${potential:>12.2f}")

    # Skip reasons
    if summary['skip_reasons']:
        print(f"\n⏭️  SKIP REASONS")
        print_separator("-")
        for reason in summary['skip_reasons']:
            if reason['skip_reason']:
                print(f"  {reason['skip_reason']:<40} {reason['count']:>5}x")

    # Crossing statistics
    crossings = viewer.get_crossings_summary(start_date)
    if crossings['total_crossings'] > 0:
        print(f"\n⚡ SIGNAL LATENCY")
        print_separator("-")
        print(f"  Total Crossings:        {crossings['total_crossings']}")
        print(f"  High Confidence:        {crossings['high_confidence']} ({crossings['high_confidence']/crossings['total_crossings']*100:.1f}%)")
        print(f"  Low Confidence:         {crossings['low_confidence']} ({crossings['low_confidence']/crossings['total_crossings']*100:.1f}%)")
        print(f"  Average Latency:        {crossings['avg_latency']:.2f}s")
        print(f"  Min Latency:            {crossings['min_latency']:.2f}s")
        print(f"  Max Latency:            {crossings['max_latency']:.2f}s")

    print_separator()


def print_detailed_trades(viewer: ResultsViewer, start_date: Optional[str] = None, limit: int = 20):
    """Print detailed trade history."""
    trades = viewer.get_all_trades(start_date)

    if not trades:
        print("\nNo trades found.")
        return

    print_separator()
    print(f"  TRADE HISTORY (showing last {min(limit, len(trades))} trades)")
    print_separator()

    for i, trade in enumerate(trades[:limit], 1):
        trade_time = datetime.fromisoformat(trade['trade_time']).strftime('%Y-%m-%d %H:%M:%S')

        print(f"\n[{i}] {trade_time} UTC")
        print(f"    City: {trade['city']}  |  Bracket: {trade['bracket_label']}")
        print(f"    Temp: {trade['observed_temp_f']:.1f}°F (daily max: {trade['daily_max_f']:.1f}°F)")
        print(f"    Confidence: {trade['confidence']}  |  Latency: {trade['latency_seconds']:.1f}s")

        if trade['action'] == 'BUY_YES':
            cost = trade['entry_cost_cents'] / 100
            profit = trade['potential_profit_cents'] / 100
            print(f"    ✅ BOUGHT {trade['position_size']} @ {trade['market_yes_price_cents']}¢")
            print(f"       Cost: ${cost:.2f}  |  Max Profit: ${profit:.2f}")
        else:
            print(f"    ⏭️  SKIPPED: {trade['skip_reason']}")
            if trade['market_yes_price_cents']:
                print(f"       Market price was: {trade['market_yes_price_cents']}¢")

    if len(trades) > limit:
        print(f"\n... and {len(trades) - limit} more trades")

    print_separator()


def main():
    parser = argparse.ArgumentParser(description="View Kalshi weather bot paper trading results")
    parser.add_argument("--db", default="data/weather_bot.db", help="Database path")
    parser.add_argument("--today", action="store_true", help="Show only today's results")
    parser.add_argument("--week", action="store_true", help="Show last 7 days")
    parser.add_argument("--detailed", action="store_true", help="Show detailed trade history")
    parser.add_argument("--limit", type=int, default=20, help="Limit for detailed view")
    args = parser.parse_args()

    viewer = ResultsViewer(args.db)

    # Determine date filter
    start_date = None
    title = "PAPER TRADING SUMMARY (All Time)"

    if args.today:
        start_date = datetime.now(timezone.utc).date().isoformat()
        title = f"PAPER TRADING SUMMARY (Today - {start_date})"
    elif args.week:
        start_date = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        title = "PAPER TRADING SUMMARY (Last 7 Days)"

    # Show summary
    print_summary(viewer, start_date, title)

    # Show detailed trades if requested
    if args.detailed:
        print("\n")
        print_detailed_trades(viewer, start_date, args.limit)

    viewer.close()


if __name__ == "__main__":
    main()
