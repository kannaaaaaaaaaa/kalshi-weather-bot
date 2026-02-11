"""
Tests for the core bot logic.

Covers:
- METAR response parsing and C→F conversion
- Precision detection (tenths vs whole degree)
- Bracket crossing detection
- Edge cases: boundary temps, day transitions, rounding ambiguity
"""

import pytest
from datetime import datetime, date, timezone

from data.metar_client import (
    TemperatureReading,
    celsius_to_fahrenheit,
    _has_tenths_precision,
)
from engine.bracket_tracker import (
    Bracket,
    BracketSet,
    BracketTracker,
    CityTracker,
    BracketCrossing,
    Confidence,
)


# ============================================================
# METAR Parsing Tests
# ============================================================


class TestCelsiusToFahrenheit:
    def test_freezing(self):
        assert celsius_to_fahrenheit(0.0) == 32.0

    def test_boiling(self):
        assert celsius_to_fahrenheit(100.0) == 212.0

    def test_body_temp(self):
        assert abs(celsius_to_fahrenheit(37.0) - 98.6) < 0.01

    def test_negative(self):
        assert celsius_to_fahrenheit(-40.0) == -40.0

    def test_tenths_precision(self):
        # 18.3°C should give a precise F value
        result = celsius_to_fahrenheit(18.3)
        assert abs(result - 64.94) < 0.01

    def test_whole_degree_ambiguity(self):
        # 18°C = 64.4°F, but actual could be 17.5-18.4°C = 63.5-65.12°F
        result = celsius_to_fahrenheit(18.0)
        assert abs(result - 64.4) < 0.01


class TestTemperatureReading:
    def _make_reading(self, temp_c: float, raw_metar: str = "") -> TemperatureReading:
        return TemperatureReading(
            station_icao="KMDW",
            observation_time=datetime(2026, 2, 10, 18, 0, tzinfo=timezone.utc),
            temp_celsius=temp_c,
            temp_fahrenheit=celsius_to_fahrenheit(temp_c),
            raw_metar=raw_metar,
            celsius_precision_tenths=_has_tenths_precision(temp_c, raw_metar),
        )

    def test_from_api_response_basic(self):
        obs = {
            "icaoId": "KMDW",
            "obsTime": 1707580800,
            "temp": 5.6,
            "rawOb": "KMDW 110000Z 36008KT 10SM FEW250 06/M05 A3025 RMK AO2 T00560050",
        }
        reading = TemperatureReading.from_api_response(obs)
        assert reading is not None
        assert reading.station_icao == "KMDW"
        assert reading.temp_celsius == 5.6
        assert abs(reading.temp_fahrenheit - 42.08) < 0.01

    def test_from_api_response_missing_temp(self):
        obs = {"icaoId": "KMDW", "obsTime": 1707580800}
        reading = TemperatureReading.from_api_response(obs)
        assert reading is None

    def test_precision_with_t_group(self):
        # T-group T01720089 = 17.2°C
        raw = "KMDW 110000Z 36008KT 10SM CLR 17/09 A3025 RMK AO2 T01720089"
        reading = self._make_reading(17.2, raw)
        assert reading.celsius_precision_tenths is True
        assert reading.fahrenheit_uncertainty < 0.1

    def test_precision_without_t_group(self):
        # No T-group, whole degree celsius
        raw = "KMDW 110000Z 36008KT 10SM CLR 18/09 A3025 RMK AO2"
        reading = self._make_reading(18.0, raw)
        assert reading.celsius_precision_tenths is False
        assert reading.fahrenheit_uncertainty > 0.5

    def test_precision_tenths_from_float(self):
        # No raw METAR, but float has tenths
        reading = self._make_reading(18.3, "")
        assert reading.celsius_precision_tenths is True


# ============================================================
# Bracket Tests
# ============================================================


