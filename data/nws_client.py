"""
NWS (National Weather Service) forecast client.

Fetches today's official high temperature forecast for each city from
api.weather.gov. The NWS forecast is the single best publicly available
predictor of the Kalshi daily-high settlement temperature, since Kalshi
settles against the NWS Daily Climate Report.

API flow:
  1. GET /points/{lat},{lon}          -> resolves the NWS grid office + grid x/y
  2. GET /gridpoints/{wfo}/{x},{y}/forecast -> daily forecast with high temps

Grid info is stable (doesn't change) so we cache it across the bot lifetime.
Forecasts are cached for NWSConfig.cache_ttl_seconds (default: 30 min).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config.settings import NWS, STATIONS, City

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


@dataclass(frozen=True)
class ForecastData:
    """Today's NWS high temperature forecast for one city."""

    city: str
    forecast_high_f: float   # Predicted daily high in °F
    forecast_low_f: float    # Predicted daily low in °F (for context)
    period_name: str         # e.g. "Today", "This Afternoon"
    fetched_at: datetime     # When this forecast was retrieved (UTC)

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()


@dataclass
class _GridInfo:
    """Cached NWS grid parameters for a location."""
    forecast_url: str


class NWSForecastClient:
    """
    Fetches NWS high-temperature forecasts for all configured cities.

    Usage:
        client = NWSForecastClient()
        forecasts = await client.fetch_all()
        forecast = forecasts.get(City.NYC)
        if forecast:
            print(f"NYC high: {forecast.forecast_high_f}°F")
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        # Stable per-city grid info — fetched once, cached forever
        self._grid_cache: dict[City, _GridInfo] = {}
        # Forecast cache — refreshed every cache_ttl_seconds
        self._forecast_cache: dict[City, ForecastData] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if httpx is None:
            raise RuntimeError("httpx is required. Install with: pip install httpx")
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=NWS.timeout_seconds,
                headers={
                    "User-Agent": NWS.user_agent,
                    "Accept": "application/geo+json",
                },
            )
        return self._client

    async def fetch_all(self) -> dict[City, ForecastData]:
        """
        Fetch today's high forecast for all configured cities.

        Returns a dict mapping City -> ForecastData. Cities where the
        fetch fails are omitted (caller should handle None gracefully).
        """
        results = {}
        for city, config in STATIONS.items():
            if config.lat == 0.0 and config.lon == 0.0:
                logger.warning("%s: No lat/lon configured, skipping NWS fetch", city.value)
                continue

            forecast = await self.fetch_city(city)
            if forecast:
                results[city] = forecast

        return results

    async def fetch_city(self, city: City) -> Optional[ForecastData]:
        """
        Fetch today's high forecast for a single city.

        Returns cached result if still fresh, otherwise re-fetches.
        """
        cached = self._forecast_cache.get(city)
        if cached and cached.age_seconds < NWS.cache_ttl_seconds:
            logger.debug(
                "%s: Using cached NWS forecast (age %.0fs): %.1f°F",
                city.value,
                cached.age_seconds,
                cached.forecast_high_f,
            )
            return cached

        config = STATIONS[city]

        # Resolve grid info if not cached
        grid = self._grid_cache.get(city)
        if grid is None:
            grid = await self._fetch_grid_info(city, config.lat, config.lon)
            if grid is None:
                return None
            self._grid_cache[city] = grid

        # Fetch forecast
        forecast = await self._fetch_forecast(city, grid.forecast_url)
        if forecast:
            self._forecast_cache[city] = forecast
            logger.info(
                "%s: NWS forecast high = %.1f°F (%s)",
                city.value,
                forecast.forecast_high_f,
                forecast.period_name,
            )

        return forecast

    async def _fetch_grid_info(
        self, city: City, lat: float, lon: float
    ) -> Optional[_GridInfo]:
        """Call /points/{lat},{lon} to resolve the NWS grid forecast URL."""
        url = f"{NWS.base_url}/points/{lat:.4f},{lon:.4f}"
        logger.debug("%s: Resolving NWS grid via %s", city.value, url)

        client = await self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("%s: NWS /points request failed: %s", city.value, e)
            return None

        forecast_url = (
            data.get("properties", {}).get("forecast")
        )
        if not forecast_url:
            logger.error("%s: No forecast URL in /points response", city.value)
            return None

        return _GridInfo(forecast_url=forecast_url)

    async def _fetch_forecast(
        self, city: City, forecast_url: str
    ) -> Optional[ForecastData]:
        """Fetch the daily forecast and extract today's high temperature."""
        logger.debug("%s: Fetching NWS forecast from %s", city.value, forecast_url)

        client = await self._get_client()
        try:
            response = await client.get(forecast_url)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("%s: NWS forecast request failed: %s", city.value, e)
            return None

        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            logger.error("%s: No forecast periods in NWS response", city.value)
            return None

        # NWS returns alternating daytime/nighttime periods.
        # The first daytime period is today's daytime high.
        # "isDaytime": true -> contains the high; false -> contains the low.
        high_period = None
        low_period = None

        for period in periods[:4]:  # Only look at the next 4 periods (2 days)
            if period.get("isDaytime") and high_period is None:
                high_period = period
            elif not period.get("isDaytime") and low_period is None:
                low_period = period

        if high_period is None:
            # All periods are nighttime (happens after ~6 PM local) — use first
            high_period = periods[0]

        temp = high_period.get("temperature")
        temp_unit = high_period.get("temperatureUnit", "F")
        period_name = high_period.get("name", "Today")

        if temp is None:
            logger.error("%s: No temperature in NWS forecast period", city.value)
            return None

        # Convert to Fahrenheit if NWS returns Celsius (rare but possible)
        if temp_unit == "C":
            high_f = temp * 9.0 / 5.0 + 32.0
        else:
            high_f = float(temp)

        # Extract tonight's low for context
        low_f = high_f - 15.0  # fallback estimate
        if low_period:
            low_temp = low_period.get("temperature")
            if low_temp is not None:
                low_unit = low_period.get("temperatureUnit", "F")
                low_f = float(low_temp) if low_unit == "F" else low_temp * 9.0 / 5.0 + 32.0

        return ForecastData(
            city=city.value,
            forecast_high_f=high_f,
            forecast_low_f=low_f,
            period_name=period_name,
            fetched_at=datetime.now(timezone.utc),
        )

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
