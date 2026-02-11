"""
Standalone validation of core bot logic.
Runs with stdlib only — no external dependencies.

Usage: python3 validate.py
"""

import sys
import os
import tempfile
from datetime import datetime, date, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.metar_client import (
    TemperatureReading,
    celsius_to_fahrenheit,
    _has_tenths_precision,
)
from engine.bracket_tracker import (
    BracketSet,
    CityTracker,
    Confidence,
)
from storage.db import Database

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} — {detail}")


def make_reading(temp_c, hour=12, icao="KMDW", has_tenths=True):
    if has_tenths:
        t_val = int(abs(temp_c) * 10)
        sign = "1" if temp_c < 0 else "0"
        raw = f"{icao} 100000Z 36008KT 10SM CLR RMK AO2 T{sign}{t_val:03d}0050"
    else:
        raw = f"{icao} 100000Z 36008KT 10SM CLR {int(temp_c):02d}/09 A3025 RMK AO2"
    return TemperatureReading(
        station_icao=icao,
        observation_time=datetime(2026, 2, 10, hour, 0, tzinfo=timezone.utc),
        temp_celsius=temp_c,
        temp_fahrenheit=celsius_to_fahrenheit(temp_c),
        raw_metar=raw,
        celsius_precision_tenths=has_tenths,
    )


def make_tracker(center_f=66.0):
    tracker = CityTracker(city="chicago", station_icao="KMDW")
    brackets = BracketSet.from_center(
        center_f=center_f,
        event_date=date(2026, 2, 10),
        city="chicago",
        series_ticker="KXHIGHCHI",
    )
    tracker.set_brackets(brackets)
    tracker.reset_day(date(2026, 2, 10))
    return tracker


# === C->F Conversion ===
print("\n=== C→F Conversion ===")
check("freezing point", celsius_to_fahrenheit(0.0) == 32.0)
check("boiling point", celsius_to_fahrenheit(100.0) == 212.0)
check("-40 invariant", celsius_to_fahrenheit(-40.0) == -40.0)
check("18.3°C = ~64.94°F", abs(celsius_to_fahrenheit(18.3) - 64.94) < 0.01,
      f"got {celsius_to_fahrenheit(18.3)}")
check("18.0°C = 64.4°F", abs(celsius_to_fahrenheit(18.0) - 64.4) < 0.01,
      f"got {celsius_to_fahrenheit(18.0)}")

# === Precision Detection ===
print("\n=== Precision Detection ===")
r_tenths = make_reading(17.2, has_tenths=True)
check("T-group -> tenths precision", r_tenths.celsius_precision_tenths is True)
check("tenths uncertainty < 0.1F", r_tenths.fahrenheit_uncertainty < 0.1)

r_whole = make_reading(18.0, has_tenths=False)
check("no T-group -> whole precision", r_whole.celsius_precision_tenths is False)
check("whole uncertainty > 0.5F", r_whole.fahrenheit_uncertainty > 0.5)

# === Bracket Structure ===
print("\n=== Bracket Structure ===")
bs = BracketSet.from_center(center_f=66.0, event_date=date(2026, 2, 10),
                            city="chicago", series_ticker="KXHIGHCHI")
check("6 brackets", len(bs.brackets) == 6)
check("bracket 0 open-ended low", bs.brackets[0].lower_f is None)
check("bracket 0 upper = 62", bs.brackets[0].upper_f == 62.0,
      f"got {bs.brackets[0].upper_f}")
check("bracket 5 open-ended high",
      bs.brackets[5].upper_f is None and bs.brackets[5].lower_f == 70.0)

check("50F -> bracket 0", bs.find_bracket(50.0).index == 0)
check("63F -> bracket 1", bs.find_bracket(63.0).index == 1)
check("65F -> bracket 2", bs.find_bracket(65.0).index == 2)
check("66F -> bracket 3", bs.find_bracket(66.0).index == 3)
check("69F -> bracket 4", bs.find_bracket(69.0).index == 4)
check("75F -> bracket 5", bs.find_bracket(75.0).index == 5)
check("64F -> bracket 2 (inclusive lower)", bs.find_bracket(64.0).index == 2,
      f"got bracket {bs.find_bracket(64.0).index}")

# === Bracket Crossing Detection ===
print("\n=== Bracket Crossing Detection ===")