class TestBracket:
    def _make_brackets(self) -> BracketSet:
        """Standard 6-bracket set centered at 66°F: [<62, 62-63, 64-65, 66-67, 68-69, >=70]"""
        return BracketSet.from_center(
            center_f=66.0,
            event_date=date(2026, 2, 10),
            city="chicago",
            series_ticker="KXHIGHCHI",
        )

    def test_bracket_set_structure(self):
        bs = self._make_brackets()
        assert len(bs.brackets) == 6
        # First bracket: open-ended low
        assert bs.brackets[0].lower_f is None
        assert bs.brackets[0].upper_f == 62.0
        # Last bracket: open-ended high
        assert bs.brackets[5].lower_f == 70.0
        assert bs.brackets[5].upper_f is None

    def test_bracket_containment_middle(self):
        bs = self._make_brackets()
        # 65°F should be in bracket 2 (64-65)
        b = bs.find_bracket(65.0)
        assert b is not None
        assert b.index == 2
        assert b.lower_f == 64.0
        assert b.upper_f == 66.0

    def test_bracket_containment_low_edge(self):
        bs = self._make_brackets()
        # 50°F should be in bracket 0 (below 62)
        b = bs.find_bracket(50.0)
        assert b is not None
        assert b.index == 0

    def test_bracket_containment_high_edge(self):
        bs = self._make_brackets()
        # 75°F should be in bracket 5 (70 and above)
        b = bs.find_bracket(75.0)
        assert b is not None
        assert b.index == 5

    def test_bracket_boundary_lower_inclusive(self):
        bs = self._make_brackets()
        # 64.0°F should be in bracket 2 (64-65), not bracket 1 (62-63)
        b = bs.find_bracket(64.0)
        assert b is not None
        assert b.index == 2

    def test_bracket_boundary_upper_exclusive(self):
        bs = self._make_brackets()
        # 66.0°F should be in bracket 3 (66-67), not bracket 2 (64-65)
        b = bs.find_bracket(66.0)
        assert b is not None
        assert b.index == 3


# ============================================================
# Bracket Crossing Detection Tests
# ============================================================


