"""
Configuration for the Kalshi weather trading bot.

All city/station mappings, API endpoints, and operational parameters live here.
"""

from dataclasses import dataclass, field
from enum import Enum
from datetime import timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

# Timezone helpers — fall back to fixed UTC offsets if tzdata is missing
def _tz(name: str, utc_offset_hours: int):
    """Try ZoneInfo first, fall back to fixed offset."""
    try:
        if ZoneInfo is not None:
            return ZoneInfo(name)
    except Exception:
        pass
    return timezone(timedelta(hours=utc_offset_hours))


class City(Enum):
    NYC = "nyc"
    CHICAGO = "chicago"
    MIAMI = "miami"
    AUSTIN = "austin"


@dataclass(frozen=True)
class StationConfig:
    """Maps a Kalshi weather market to its data source."""

    city: City
    icao: str  # METAR station identifier
    kalshi_series: str  # Kalshi series ticker for daily high temp
    timezone: object  # ZoneInfo or fixed UTC offset — for LST day boundary calcs
    description: str
    lat: float = 0.0   # WGS84 latitude for NWS forecast lookups
    lon: float = 0.0   # WGS84 longitude (negative = west)

    @property
    def lst_tz(self):
        """
        Kalshi settles on Local Standard Time, NOT daylight saving.
        During DST, the daily high window is 1:00 AM to 12:59 AM next day (local clock).
        We need standard time for day boundary calculations.
        """
        return self.timezone


# Station definitions — these are the resolution sources Kalshi uses
STATIONS: dict[City, StationConfig] = {
    City.NYC: StationConfig(
        city=City.NYC,
        icao="KNYC",  # Central Park — may not be standard ASOS; needs verification
        kalshi_series="KXHIGHNY",
        timezone=_tz("US/Eastern", -5),
        description="Central Park, New York",
        lat=40.7812,
        lon=-73.9665,
    ),
    City.CHICAGO: StationConfig(
        city=City.CHICAGO,
        icao="KMDW",  # Midway Airport
        kalshi_series="KXHIGHCHI",
        timezone=_tz("US/Central", -6),
        description="Midway Airport, Chicago",
        lat=41.7868,
        lon=-87.7522,
    ),
    City.MIAMI: StationConfig(
        city=City.MIAMI,
        icao="KMIA",  # Miami International Airport
        kalshi_series="KXHIGHMIA",
        timezone=_tz("US/Eastern", -5),
        description="Miami International Airport",
        lat=25.7959,
        lon=-80.2870,
    ),
    City.AUSTIN: StationConfig(
        city=City.AUSTIN,
        icao="KAUS",  # Austin-Bergstrom International Airport
        kalshi_series="KXHIGHAUS",
        timezone=_tz("US/Central", -6),
        description="Austin-Bergstrom International Airport",
        lat=30.1945,
        lon=-97.6699,
    ),
}


# --- API Configuration ---

@dataclass(frozen=True)
class NWSConfig:
    """NWS (National Weather Service) forecast API settings."""

    base_url: str = "https://api.weather.gov"
    timeout_seconds: int = 15
    # How long to cache a forecast before re-fetching (seconds).
    # NWS updates forecasts roughly every 1-6 hours, so 30 min is safe.
    cache_ttl_seconds: int = 1800
    user_agent: str = "KalshiWeatherBot/0.1 (research; contact@example.com)"


@dataclass(frozen=True)
class AviationWeatherConfig:
    """aviationweather.gov METAR API settings."""

    base_url: str = "https://aviationweather.gov/api/data/metar"
    format: str = "json"
    # Rate limit: 100 requests/min. We poll every 60s with 1 request = very safe.
    poll_interval_seconds: int = 60
    # Request timeout
    timeout_seconds: int = 10
    # Custom user agent to avoid automated filtering (per their docs)
    user_agent: str = "KalshiWeatherBot/0.1 (research; contact@example.com)"

    def build_url(self, station_ids: list[str]) -> str:
        """Build the METAR request URL for given stations."""
        ids = ",".join(station_ids)
        return f"{self.base_url}?ids={ids}&format={self.format}"


@dataclass(frozen=True)
class KalshiConfig:
    """Kalshi API settings (public market data, no auth needed)."""

    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    timeout_seconds: int = 10

    def markets_url(self, series_ticker: str) -> str:
        return f"{self.base_url}/markets?series_ticker={series_ticker}&status=open"

    def event_url(self, event_ticker: str) -> str:
        return f"{self.base_url}/events/{event_ticker}"


