"""
METAR data client for aviationweather.gov.

Fetches decoded METAR observations and extracts temperature data
with precision tracking for the C→F conversion problem.

The aviationweather.gov API returns JSON with pre-decoded fields:
  - temp: temperature in °C (float, tenths precision in standard METAR)
  - obsTime: observation epoch timestamp
  - icaoId: station identifier
  - rawOb: raw METAR string (for verification)

Key design decision: We track both the raw Celsius value and the
converted Fahrenheit value, along with a confidence flag for whether
the conversion introduces bracket ambiguity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config.settings import AVIATION_WEATHER, STATIONS, City, StationConfig

logger = logging.getLogger(__name__)

# httpx is imported lazily in MetarClient so the data models
# can be used without it (e.g., for testing, backtesting).
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


@dataclass(frozen=True)
class TemperatureReading:
    """A single temperature observation from a METAR station."""

    station_icao: str
    observation_time: datetime  # UTC, from the METAR
    temp_celsius: float  # As reported (tenths precision for standard METAR)
    temp_fahrenheit: float  # Converted: (C * 9/5) + 32
    raw_metar: str  # Original METAR string for audit trail

    # Precision tracking for the C→F conversion problem.
    # Standard METAR: tenths of °C → ±0.1°C → ±0.18°F (high precision)
    # HF-ASOS 1-min: whole °C → ±0.5°C → ±0.9°F (low precision)
    celsius_precision_tenths: bool  # True if tenths precision, False if whole degrees

    @property
    def fahrenheit_uncertainty(self) -> float:
        """Max uncertainty in °F due to Celsius rounding."""
        if self.celsius_precision_tenths:
            # ±0.05°C → ±0.09°F — negligible for 2°F brackets
            return 0.09
        else:
            # ±0.5°C → ±0.9°F — can span a 2°F bracket boundary
            return 0.9

    @classmethod
    def from_api_response(cls, obs: dict) -> Optional[TemperatureReading]:
        """
        Parse a single observation from the aviationweather.gov JSON response.

        Expected shape (from their docs + real responses):
        {
            "icaoId": "KMDW",
            "obsTime": 1707600000,   // epoch seconds
            "temp": 5.6,             // °C
            "rawOb": "KMDW 110551Z ...",
            ...
        }
        """
        try:
            icao = obs.get("icaoId", "")
            temp_c = obs.get("temp")
            obs_time_epoch = obs.get("obsTime")
            raw_ob = obs.get("rawOb", "")

            if temp_c is None or obs_time_epoch is None:
                logger.warning("Missing temp or obsTime for station %s", icao)
                return None

            temp_c = float(temp_c)
            obs_time = datetime.fromtimestamp(obs_time_epoch, tz=timezone.utc)

            # Determine precision: if the API returns a value with no decimal
            # (e.g., 18.0 vs 18.3), it might be whole-degree.
            # However, standard METAR always has tenths — the API just may
            # truncate trailing zeros. We check the raw METAR string.
            has_tenths = _has_tenths_precision(temp_c, raw_ob)

            temp_f = celsius_to_fahrenheit(temp_c)

            return cls(
                station_icao=icao,
                observation_time=obs_time,
                temp_celsius=temp_c,
                temp_fahrenheit=temp_f,
                raw_metar=raw_ob,
                celsius_precision_tenths=has_tenths,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error("Failed to parse observation: %s — %s", obs, e)
            return None


def celsius_to_fahrenheit(temp_c: float) -> float:
    """
    Convert Celsius to Fahrenheit.

    Note on NWS rounding: The NWS Daily Climate Report converts C→F and
    rounds to the nearest whole °F. We keep full precision here and let
    the bracket tracker handle rounding decisions.
    """
    return (temp_c * 9.0 / 5.0) + 32.0


def _has_tenths_precision(temp_c: float, raw_metar: str) -> bool:
    """
    Determine if a temperature reading has tenths-of-degree precision.

    Standard METAR encodes temperature in the remarks section as TxxYxxYxx
    where the T-group gives tenths of °C. If the raw METAR contains a T-group,
    we have tenths precision regardless of what the API float looks like.

    Example T-group: T01720089 = temp 17.2°C, dewpoint 8.9°C
    """
    if not raw_metar:
        # No raw string to inspect — check if the float has a non-zero decimal
        return (temp_c * 10) % 10 != 0

    # Look for the T-group in remarks (after "RMK")
    rmk_idx = raw_metar.find("RMK")
    if rmk_idx == -1:
        return (temp_c * 10) % 10 != 0

    remarks = raw_metar[rmk_idx:]
    # T-group: T followed by 8 digits (sign+3 digits for temp, sign+3 digits for dewpoint)
    for part in remarks.split():
        if part.startswith("T") and len(part) == 9 and part[1:].isdigit():
            return True

    return (temp_c * 10) % 10 != 0


class MetarClient:
    """
    Fetches METAR observations from aviationweather.gov.

    Usage:
        client = MetarClient()
        readings = await client.fetch_latest()
        for reading in readings:
            print(f"{reading.station_icao}: {reading.temp_fahrenheit:.1f}°F")
    """

    def __init__(self, config: type = AVIATION_WEATHER):
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if httpx is None:
            raise RuntimeError("httpx is required for MetarClient. Install with: pip install httpx")
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._config.timeout_seconds,
                headers={"User-Agent": self._config.user_agent},
            )
        return self._client

    async def fetch_latest(
        self, station_ids: Optional[list[str]] = None
    ) -> list[TemperatureReading]:
        """
        Fetch the most recent METAR for each station.

        Args:
            station_ids: List of ICAO codes. Defaults to all configured stations.

        Returns:
            List of TemperatureReading, one per station that returned data.
            May be fewer than requested if a station has no recent observation.
        """
        if station_ids is None:
            station_ids = [s.icao for s in STATIONS.values()]

        url = self._config.build_url(station_ids)
        logger.debug("Fetching METAR from %s", url)

        client = await self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("METAR API HTTP error: %s", e)
            return []
        except httpx.RequestError as e:
            logger.error("METAR API request failed: %s", e)
            return []

        try:
            observations = response.json()
        except ValueError:
            logger.error("METAR API returned non-JSON response")
            return []

        if not isinstance(observations, list):
            logger.error("METAR API returned unexpected format: %s", type(observations))
            return []

        readings = []
        for obs in observations:
            reading = TemperatureReading.from_api_response(obs)
            if reading is not None:
                readings.append(reading)
                logger.info(
                    "%s: %.1f°C / %.1f°F (obs: %s, precision: %s)",
                    reading.station_icao,
                    reading.temp_celsius,
                    reading.temp_fahrenheit,
                    reading.observation_time.isoformat(),
                    "tenths" if reading.celsius_precision_tenths else "whole",
                )

        logger.info("Fetched %d/%d readings", len(readings), len(station_ids))
        return readings

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