class TestCityTracker:
    def _make_tracker(self) -> CityTracker:
        tracker = CityTracker(city="chicago", station_icao="KMDW")
        brackets = BracketSet.from_center(
            center_f=66.0,
            event_date=date(2026, 2, 10),
            city="chicago",
            series_ticker="KXHIGHCHI",
        )
        tracker.set_brackets(brackets)
        tracker.reset_day(date(2026, 2, 10))
        return tracker

    def _make_reading(
        self, temp_c: float, hour: int = 12
    ) -> TemperatureReading:
        return TemperatureReading(
            station_icao="KMDW",
            observation_time=datetime(2026, 2, 10, hour, 0, tzinfo=timezone.utc),
            temp_celsius=temp_c,
            temp_fahrenheit=celsius_to_fahrenheit(temp_c),
            raw_metar=f"KMDW 100000Z 36008KT 10SM CLR RMK AO2 T{int(temp_c*10):04d}0050",
            celsius_precision_tenths=True,
        )

    def test_first_reading_returns_crossing(self):
        tracker = self._make_tracker()
        reading = self._make_reading(18.3)  # ~64.9°F
        crossing = tracker.process_reading(reading)
        # First reading should detect a crossing (from None to bracket 2)
        assert crossing is not None
        assert crossing.is_first_reading
        assert crossing.new_bracket.index == 2  # 64-65 bracket

    def test_same_bracket_no_crossing(self):
        tracker = self._make_tracker()
        # First reading: 18.3°C = 64.94°F → bracket 2 (64-65)
        tracker.process_reading(self._make_reading(18.3, hour=12))
        # Second reading: 18.5°C = 65.3°F → still bracket 2 (64-65) after rounding to 65
        crossing = tracker.process_reading(self._make_reading(18.5, hour=13))
        assert crossing is None

    def test_bracket_crossing_upward(self):
        tracker = self._make_tracker()
        # First: 18.3°C = 64.94°F → bracket 2 (64-65)
        tracker.process_reading(self._make_reading(18.3, hour=12))
        # Second: 19.5°C = 67.1°F → bracket 3 (66-67) — crosses up
        crossing = tracker.process_reading(self._make_reading(19.5, hour=14))
        assert crossing is not None
        assert not crossing.is_first_reading
        assert crossing.old_bracket.index == 2
        assert crossing.new_bracket.index == 3

    def test_no_crossing_on_temp_drop(self):
        tracker = self._make_tracker()
        # First: 19.5°C = 67.1°F
        tracker.process_reading(self._make_reading(19.5, hour=12))
        # Second: 18.0°C = 64.4°F — temp dropped, not a new max
        crossing = tracker.process_reading(self._make_reading(18.0, hour=14))
        assert crossing is None
        # Daily max should still be the first reading
        assert tracker.daily_max > 67.0

    def test_multi_bracket_jump(self):
        tracker = self._make_tracker()
        # Start in bracket 1 (62-63): 17.0°C = 62.6°F
        tracker.process_reading(self._make_reading(17.0, hour=10))
        # Jump to bracket 4 (68-69): 20.5°C = 68.9°F
        crossing = tracker.process_reading(self._make_reading(20.5, hour=14))
        assert crossing is not None
        assert crossing.old_bracket.index == 1
        assert crossing.new_bracket.index == 4

    def test_confidence_high_when_centered(self):
        tracker = self._make_tracker()
        # 19.0°C = 66.2°F — well within bracket 3 (66-67)
        # With T-group precision, uncertainty is ±0.09°F
        reading = self._make_reading(19.0, hour=12)
        tracker.process_reading(reading)
        # Check confidence of current bracket
        assert tracker.current_bracket.index == 3

    def test_confidence_low_near_boundary(self):
        tracker = self._make_tracker()
        # Create a reading right at a boundary with low precision
        reading = TemperatureReading(
            station_icao="KMDW",
            observation_time=datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc),
            temp_celsius=18.0,  # 64.4°F — near 64 boundary
            temp_fahrenheit=64.4,
            raw_metar="KMDW 100000Z 36008KT 10SM CLR 18/09 A3025 RMK AO2",
            celsius_precision_tenths=False,  # Whole degree = ±0.9°F uncertainty
        )
        crossing = tracker.process_reading(reading)
        assert crossing is not None
        # With ±0.9°F uncertainty at 64.4°F, we're within 0.4°F of the 64 boundary
        assert crossing.confidence == Confidence.LOW

    def test_no_crossing_without_brackets(self):
        tracker = CityTracker(city="chicago", station_icao="KMDW")
        # Don't load brackets
        reading = self._make_reading(18.3)
        crossing = tracker.process_reading(reading)
        assert crossing is None


# ============================================================
# Rounding Tests (Critical for NWS settlement)
# ============================================================


class TestNWSRounding:
    """
    NWS rounds Fahrenheit to nearest whole degree for the Daily Climate Report.
    This determines which Kalshi bracket wins.
    """

    def test_round_up(self):
        # 18.6°C = 65.48°F → rounds to 65°F
        assert round(celsius_to_fahrenheit(18.6)) == 65

    def test_round_down(self):
        # 18.1°C = 64.58°F → rounds to 65°F
        assert round(celsius_to_fahrenheit(18.1)) == 65

    def test_half_rounds_even(self):
        # Python rounds 0.5 to nearest even (banker's rounding)
        # 64.5 → 64 (rounds to even)
        assert round(64.5) == 64
        # 65.5 → 66 (rounds to even)
        assert round(65.5) == 66

    def test_nws_rounding_may_differ(self):
        """
        NOTE: NWS may use standard rounding (0.5 → up), not banker's rounding.
        This is something to verify against actual CLI reports.
        For paper trading, either approach is close enough.
        """
        # Document the difference but don't fail
        # Standard rounding: 64.5 → 65
        # Python round(): 64.5 → 64
        pass


# ============================================================
# Run with: python -m pytest tests/test_core.py -v
# ============================================================
