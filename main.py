"""
Main event loop for the Kalshi weather trading bot.

Runs continuously:
  1. Poll METAR data for all stations
  2. Store observations
  3. Check for bracket crossings
  4. On crossing: fetch Kalshi prices, evaluate, log paper trade
  5. Sleep and repeat

Usage:
    python main.py
    python main.py --once    # Single poll cycle (for testing)
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime, date, timezone

from config.settings import (
    AVIATION_WEATHER,
    STATIONS,
    TRADING,
    City,
)
from data.metar_client import MetarClient, TemperatureReading
from engine.bracket_tracker import (
    BracketTracker,
    BracketCrossing,
    BracketSet,
    Confidence,
)
from exchange.kalshi_client import KalshiClient
from storage.db import Database

logger = logging.getLogger(__name__)


class WeatherBot:
    """
    Orchestrates the polling loop and decision flow.

    Responsibilities:
    - Manage the poll → detect → evaluate → log cycle
    - Handle day transitions (reset daily max, load new brackets)
    - Provide status output for monitoring
    """

    def __init__(self, db_path: str = "data/weather_bot.db"):
        self.metar = MetarClient()
        self.kalshi = KalshiClient()
        self.tracker = BracketTracker()
        self.db = Database(db_path)

        self._running = False
        self._current_date: dict[str, date] = {}  # per-city current date
        self._cycle_count = 0
        self._total_crossings = 0
        self._total_trades = 0

    async def start(self, once: bool = False) -> None:
        """Start the bot. If once=True, run a single cycle and exit."""
        self.db.connect()
        self._running = True

        logger.info("=" * 60)
        logger.info("Kalshi Weather Bot starting")
        logger.info("Stations: %s", [s.icao for s in STATIONS.values()])
        logger.info("Poll interval: %ds", AVIATION_WEATHER.poll_interval_seconds)
        logger.info("=" * 60)

        # Load initial brackets
        await self._load_brackets_for_all()

        if once:
            await self._run_cycle()
        else:
            await self._run_loop()

        await self._shutdown()

    async def _run_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._run_cycle()
            except Exception:
                logger.exception("Error in polling cycle")

            if self._running:
                await asyncio.sleep(AVIATION_WEATHER.poll_interval_seconds)

    async def _run_cycle(self) -> None:
        """Execute one poll-detect-evaluate cycle."""
        self._cycle_count += 1
        logger.debug("--- Cycle %d ---", self._cycle_count)

        # 1. Fetch METAR data
        readings = await self.metar.fetch_latest()
        if not readings:
            logger.warning("No readings returned this cycle")
            return

        # 2. Store observations
        for reading in readings:
            self.db.record_observation(reading)

        # 3. Check for day transitions
        self._check_day_transitions(readings)

        # 4. Process through bracket tracker
        crossings = self.tracker.process_readings(readings)

        # 5. For each crossing, evaluate and log
        for crossing in crossings:
            self._total_crossings += 1
            await self._handle_crossing(crossing)

        # 6. Status output
        if self._cycle_count % 10 == 0:
            self._print_status()

    async def _handle_crossing(self, crossing: BracketCrossing) -> None:
        """Evaluate a bracket crossing and log the paper trade decision."""
        crossing_id = self.db.record_crossing(crossing)

        # Skip low-confidence crossings if configured to do so
        if TRADING.require_high_confidence and crossing.confidence == Confidence.LOW:
            self.db.record_paper_trade(
                crossing_id=crossing_id,
                crossing=crossing,
                action="SKIP",
                skip_reason="low_confidence_rounding_ambiguity",
            )
            logger.info(
                "SKIP %s — low confidence (rounding ambiguity)",
                crossing.city,
            )
            return

        # Skip first readings (no old bracket to compare against)
        if crossing.is_first_reading:
            self.db.record_paper_trade(
                crossing_id=crossing_id,
                crossing=crossing,
                action="SKIP",
                skip_reason="first_reading_of_day",
            )
            return

        # Fetch current market price for the new bracket
        price = await self.kalshi.fetch_market_price(crossing.new_bracket.ticker)
        if price is None:
            self.db.record_paper_trade(
                crossing_id=crossing_id,
                crossing=crossing,
                action="SKIP",
                skip_reason="market_price_unavailable",
            )
            logger.warning(
                "SKIP %s — could not fetch market price for %s",
                crossing.city,
                crossing.new_bracket.ticker,
            )
            return

        # Decision: buy YES if price is below threshold
        if price.yes_price_cents < TRADING.max_buy_price_cents:
            self.db.record_paper_trade(
                crossing_id=crossing_id,
                crossing=crossing,
                action="BUY_YES",
                market_yes_price_cents=price.yes_price_cents,
                market_no_price_cents=price.no_price_cents,
                market_volume=price.volume,
                position_size=TRADING.default_position_size,
            )
            self._total_trades += 1
            potential_profit = TRADING.default_position_size * (
                100 - price.yes_price_cents
            )
            logger.info(
                "PAPER TRADE: BUY YES %s %s @ %d¢ — potential profit %d¢ "
                "(latency: %.1fs)",
                crossing.city,
                crossing.new_bracket.label,
                price.yes_price_cents,
                potential_profit,
                crossing.latency_seconds,
            )
        else:
            self.db.record_paper_trade(
                crossing_id=crossing_id,
                crossing=crossing,
                action="SKIP",
                skip_reason=f"price_too_high_{price.yes_price_cents}c",
                market_yes_price_cents=price.yes_price_cents,
                market_no_price_cents=price.no_price_cents,
                market_volume=price.volume,
            )
            logger.info(
                "SKIP %s — YES price %d¢ >= threshold %d¢",
                crossing.city,
                price.yes_price_cents,
                TRADING.max_buy_price_cents,
            )

    async def _load_brackets_for_all(self) -> None:
        """Load today's bracket definitions from Kalshi for all cities."""
        for city, config in STATIONS.items():
            try:
                bracket_set = await self.kalshi.fetch_brackets(city)
                if bracket_set:
                    self.tracker.load_brackets(config.icao, bracket_set)
                else:
                    # Fallback: generate estimated brackets
                    # In production, this should fail loudly. For paper trading,
                    # we log and continue with estimated brackets.
                    logger.warning(
                        "%s: No Kalshi brackets — using estimated set "
                        "(centered at 65°F). UPDATE THIS.",
                        city.value,
                    )
                    fallback = BracketSet.from_center(
                        center_f=65.0,
                        event_date=date.today(),
                        city=city.value,
                        series_ticker=config.kalshi_series,
                    )
                    self.tracker.load_brackets(config.icao, fallback)
            except Exception:
                logger.exception("Failed to load brackets for %s", city.value)

    def _check_day_transitions(self, readings: list[TemperatureReading]) -> None:
        """
        Check if any station has crossed into a new LST day.

        This is where the DST nuance matters: NWS uses Local Standard Time,
        so during DST the "day" boundary is at 1:00 AM local (which is
        midnight LST).
        """
        for reading in readings:
            config = None
            for c in STATIONS.values():
                if c.icao == reading.station_icao:
                    config = c
                    break
            if config is None:
                continue

            # Convert observation time to LST to determine the "NWS day"
            # For now, we use the station's standard timezone.
            # TODO: Handle DST properly — the NWS day boundary is midnight LST,
            # which is 1 AM during DST. For paper trading, using UTC date is
            # acceptable; refine before going live.
            obs_date = reading.observation_time.date()

            if self._current_date.get(reading.station_icao) != obs_date:
                old_date = self._current_date.get(reading.station_icao)
                self._current_date[reading.station_icao] = obs_date
                self.tracker.reset_day(reading.station_icao, obs_date)
                if old_date:
                    logger.info(
                        "%s: Day transition %s → %s",
                        reading.station_icao,
                        old_date,
                        obs_date,
                    )

    def _print_status(self) -> None:
        """Print current bot status."""
        status = self.tracker.get_status()
        logger.info("=" * 50)
        logger.info(
            "Status — Cycle %d | Crossings: %d | Trades: %d",
            self._cycle_count,
            self._total_crossings,
            self._total_trades,
        )
        for icao, s in status.items():
            logger.info(
                "  %s (%s): max=%.1f°F bracket=%s obs=%d",
                icao,
                s["city"],
                s["daily_max_f"] or 0,
                s["current_bracket"] or "—",
                s["observations"],
            )
        logger.info("=" * 50)

    def stop(self) -> None:
        """Signal the bot to stop."""
        logger.info("Stop requested")
        self._running = False

    async def _shutdown(self) -> None:
        """Clean up resources."""
        await self.metar.close()
        await self.kalshi.close()
        self.db.close()
        logger.info("Shutdown complete")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="Kalshi Weather Trading Bot")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--db", default="data/weather_bot.db", help="Database path")
    args = parser.parse_args()

    setup_logging(args.verbose)

    bot = WeatherBot(db_path=args.db)

    # Handle Ctrl+C gracefully
    loop = asyncio.new_event_loop()

    def handle_signal():
        bot.stop()

    # Signal handlers are only supported on Unix systems
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)

    try:
        loop.run_until_complete(bot.start(once=args.once))
    except KeyboardInterrupt:
        # On Windows (and as fallback on Unix), handle Ctrl+C via exception
        bot.stop()
    finally:
        loop.close()


if __name__ == "__main__":
    main()
