"""
Quick status check - shows current bot activity at a glance.

Usage:
    python quick_status.py
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = "data/weather_bot.db"


def get_quick_status():
    """Get quick status summary."""
    if not Path(DB_PATH).exists():
        print("❌ Database not found. Bot hasn't run yet.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Last observation time
    last_obs = conn.execute("""
        SELECT station_icao, observation_time, temp_fahrenheit
        FROM observations
        ORDER BY fetch_time DESC
        LIMIT 1
    """).fetchone()

    # Recent activity (last hour)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    recent_stats = conn.execute("""
        SELECT
            COUNT(DISTINCT o.station_icao) as active_stations,
            COUNT(o.id) as observations
        FROM observations o
        WHERE o.fetch_time >= ?
    """, (one_hour_ago,)).fetchone()

    recent_trades = conn.execute("""
        SELECT COUNT(*) as count
        FROM paper_trades
        WHERE action = 'BUY_YES' AND trade_time >= ?
    """, (one_hour_ago,)).fetchone()

    # All-time stats
    all_time = conn.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(potential_profit_cents) as total_potential
        FROM paper_trades
        WHERE action = 'BUY_YES'
    """).fetchone()

    conn.close()

    # Display
    print("\n" + "=" * 60)
    print("  KALSHI WEATHER BOT - QUICK STATUS")
    print("=" * 60)

    if last_obs:
        last_time = datetime.fromisoformat(last_obs['observation_time'])
        time_ago = (datetime.now(timezone.utc) - last_time).total_seconds()

        if time_ago < 120:
            status_emoji = "🟢"
            status_text = "RUNNING"
        elif time_ago < 600:
            status_emoji = "🟡"
            status_text = "IDLE"
        else:
            status_emoji = "🔴"
            status_text = "STOPPED"

        print(f"\n{status_emoji} Status: {status_text}")
        print(f"\n📡 Last observation:")
        print(f"   Station: {last_obs['station_icao']}")
        print(f"   Temp: {last_obs['temp_fahrenheit']:.1f}°F")
        print(f"   Time: {last_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"   ({int(time_ago)}s ago)")

        print(f"\n⏱️  Last hour:")
        print(f"   Stations polled: {recent_stats['active_stations']}")
        print(f"   Observations: {recent_stats['observations']}")
        print(f"   Trades executed: {recent_trades['count']}")

        print(f"\n💰 All-time totals:")
        print(f"   Total trades: {all_time['total_trades']}")
        potential = (all_time['total_potential'] or 0) / 100
        print(f"   Potential profit: ${potential:.2f}")

    else:
        print("\n❌ No observations recorded yet")

    print("\n" + "=" * 60)
    print("\n💡 Commands:")
    print("   python view_results.py         - Full results summary")
    print("   python view_results.py --today - Today's results")
    print("   python web_dashboard.py        - Start web UI")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    get_quick_status()
