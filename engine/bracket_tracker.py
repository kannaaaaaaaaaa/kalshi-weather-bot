"""
Bracket tracker for Kalshi weather markets.

Tracks the running daily high temperature per city and detects when
a new observation causes the daily max to cross into a different
Kalshi temperature bracket.

Kalshi bracket structure (6 brackets per market):
  - Bracket 0: below lower_bound (open-ended low)
  - Brackets 1-4: 2°F wide each (e.g., 62-63, 64-65, 66-67, 68-69)
  - Bracket 5: above upper_bound (open-ended high)

Settlement: based on the NWS Daily Climate Report, which rounds to
whole °F. We track both the precise observation and the NWS-rounded
value for accurate bracket assignment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from enum import Enum
from typing import Optional

from data.metar_client import TemperatureReading

logger = logging.getLogger(__name__)


class Confidence(Enum):
    """How confident we are that a bracket assignment is correct."""

    HIGH = "high"  # Temp is clearly within one bracket (not near boundary)
    LOW = "low"  # Temp is near a bracket boundary and C→F rounding could flip it


@dataclass(frozen=True)
class Bracket:
    """A single temperature bracket in a Kalshi weather market."""

    index: int  # 0-5
    lower_f: Optional[float]  # None for the lowest bracket (open-ended)
    upper_f: Optional[float]  # None for the highest bracket (open-ended)
    ticker: str  # Kalshi market ticker for this specific bracket

    @property
    def label(self) -> str:
        if self.lower_f is None:
            return f"Below {self.upper_f:.0f}°F"
        if self.upper_f is None:
            return f"{self.lower_f:.0f}°F and above"
        return f"{self.lower_f:.0f}-{self.upper_f:.0f}°F"

    def contains(self, temp_f: float) -> bool:
        """Check if a temperature falls within this bracket."""
        if self.lower_f is None:
            return temp_f < self.upper_f
        if self.upper_f is None:
            return temp_f >= self.lower_f
        return self.lower_f <= temp_f < self.upper_f


@dataclass(frozen=True)
class BracketSet:
    """The full set of 6 brackets for a day's market."""

    brackets: list[Bracket]
    event_date: date
    city: str

    def find_bracket(self, temp_f: float) -> Optional[Bracket]:
        """Find which bracket a temperature falls into."""
        for bracket in self.brackets:
            if bracket.contains(temp_f):
                return bracket
        logger.error("Temperature %.1f°F doesn't match any bracket", temp_f)
        return None

    @classmethod
    def from_center(
        cls,
        center_f: float,
        event_date: date,
        city: str,
        series_ticker: str,
    ) -> BracketSet:
        """
        Construct a standard 6-bracket set centered around a forecast temp.

        Kalshi's 4 middle brackets are 2°F wide each, centered around the
        forecast. The exact boundaries come from the Kalshi API, but this
        method generates a plausible bracket set for testing/fallback.

        Example with center=66:
          [<62, 62-63, 64-65, 66-67, 68-69, >=70]
        """
        # Middle 4 brackets span 8°F total, centered on the forecast
        base = round(center_f) - 4  # Start 4°F below center

        brackets = []
        # Bracket 0: everything below
        brackets.append(Bracket(
            index=0,
            lower_f=None,
            upper_f=float(base),
            ticker=f"{series_ticker}-B0",  # Placeholder; real tickers come from API
        ))
        # Brackets 1-4: 2°F each
        for i in range(4):
            low = float(base + i * 2)
            high = float(base + (i + 1) * 2)
            brackets.append(Bracket(
                index=i + 1,
                lower_f=low,
                upper_f=high,
                ticker=f"{series_ticker}-B{i + 1}",
            ))
        # Bracket 5: everything above
        brackets.append(Bracket(
            index=5,
            lower_f=float(base + 8),
            upper_f=None,
            ticker=f"{series_ticker}-B5",
        ))

        return cls(brackets=brackets, event_date=event_date, city=city)


@dataclass(frozen=True)
class BracketCrossing:
    """Signal emitted when the daily high crosses into a new bracket."""

    city: str
    station_icao: str
    observation_time: datetime
    signal_time: datetime  # When we detected it (for latency measurement)

    old_bracket: Optional[Bracket]
    new_bracket: Bracket

    observed_temp_f: float
    daily_max_f: float  # Running daily max after this observation
    confidence: Confidence

    @property
    def is_first_reading(self) -> bool:
        """True if this is the first reading of the day (no old bracket)."""
        return self.old_bracket is None

    @property
    def latency_seconds(self) -> float:
        """Time between observation and detection."""
        return (self.signal_time - self.observation_time).total_seconds()


