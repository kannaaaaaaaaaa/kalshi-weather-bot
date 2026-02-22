"""
View paper trading results from the weather bot database.

Usage:
    python view_results.py                    # Portfolio summary + open positions
    python view_results.py --today            # Filter to today only
    python view_results.py --week             # Filter to last 7 days
    python view_results.py --positions        # Show all open positions
    python view_results.py --closed           # Show closed positions + P&L
    python view_results.py --evals            # Show trade evaluations (what was looked at)
    python view_results.py --all              # Show everything
"""

import sqlite3
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def connect(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def sep(char="=", length=80):
    print(char * length)


def fmt_cents(c: Optional[int]) -> str:
    if c is None:
        return "—"
    return f"${c/100:+.2f}" if c != 0 else "$0.00"


def fmt_price(c: Optional[int]) -> str:
    if c is None:
        return " —¢"
    return f"{c:3d}¢"


def fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).strftime("%m/%d %H:%M")


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------

def show_portfolio(conn: sqlite3.Connection, since: Optional[str]) -> None:
    sep()
    print("  PORTFOLIO SUMMARY")
    sep()

    # Open positions
    open_pos = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time"
    ).fetchall()

    # Closed positions (optionally filtered)
    q = "SELECT * FROM positions WHERE status = 'closed'"
    params = []
    if since:
        q += " AND exit_time >= ?"
        params.append(since)
    closed_pos = conn.execute(q + " ORDER BY exit_time DESC", params).fetchall()

    # P&L from closed positions
    pnl_row = conn.execute(
        "SELECT SUM(realized_pnl_cents) as total, COUNT(*) as n FROM positions WHERE status = 'closed'"
    ).fetchone()
    total_pnl = pnl_row["total"] or 0
    closed_count = pnl_row["n"] or 0

    win_row = conn.execute(
        "SELECT COUNT(*) as n FROM positions WHERE status = 'closed' AND realized_pnl_cents > 0"
    ).fetchone()
    wins = win_row["n"] or 0

    # Deployed capital
    deployed = conn.execute(
        "SELECT SUM(entry_cost_cents) as d FROM positions WHERE status = 'open'"
    ).fetchone()["d"] or 0

    print(f"\n  Open Positions:   {len(open_pos)}")
    print(f"  Closed Positions: {closed_count}")
    print(f"  Deployed Capital: ${deployed/100:.2f}")
    print(f"  Realized P&L:     {fmt_cents(total_pnl)}")
    if closed_count > 0:
        print(f"  Win Rate:         {wins}/{closed_count} ({wins/closed_count*100:.0f}%)")

    # Last portfolio snapshot
    snap = conn.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if snap:
        print(f"\n  --- Last Portfolio Snapshot ({fmt_time(snap['snapshot_time'])}) ---")
        print(f"  Cash:             ${snap['cash_cents']/100:.2f}")
        print(f"  Total Capital:    ${snap['total_capital_cents']/100:.2f}")
        print(f"  ROI:              {(snap['realized_pnl_cents']/(snap['total_capital_cents'] or 1))*100:+.2f}%")
    sep()


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------

def show_open_positions(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' ORDER BY city, entry_time"
    ).fetchall()

    sep()
    print(f"  OPEN POSITIONS ({len(rows)})")
    sep()

    if not rows:
        print("  No open positions.")
        sep()
        return

    by_city: dict[str, list] = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(r)

    for city, positions in by_city.items():
        print(f"\n  {city.upper()}")
        print(f"  {'Side':<5} {'Bracket':<22} {'Entry':<6} {'Qty':<5} {'Cost':<10} {'Entry Time'}")
        print("  " + "-" * 70)
        for p in positions:
            cost = p["entry_price_cents"] * p["contracts"]
            print(
                f"  {p['side']:<5} {p['bracket_label']:<22} "
                f"{fmt_price(p['entry_price_cents']):<6} {p['contracts']:<5} "
                f"${cost/100:<9.2f} {fmt_time(p['entry_time'])}"
            )
    sep()


# ---------------------------------------------------------------------------
# Closed positions
# ---------------------------------------------------------------------------

def show_closed_positions(conn: sqlite3.Connection, since: Optional[str], limit: int) -> None:
    q = "SELECT * FROM positions WHERE status = 'closed'"
    params = []
    if since:
        q += " AND exit_time >= ?"
        params.append(since)
    q += " ORDER BY exit_time DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(q, params).fetchall()

    sep()
    print(f"  CLOSED POSITIONS (last {limit})")
    sep()

    if not rows:
        print("  No closed positions.")
        sep()
        return

    total_pnl = 0
    wins = 0

    print(f"\n  {'#':<4} {'Side':<5} {'City':<10} {'Bracket':<22} {'Buy':<5} {'Exit':<5} {'P&L':<10} {'Reason':<20} {'Closed'}")
    print("  " + "-" * 100)

    for i, p in enumerate(rows, 1):
        pnl = p["realized_pnl_cents"] or 0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        pnl_str = f"{fmt_cents(pnl)}"

        print(
            f"  {i:<4} {p['side']:<5} {p['city']:<10} {p['bracket_label']:<22} "
            f"{fmt_price(p['entry_price_cents']):<5} {fmt_price(p['exit_price_cents']):<5} "
            f"{pnl_str:<10} {(p['exit_reason'] or '—'):<20} {fmt_time(p['exit_time'])}"
        )

    print(f"\n  Net P&L: {fmt_cents(total_pnl)}   Win rate: {wins}/{len(rows)} ({wins/len(rows)*100:.0f}%)")
    sep()


