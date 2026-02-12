"""
Simple web dashboard for viewing paper trading results.

Usage:
    python web_dashboard.py

Then open http://localhost:5000 in your browser.
"""

from flask import Flask, render_template_string, jsonify
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

app = Flask(__name__)
DB_PATH = "data/weather_bot.db"


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_summary_stats():
    """Get overall summary statistics."""
    db = get_db()

    overall = db.execute("""
        SELECT
            COUNT(*) as total_decisions,
            SUM(CASE WHEN action = 'BUY_YES' THEN 1 ELSE 0 END) as total_buys,
            SUM(CASE WHEN action = 'SKIP' THEN 1 ELSE 0 END) as total_skips,
            SUM(CASE WHEN action = 'BUY_YES' THEN entry_cost_cents ELSE 0 END) as total_cost_cents,
            SUM(CASE WHEN action = 'BUY_YES' THEN potential_profit_cents ELSE 0 END) as total_potential_profit,
            SUM(CASE WHEN action = 'BUY_YES' THEN position_size ELSE 0 END) as total_contracts,
            AVG(CASE WHEN action = 'BUY_YES' THEN market_yes_price_cents END) as avg_entry_price
        FROM paper_trades
    """).fetchone()

    by_city = db.execute("""
        SELECT
            city,
            COUNT(*) as decisions,
            SUM(CASE WHEN action = 'BUY_YES' THEN 1 ELSE 0 END) as buys,
            SUM(CASE WHEN action = 'BUY_YES' THEN potential_profit_cents ELSE 0 END) as potential_profit
        FROM paper_trades
        GROUP BY city
        ORDER BY buys DESC
    """).fetchall()

    recent_trades = db.execute("""
        SELECT
            pt.trade_time,
            pt.city,
            pt.bracket_label,
            pt.action,
            pt.market_yes_price_cents,
            pt.position_size,
            pt.potential_profit_cents,
            bc.confidence,
            bc.latency_seconds
        FROM paper_trades pt
        JOIN bracket_crossings bc ON pt.crossing_id = bc.id
        WHERE pt.action = 'BUY_YES'
        ORDER BY pt.trade_time DESC
        LIMIT 10
    """).fetchall()

    db.close()

    return {
        'overall': dict(overall),
        'by_city': [dict(row) for row in by_city],
        'recent_trades': [dict(row) for row in recent_trades]
    }


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Weather Bot - Paper Trading Dashboard</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        .header {
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            text-align: center;
        }

        .header h1 {
            color: #333;
            margin-bottom: 10px;
        }

        .header .subtitle {
            color: #666;
            font-size: 14px;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }

        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }

        .stat-card h3 {
            color: #666;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .stat-card .value {
            color: #333;
            font-size: 32px;
            font-weight: 700;
            margin-bottom: 5px;
        }

        .stat-card .subvalue {
            color: #888;
            font-size: 14px;
        }

        .stat-card.profit .value {
            color: #10b981;
        }

        .stat-card.cost .value {
            color: #f59e0b;
        }

        .section {
            background: white;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }

        .section h2 {
            color: #333;
            margin-bottom: 20px;
            font-size: 20px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th {
            background: #f3f4f6;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #666;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        td {
            padding: 12px;
            border-bottom: 1px solid #f3f4f6;
            color: #333;
        }

        tr:hover {
            background: #f9fafb;
        }

        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }

        .badge.buy {
            background: #d1fae5;
            color: #065f46;
        }

        .badge.skip {
            background: #fee2e2;
            color: #991b1b;
        }

        .badge.high-conf {
            background: #dbeafe;
            color: #1e40af;
        }

        .profit {
            color: #10b981;
            font-weight: 600;
        }

        .refresh-btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            margin-top: 10px;
        }

        .refresh-btn:hover {
            background: #5568d3;
        }

        .last-updated {
            text-align: center;
            color: white;
            margin-top: 20px;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Kalshi Weather Bot</h1>
            <p class="subtitle">Paper Trading Dashboard</p>
            <button class="refresh-btn" onclick="location.reload()">🔄 Refresh</button>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <h3>Total Decisions</h3>
                <div class="value">{{ stats.overall.total_decisions }}</div>
                <div class="subvalue">All time</div>
            </div>

            <div class="stat-card">
                <h3>Trades Executed</h3>
                <div class="value">{{ stats.overall.total_buys }}</div>
                <div class="subvalue">{{ "%.1f"|format((stats.overall.total_buys / (stats.overall.total_decisions or 1) * 100)) }}% execution rate</div>
            </div>

            <div class="stat-card profit">
                <h3>Potential Profit</h3>
                <div class="value">${{ "%.2f"|format((stats.overall.total_potential_profit or 0) / 100) }}</div>
                <div class="subvalue">If all trades won</div>
            </div>

            <div class="stat-card cost">
                <h3>Total Investment</h3>
                <div class="value">${{ "%.2f"|format((stats.overall.total_cost_cents or 0) / 100) }}</div>
                <div class="subvalue">{{ stats.overall.total_contracts }} contracts</div>
            </div>
        </div>

        <div class="section">
            <h2>🌆 Performance by City</h2>
            <table>
                <thead>
                    <tr>
                        <th>City</th>
                        <th>Decisions</th>
                        <th>Trades</th>
                        <th>Execution %</th>
                        <th>Potential Profit</th>
                    </tr>
                </thead>
                <tbody>
                    {% for city in stats.by_city %}
                    <tr>
                        <td><strong>{{ city.city }}</strong></td>
                        <td>{{ city.decisions }}</td>
                        <td>{{ city.buys }}</td>
                        <td>{{ "%.1f"|format((city.buys / (city.decisions or 1) * 100)) }}%</td>
                        <td class="profit">${{ "%.2f"|format((city.potential_profit or 0) / 100) }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>📈 Recent Trades</h2>
            <table>
                <thead>
                    <tr>
                        <th>Time (UTC)</th>
                        <th>City</th>
                        <th>Bracket</th>
                        <th>Price</th>
                        <th>Size</th>
                        <th>Max Profit</th>
                        <th>Confidence</th>
                        <th>Latency</th>
                    </tr>
                </thead>
                <tbody>
                    {% for trade in stats.recent_trades %}
                    <tr>
                        <td>{{ trade.trade_time[:19] }}</td>
                        <td>{{ trade.city }}</td>
                        <td>{{ trade.bracket_label }}</td>
                        <td>{{ trade.market_yes_price_cents }}¢</td>
                        <td>{{ trade.position_size }}</td>
                        <td class="profit">${{ "%.2f"|format((trade.potential_profit_cents or 0) / 100) }}</td>
                        <td><span class="badge high-conf">{{ trade.confidence }}</span></td>
                        <td>{{ "%.1f"|format(trade.latency_seconds) }}s</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="last-updated">
            Last updated: {{ now }}
        </div>
    </div>

    <script>
        // Auto-refresh every 60 seconds
        setTimeout(() => location.reload(), 60000);
    </script>
</body>
</html>
"""


@app.route('/')
def dashboard():
    """Main dashboard view."""
    if not Path(DB_PATH).exists():
        return "Database not found. Please run the bot first to generate data.", 404

    stats = get_summary_stats()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    return render_template_string(HTML_TEMPLATE, stats=stats, now=now)


@app.route('/api/stats')
def api_stats():
    """API endpoint for stats (for future use)."""
    stats = get_summary_stats()
    return jsonify(stats)


if __name__ == '__main__':
    print("=" * 60)
    print("  Kalshi Weather Bot - Web Dashboard")
    print("=" * 60)
    print("\n  Starting server...")
    print("  Open your browser to: http://localhost:5000")
    print("\n  Press Ctrl+C to stop")
    print("=" * 60)

    app.run(debug=True, host='0.0.0.0', port=5000)