@dataclass
class CityTracker:
    """
    Tracks the running daily high and bracket state for a single city.

    Reset at the LST day boundary. Emits BracketCrossing events when
    the daily max moves into a new bracket.
    """

    city: str
    station_icao: str
    bracket_set: Optional[BracketSet] = None

    # Daily state
    _current_date: Optional[date] = field(default=None, repr=False)
    _daily_max_f: Optional[float] = field(default=None, repr=False)
    _current_bracket: Optional[Bracket] = field(default=None, repr=False)
    _observation_count: int = field(default=0, repr=False)

    def set_brackets(self, bracket_set: BracketSet) -> None:
        """Load today's bracket definitions (from Kalshi API)."""
        self.bracket_set = bracket_set
        logger.info(
            "%s: Loaded brackets for %s — %s",
            self.city,
            bracket_set.event_date,
            [b.label for b in bracket_set.brackets],
        )

    def reset_day(self, new_date: date) -> None:
        """Reset daily tracking state for a new day."""
        logger.info("%s: New day %s — resetting tracker", self.city, new_date)
        self._current_date = new_date
        self._daily_max_f = None
        self._current_bracket = None
        self._observation_count = 0

    def process_reading(self, reading: TemperatureReading) -> Optional[BracketCrossing]:
        """
        Process a new temperature reading. Returns a BracketCrossing if
        the daily max moved into a new bracket, otherwise None.
        """
        if self.bracket_set is None:
            logger.warning("%s: No brackets loaded — skipping", self.city)
            return None

        temp_f = reading.temp_fahrenheit
        now = datetime.now(timezone.utc)
        self._observation_count += 1

        # Check if this is a new daily max
        if self._daily_max_f is not None and temp_f <= self._daily_max_f:
            # Not a new max — no bracket crossing possible
            return None

        # New daily max
        old_max = self._daily_max_f
        self._daily_max_f = temp_f

        # Determine NWS-rounded temperature (they round to nearest whole °F)
        rounded_f = round(temp_f)

        # Find the bracket for the rounded temp
        new_bracket = self.bracket_set.find_bracket(rounded_f)
        if new_bracket is None:
            return None

        # Determine confidence based on proximity to bracket boundary
        confidence = self._assess_confidence(reading, new_bracket)

        # Check if bracket changed
        old_bracket = self._current_bracket
        if new_bracket.index == (old_bracket.index if old_bracket else -1):
            # Same bracket — daily max went up but stayed in same bracket
            self._current_bracket = new_bracket
            return None

        # Bracket crossing detected
        self._current_bracket = new_bracket
        crossing = BracketCrossing(
            city=self.city,
            station_icao=self.station_icao,
            observation_time=reading.observation_time,
            signal_time=now,
            old_bracket=old_bracket,
            new_bracket=new_bracket,
            observed_temp_f=temp_f,
            daily_max_f=self._daily_max_f,
            confidence=confidence,
        )

        logger.info(
            "BRACKET CROSSING: %s — %s → %s (%.1f°F, confidence: %s, "
            "latency: %.1fs)",
            self.city,
            old_bracket.label if old_bracket else "None",
            new_bracket.label,
            temp_f,
            confidence.value,
            crossing.latency_seconds,
        )

        return crossing

    def _assess_confidence(
        self, reading: TemperatureReading, bracket: Bracket
    ) -> Confidence:
        """
        Assess confidence that the temperature is truly in this bracket.

        Low confidence when:
        - The temperature is within the uncertainty range of a bracket boundary
        - The Celsius reading has only whole-degree precision
        """
        temp_f = reading.temp_fahrenheit
        uncertainty = reading.fahrenheit_uncertainty

        # Check distance to nearest bracket boundary
        distances = []
        if bracket.lower_f is not None:
            distances.append(abs(temp_f - bracket.lower_f))
        if bracket.upper_f is not None:
            distances.append(abs(temp_f - bracket.upper_f))

        if not distances:
            return Confidence.HIGH

        min_distance = min(distances)

        # If the uncertainty range overlaps a bracket boundary, low confidence
        if min_distance < uncertainty:
            return Confidence.LOW

        return Confidence.HIGH

    @property
    def daily_max(self) -> Optional[float]:
        return self._daily_max_f

    @property
    def current_bracket(self) -> Optional[Bracket]:
        return self._current_bracket

    @property
    def observation_count(self) -> int:
        return self._observation_count


class BracketTracker:
    """
    Manages bracket tracking across all configured cities.

    Usage:
        tracker = BracketTracker()
        tracker.load_brackets(...)  # From Kalshi API
        crossings = tracker.process_readings(readings)
    """

    def __init__(self):
        from config.settings import STATIONS
        self._trackers: dict[str, CityTracker] = {}
        for city, config in STATIONS.items():
            self._trackers[config.icao] = CityTracker(
                city=config.city.value,
                station_icao=config.icao,
            )

    def load_brackets(self, icao: str, bracket_set: BracketSet) -> None:
        """Load bracket definitions for a station."""
        if icao in self._trackers:
            self._trackers[icao].set_brackets(bracket_set)

    def reset_day(self, icao: str, new_date: date) -> None:
        """Reset day for a specific station."""
        if icao in self._trackers:
            self._trackers[icao].reset_day(new_date)

    def reset_all(self, new_date: date) -> None:
        """Reset all trackers for a new day."""
        for tracker in self._trackers.values():
            tracker.reset_day(new_date)

    def process_readings(
        self, readings: list[TemperatureReading]
    ) -> list[BracketCrossing]:
        """
        Process a batch of readings (one per station).
        Returns any bracket crossings detected.
        """
        crossings = []
        for reading in readings:
            tracker = self._trackers.get(reading.station_icao)
            if tracker is None:
                logger.debug("No tracker for station %s", reading.station_icao)
                continue

            crossing = tracker.process_reading(reading)
            if crossing is not None:
                crossings.append(crossing)

        return crossings

    def get_status(self) -> dict[str, dict]:
        """Get current state of all trackers (for monitoring)."""
        status = {}
        for icao, tracker in self._trackers.items():
            status[icao] = {
                "city": tracker.city,
                "daily_max_f": tracker.daily_max,
                "current_bracket": (
                    tracker.current_bracket.label
                    if tracker.current_bracket
                    else None
                ),
                "observations": tracker.observation_count,
            }
        return status
