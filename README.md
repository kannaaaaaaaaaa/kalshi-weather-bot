# Kalshi Weather Bot

A Python bot for paper-trading Kalshi daily high temperature markets using live METAR observations.

It continuously:
1. Pulls latest METAR readings for configured stations.
2. Tracks each city's running daily high.
3. Detects when the running high crosses into a new Kalshi temperature bracket.
4. Fetches current Kalshi market prices for the crossed-into bracket.
5. Logs a simulated trade decision (`BUY_YES` or `SKIP`) to SQLite.

## Project Goals

- Build a reliable signal pipeline for weather market bracket crossings.
- Measure real-world latency from observation timestamp to signal generation.
- Capture every decision in an append-only database for later edge analysis.
- Stay in paper-trading mode until data quality, settlement logic, and execution assumptions are validated.

## Implementation Overview

### Core Components

- `main.py`
  - Orchestrates the poll -> detect -> evaluate -> log loop.
  - Handles one-shot mode (`--once`) and continuous mode.
  - Coordinates startup, status logging, and graceful shutdown.

- `data/metar_client.py`
  - Async METAR client for `aviationweather.gov`.
  - Parses observations into `TemperatureReading`.
  - Tracks Celsius precision (tenths vs whole degree) and Fahrenheit uncertainty.

- `engine/bracket_tracker.py`
  - Maintains per-city daily high state.
  - Assigns NWS-rounded temperatures into bracket definitions.
  - Emits `BracketCrossing` signals with confidence (`high` or `low`).

- `exchange/kalshi_client.py`
  - Reads public Kalshi market data (no auth in current implementation).
  - Loads bracket definitions and fetches live market prices.

- `storage/db.py`
  - SQLite persistence layer.
  - Stores observations, bracket crossings, and paper-trade decisions.
  - Uses append-only tables with timestamps for auditability.

- `config/settings.py`
  - City/station mapping, API settings, and paper-trading parameters.

## End-to-End Flow

1. On startup, load bracket definitions per city from Kalshi.
2. Every poll interval (default 60s), fetch METAR data for configured ICAO stations.
3. Store all observations.
4. For each station, update the running daily max and detect bracket transitions.
5. On crossing:
   - skip if confidence is low and strict confidence is enabled,
   - skip if first reading of day,
   - fetch current Kalshi price for the new bracket,
   - `BUY_YES` if `yes_price_cents < max_buy_price_cents`, else skip.
6. Persist crossing and trade decision with metadata (price, volume, latency, reason).

## Data Model (SQLite)

Database file: `data/weather_bot.db`

- `observations`
  - Raw ingest of METAR-derived readings.
- `bracket_crossings`
  - Signal events when daily high enters a new bracket.
- `paper_trades`
  - Simulated actions and skip reasons tied to crossing IDs.

## Configuration

Key settings live in `config/settings.py`:

- `STATIONS`
  - Maps each city to ICAO source and Kalshi series ticker.
- `AVIATION_WEATHER`
  - METAR endpoint, polling interval, timeout, user-agent.
- `KALSHI`
  - Kalshi API base URL and timeout.
- `TRADING`
  - Paper-trading logic:
    - `max_buy_price_cents` (default `90`)
    - `require_high_confidence` (default `True`)
    - `default_position_size` (default `10`)

## Setup

### 1. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Run once (single cycle)

```powershell
python main.py --once
```

### 4. Run continuously

```powershell
python main.py
```

### Optional flags

```powershell
python main.py --verbose
python main.py --db data/weather_bot.db
```

## Testing and Validation

- Pytest suite:

```powershell
pytest -v test_core.py
```

- Standalone validation script:

```powershell
python validate.py
```

## Current Assumptions and Known Gaps

- Paper trading only: no live order placement is implemented.
- Some station/series mappings are marked TODO in `config/settings.py` and should be verified.
- Day-boundary handling for Local Standard Time vs DST is partially implemented and explicitly marked for refinement.
- Bracket parsing from market titles is fallback logic and may need hardening against API format changes.
- Python's `round()` behavior (banker's rounding) may differ from official NWS settlement rounding in tie cases.

## Viewing Paper Trading Results

The bot stores all decisions in SQLite. You have multiple ways to view results:

### 1. Command-line viewer (Quick)

```powershell
# View all-time summary
python view_results.py

# View today's results only
python view_results.py --today

# View last 7 days
python view_results.py --week

# Show detailed trade history
python view_results.py --detailed
```

### 2. Web Dashboard (Visual)

Start the web server:
```powershell
python web_dashboard.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

The dashboard shows:
- Overall performance metrics
- Total investment and potential profit
- Per-city breakdown
- Recent trades with full details
- Auto-refreshes every 60 seconds

### 3. Direct SQL queries

```powershell
sqlite3 data/weather_bot.db
```

```sql
-- See all trades
SELECT * FROM paper_trades ORDER BY trade_time DESC LIMIT 10;

-- Summary by city
SELECT city, COUNT(*) as trades,
       SUM(potential_profit_cents)/100.0 as potential_profit
FROM paper_trades
WHERE action = 'BUY_YES'
GROUP BY city;
```

## Running for Extended Periods

### Run for a week

```powershell
# Windows PowerShell
.\run_bot_week.ps1
```

Or manually:
```powershell
python main.py
```

The bot will:
- Run continuously until stopped (Ctrl+C)
- Poll METAR data every 60 seconds (configurable in `config/settings.py`)
- Log all activity to console
- Store every observation and decision in `data/weather_bot.db`
- Automatically handle day transitions

### As a background service (Advanced)

For longer runs, consider using a process manager:

**Windows (NSSM)**:
```powershell
# Download NSSM, then:
nssm install KalshiWeatherBot "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\main.py"
nssm start KalshiWeatherBot
```

**Linux/Mac (systemd or tmux)**:
```bash
# Using tmux
tmux new -s weather-bot
python main.py
# Detach with Ctrl+B, D
```

## Suggested Next Steps

- Validate all Kalshi series tickers and station mappings against live markets.
- Implement exact NWS-compatible rounding and day-boundary logic.
- Add integration tests with recorded Kalshi/METAR API payloads.
- Introduce risk controls and execution module before any live trading path.
- Analyze week-long paper trading results to validate edge hypothesis.