# --- Operational Parameters ---

@dataclass
class TradingConfig:
    """Paper trading parameters."""

    # Starting capital for paper trading (in dollars)
    starting_capital_dollars: float = 100.0

    # Only generate BUY signal if the YES price for the crossed-into bracket
    # is below this threshold (in cents). e.g., 90 means buy if < $0.90.
    max_buy_price_cents: int = 90

    # Minimum confidence to act on a bracket crossing.
    # "high" = temp clearly in one bracket; "low" = rounding ambiguity.
    require_high_confidence: bool = True

    # Position sizing strategy
    # - 'fixed': Use default_position_size contracts per trade
    # - 'risk_pct': Calculate based on risk_percent_per_trade
    position_sizing_mode: str = 'risk_pct'

    # Simulated position size per trade (number of contracts) - used if mode='fixed'
    default_position_size: int = 10

    # Maximum % of portfolio to risk per trade (used if mode='risk_pct')
    risk_percent_per_trade: float = 2.0

    # Maximum contracts per trade (hard limit)
    max_contracts_per_trade: int = 20

    # Two-sided trading parameters
    enable_two_sided_trading: bool = True
    enable_active_exits: bool = True

    # Entry thresholds for YES positions
    yes_entry_current_bracket_max: int = 80  # Buy YES if < 80¢ for current bracket
    yes_entry_adjacent_bracket_max: int = 60  # Buy YES if < 60¢ for adjacent bracket
    yes_entry_far_bracket_max: int = 50  # Buy YES if < 50¢ for far brackets

    # Entry thresholds for NO positions
    no_entry_far_bracket_max: int = 70  # Buy NO if < 70¢ for far brackets (YES > 30¢)
    no_entry_crossed_bracket_max: int = 80  # Buy NO if < 80¢ for crossed brackets

    # Exit thresholds
    take_profit_cents: int = 15  # Exit if +15¢ gain
    take_profit_percent: float = 25.0  # OR +25% ROI (only if gain >= take_profit_min_cents)
    take_profit_min_cents: int = 5  # Minimum absolute gain before % exit triggers
    stop_loss_percent: float = 30.0  # Exit if -30% loss
    lock_profit_price: int = 90  # Exit if price reaches 90¢+

    # Position limits (risk management)
    max_positions_per_city: int = 5
    max_total_positions: int = 20
    max_position_pct_of_portfolio: float = 50.0  # Max % of portfolio in single position
    max_deployed_capital_pct: float = 80.0  # Keep 20% cash reserve

    # Trading frequency
    evaluation_interval_seconds: int = 30  # Check opportunities every 30s

    # Daily risk limits
    max_trades_per_day: int = 20
    daily_loss_limit_pct: float = 20.0

    # Data quality thresholds
    max_data_staleness_seconds: int = 600  # 10 minutes
    min_market_volume: int = 100  # Skip if volume < 100
    max_spread_cents: int = 20  # Skip if spread > 20¢

    # --- Improved signal quality filters ---

    # Minimum entry prices — never fight the market at near-zero prices.
    # If the market says 1-14c, there's a 85-99% chance we're wrong.
    min_yes_entry_cents: int = 15  # Skip YES entries cheaper than 15¢
    min_no_entry_cents: int = 10   # Skip NO entries cheaper than 10¢

    # Market divergence threshold — if our probability estimate differs from
    # the market-implied probability by more than this many percentage points,
    # the market knows something we don't. Skip the trade.
    # Tightened from 30 to 20 — the market is usually right.
    max_market_divergence_pct: float = 20.0

    # Hard stop: no new position entries after this local hour.
    # After 4 PM the warming phase is over and the daily high is nearly set.
    no_new_entries_after_hour: int = 16

    # No entries before this local hour — overnight temps are meaningless
    # for predicting the daily high. Wait until warming is underway.
    no_new_entries_before_hour: int = 10


# --- Database ---

DB_PATH = "data/weather_bot.db"


# --- Convenience ---

AVIATION_WEATHER = AviationWeatherConfig()
KALSHI = KalshiConfig()
NWS = NWSConfig()
TRADING = TradingConfig()

ALL_ICAO_IDS = [s.icao for s in STATIONS.values()]