# ---------------------------------------------------------------------------
# Trade evaluations
# ---------------------------------------------------------------------------

def show_evaluations(conn: sqlite3.Connection, since: Optional[str], limit: int) -> None:
    q = "SELECT * FROM trade_evaluations"
    params = []
    if since:
        q += " WHERE evaluation_time >= ?"
        params.append(since)
    q += " ORDER BY evaluation_time DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(q, params).fetchall()

    # Summary counts
    summary = conn.execute(
        "SELECT action, COUNT(*) as n FROM trade_evaluations GROUP BY action"
    ).fetchall()
    skip_reasons = conn.execute(
        "SELECT skip_reason, COUNT(*) as n FROM trade_evaluations WHERE action = 'SKIP' GROUP BY skip_reason ORDER BY n DESC"
    ).fetchall()

    sep()
    print(f"  TRADE EVALUATIONS")
    sep()

    print("\n  Actions:")
    for row in summary:
        print(f"    {row['action']:<15} {row['n']:>5}x")

    if skip_reasons:
        print("\n  Skip Reasons:")
        for row in skip_reasons:
            reason = row["skip_reason"] or "(none)"
            print(f"    {reason:<50} {row['n']:>5}x")

    print(f"\n  Recent Evaluations (last {min(limit, len(rows))}):")
    print(f"\n  {'Action':<10} {'City':<10} {'Bracket':<22} {'Temp':>6} {'Dist':>6} {'YES':>5} {'NO':>5} {'Skip Reason'}")
    print("  " + "-" * 100)

    for r in rows:
        dist = r["bracket_distance_f"]
        dist_str = f"{dist:+.1f}°" if dist is not None else "—"
        print(
            f"  {r['action']:<10} {r['city']:<10} {r['bracket_label']:<22} "
            f"{r['current_temp_f']:>5.1f}° {dist_str:>6} "
            f"{fmt_price(r['yes_price_cents']):>5} {fmt_price(r['no_price_cents']):>5}  "
            f"{r['skip_reason'] or ''}"
        )

    sep()


# ---------------------------------------------------------------------------
# Crossing history (legacy info)
# ---------------------------------------------------------------------------

def show_crossings(conn: sqlite3.Connection, since: Optional[str]) -> None:
    q = "SELECT * FROM bracket_crossings"
    params = []
    if since:
        q += " WHERE signal_time >= ?"
        params.append(since)
    q += " ORDER BY signal_time DESC LIMIT 20"

    rows = conn.execute(q, params).fetchall()

    sep()
    print(f"  BRACKET CROSSINGS (last 20)")
    sep()

    if not rows:
        print("  No crossings recorded.")
        sep()
        return

    print(f"\n  {'City':<10} {'From':<22} {'To':<22} {'Temp':>6} {'Conf':<8} {'Time'}")
    print("  " + "-" * 90)
    for r in rows:
        old_label = r["old_bracket_label"] or "—"
        print(
            f"  {r['city']:<10} {old_label:<22} {r['new_bracket_label']:<22} "
            f"{r['observed_temp_f']:>5.1f}° {r['confidence']:<8} {fmt_time(r['signal_time'])}"
        )
    sep()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="View Kalshi weather bot results")
    parser.add_argument("--db", default="data/weather_bot.db", help="Database path")
    parser.add_argument("--today", action="store_true", help="Filter to today")
    parser.add_argument("--week", action="store_true", help="Filter to last 7 days")
    parser.add_argument("--positions", action="store_true", help="Show open positions")
    parser.add_argument("--closed", action="store_true", help="Show closed positions + P&L")
    parser.add_argument("--evals", action="store_true", help="Show trade evaluations")
    parser.add_argument("--crossings", action="store_true", help="Show bracket crossings")
    parser.add_argument("--all", dest="show_all", action="store_true", help="Show everything")
    parser.add_argument("--limit", type=int, default=30, help="Row limit for detailed views")
    args = parser.parse_args()

    conn = connect(args.db)

    # Time filter
    since = None
    if args.today:
        since = datetime.now(timezone.utc).date().isoformat()
    elif args.week:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    show_all = args.show_all or not any([
        args.positions, args.closed, args.evals, args.crossings
    ])

    if show_all or True:  # Always show portfolio summary
        show_portfolio(conn, since)

    if show_all or args.positions:
        show_open_positions(conn)

    if show_all or args.closed:
        show_closed_positions(conn, since, args.limit)

    if show_all or args.evals:
        show_evaluations(conn, since, args.limit)

    if args.crossings or args.show_all:
        show_crossings(conn, since)

    conn.close()


if __name__ == "__main__":
    main()