tracker = make_tracker()
c1 = tracker.process_reading(make_reading(18.3, hour=12))  # 64.94F
check("first reading produces crossing", c1 is not None)
check("first reading is_first_reading", c1.is_first_reading)
check("first reading -> bracket 2", c1.new_bracket.index == 2,
      f"got bracket {c1.new_bracket.index}")

c2 = tracker.process_reading(make_reading(18.5, hour=13))  # 65.3F rounds to 65
check("same bracket -> no crossing", c2 is None)

c3 = tracker.process_reading(make_reading(19.5, hour=14))  # 67.1F -> bracket 3
check("upward crossing detected", c3 is not None)
check("old=2 new=3", c3.old_bracket.index == 2 and c3.new_bracket.index == 3,
      f"got {c3.old_bracket.index} -> {c3.new_bracket.index}")

c4 = tracker.process_reading(make_reading(16.0, hour=15))  # 60.8F below max
check("temp drop -> no crossing", c4 is None)
check("daily max unchanged", tracker.daily_max > 67.0, f"got {tracker.daily_max}")

# Multi-bracket jump
tracker2 = make_tracker()
tracker2.process_reading(make_reading(17.0, hour=10))  # 62.6F -> bracket 1
c5 = tracker2.process_reading(make_reading(20.5, hour=14))  # 68.9F -> bracket 4
check("multi-bracket jump detected", c5 is not None)
check("jump: 1 -> 4", c5.old_bracket.index == 1 and c5.new_bracket.index == 4,
      f"got {c5.old_bracket.index} -> {c5.new_bracket.index}")

# === Confidence Assessment ===
print("\n=== Confidence Assessment ===")

tracker3 = make_tracker()
c6 = tracker3.process_reading(make_reading(19.0, hour=12, has_tenths=True))  # 66.2F
check("centered + tenths -> HIGH", c6.confidence == Confidence.HIGH)

tracker4 = make_tracker()
r_boundary = TemperatureReading(
    station_icao="KMDW",
    observation_time=datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc),
    temp_celsius=18.0,  # 64.4F, 0.4F from the 64 boundary
    temp_fahrenheit=64.4,
    raw_metar="KMDW 100000Z 36008KT 10SM CLR 18/09 A3025 RMK AO2",
    celsius_precision_tenths=False,  # +/-0.9F uncertainty
)
c7 = tracker4.process_reading(r_boundary)
check("near boundary + whole -> LOW", c7.confidence == Confidence.LOW,
      f"got {c7.confidence}")

# === NWS Rounding ===
print("\n=== NWS Rounding ===")
check("18.6C rounds to 65F", round(celsius_to_fahrenheit(18.6)) == 65)
check("18.1C rounds to 65F", round(celsius_to_fahrenheit(18.1)) == 65)

# === METAR Response Parsing ===
print("\n=== METAR Response Parsing ===")
obs = {
    "icaoId": "KMDW",
    "obsTime": 1707580800,
    "temp": 5.6,
    "rawOb": "KMDW 110000Z 36008KT 10SM FEW250 06/M05 A3025 RMK AO2 T00560050",
}
r = TemperatureReading.from_api_response(obs)
check("parses valid response", r is not None)
check("correct ICAO", r.station_icao == "KMDW")
check("correct temp C", r.temp_celsius == 5.6)
check("correct temp F", abs(r.temp_fahrenheit - 42.08) < 0.01,
      f"got {r.temp_fahrenheit}")
check("detected T-group", r.celsius_precision_tenths is True)

obs_bad = {"icaoId": "KMDW", "obsTime": 1707580800}
check("missing temp -> None", TemperatureReading.from_api_response(obs_bad) is None)

# === SQLite Storage ===
print("\n=== SQLite Storage ===")
db_path = os.path.join(tempfile.mkdtemp(), "test.db")
db = Database(db_path)
db.connect()

row_id = db.record_observation(make_reading(18.3, hour=12))
check("observation stored", row_id >= 1)

tracker5 = make_tracker()
crossing = tracker5.process_reading(make_reading(18.3, hour=12))
cid = db.record_crossing(crossing)
check("crossing stored", cid >= 1)

recent = db.get_recent_crossings(limit=5)
check("retrieve crossings", len(recent) == 1)
check("crossing city correct", recent[0]["city"] == "chicago")

db.close()
os.unlink(db_path)

# === Results ===
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(1 if failed > 0 else 0)
