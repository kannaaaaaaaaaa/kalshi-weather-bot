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
from engine.portfolio import Portfolio
from engine.trading_strategy import TradingStrategy
from engine.position_manager import PositionManager
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
        self.portfolio = Portfolio.create(
            starting_capital_dollars=TRADING.starting_capital_dollars
        )
        self.strategy = TradingStrategy()
        self.position_manager = PositionManager(
            portfolio=self.portfolio,
            db=self.db,
            kalshi=self.kalshi,
            strategy=self.strategy,
        )

        self._running = False
        self._current_date: dict[str, date] = {}  # per-city current date
        self._cycle_count = 0
        self._trading_cycle_count = 0
        self._total_crossings = 0
        self._total_trades = 0

    async def start(self, once: bool = False) -> None:
        """Start the bot. If once=True, run a single cycle and exit."""
        self.db.connect()
        self._running = True

        logger.info("=" * 60)
        logger.info("Kalshi Weather Bot starting (Two-Sided Trading Mode)")
        logger.info("Stations: %s", [s.icao for s in STATIONS.values()])
        logger.info("METAR poll: %ds | Trading eval: %ds",
                   AVIATION_WEATHER.poll_interval_seconds,
                   TRADING.evaluation_interval_seconds)
        logger.info("Starting capital: $%.2f", TRADING.starting_capital_dollars)
        logger.info("=" * 60)

        # Load initial brackets
        await self._load_brackets_for_all()

        # Restore open positions from previous run
        self.position_manager.load_portfolio_from_db()

        if once:
            # Single cycle mode - run both once
            await self._run_metar_cycle()
            await self._run_trading_cycle()
        else:
            # Normal mode - run both loops concurrently
            await asyncio.gather(
                self._metar_loop(),
                self._trading_loop(),
            )

        await self._shutdown()

    async def _metar_loop(self) -> None:
        """METAR polling loop - fetches temperature data."""
        while self._running:
            try:
                await self._run_metar_cycle()
            except Exception:
                logger.exception("Error in METAR polling cycle")

            if self._running:
                await asyncio.sleep(AVIATION_WEATHER.poll_interval_seconds)

    async def _run_metar_cycle(self) -> None:
        """Execute one METAR poll-and-update cycle."""
        self._cycle_count += 1
        logger.debug("--- METAR Cycle %d ---", self._cycle_count)

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

        # 4. Process through bracket tracker (for state tracking)
        crossings = self.tracker.process_readings(readings)

        # 5. Log crossings (but don't trade on them directly anymore)
        for crossing in crossings:
            self._total_crossings += 1
            self.db.record_crossing(crossing)
            logger.info(
                "Bracket crossing detected: %s %s → %s",
                crossing.city,
                crossing.old_bracket.label if crossing.old_bracket else "None",
                crossing.new_bracket.label,
            )

        # 6. Status output
        if self._cycle_count % 10 == 0:
            self._print_metar_status()

    async def _trading_loop(self) -> None:
        """Trading evaluation loop - evaluates all markets and manages positions."""
        while self._running:
            try:
                await self._run_trading_cycle()
            except Exception:
                logger.exception("Error in trading cycle")

            if self._running:
                await asyncio.sleep(TRADING.evaluation_interval_seconds)

    async def _run_trading_cycle(self) -> None:
        """Execute one trading evaluation cycle."""
        self._trading_cycle_count += 1
        logger.debug("--- Trading Cycle %d ---", self._trading_cycle_count)

        # 1. Get current temperature state
        state = self.tracker.get_status()

        # 2. For each city, evaluate all brackets
        for city, config in STATIONS.items():
            if not TRADING.enable_two_sided_trading:
                continue

            await self._evaluate_city_opportunities(city, config, state)

        # 3. Check existing positions for exits
        if TRADING.enable_active_exits:
            await self._manage_open_positions()

        # 4. Snapshot portfolio periodically
        if self._trading_cycle_count % 10 == 0:
            self.db.record_portfolio_snapshot(self.portfolio)
            self._print_trading_status()

    async def _evaluate_city_opportunities(
        self,
        city: City,
        config,
        state: dict,
    ) -> None:
        """Evaluate all brackets for a city and execute trades."""
        city_state = state.get(config.icao)
        if not city_state:
            return

        daily_max_f = city_state.get("daily_max_f")
        if daily_max_f is None:
            # No temperature data yet
            return

        current_bracket_label = city_state.get("current_bracket")
        if not current_bracket_label:
            return

        # Fetch all bracket prices for this city
        prices = await self.kalshi.fetch_prices_for_series(config.kalshi_series)
        if not prices:
            logger.debug("No prices available for %s", config.kalshi_series)
            return

        # Get local time for this city
        from datetime import datetime
        local_time = datetime.now(config.lst_tz)

        # Get bracket set for this city
        tracker = self.tracker._trackers.get(config.icao)
        if not tracker or not tracker.bracket_set:
            return

        current_bracket = tracker.current_bracket
        if not current_bracket:
            return

        # Evaluate each bracket
        for bracket in tracker.bracket_set.brackets:
            # Get price for this bracket
            price = prices.get(bracket.ticker)
            if not price:
                continue

            # Calculate distance
            distance = bracket.lower_f - daily_max_f if bracket.lower_f else 0

            # Evaluate opportunity
            signal = self.strategy.evaluate_bracket_opportunity(
                bracket=bracket,
                current_temp_f=daily_max_f,
                daily_max_f=daily_max_f,
                current_bracket_index=current_bracket.index,
                local_time=local_time,
                market_price=price,
                portfolio=self.portfolio,
            )

            # Log evaluation
            self.db.record_trade_evaluation(
                city=city.value,
                ticker=bracket.ticker,
                bracket_label=bracket.label,
                current_temp_f=daily_max_f,
                bracket_distance_f=distance,
                time_of_day_local=local_time.strftime("%H:%M"),
                yes_price_cents=price.yes_price_cents,
                no_price_cents=price.no_price_cents,
                action=signal.action,
                skip_reason=signal.skip_reason,
            )

            # Execute if signal says to trade
            if signal.action in ("BUY_YES", "BUY_NO"):
                position_id = await self.position_manager.open_position(signal)
                if position_id:
                    self._total_trades += 1

    async def _manage_open_positions(self) -> None:
        """Check all open positions for exit conditions."""
        exits = await self.position_manager.check_position_exits()

        for position_id, reason, exit_price in exits:
            await self.position_manager.close_position(
                position_id=position_id,
                exit_price_cents=exit_price,
                exit_reason=reason,
            )

    # NOTE: Old _handle_crossing method removed - now using continuous evaluation
    # in _trading_loop instead of reactive bracket crossing approach

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

    def _print_metar_status(self) -> None:
        """Print METAR status."""
        status = self.tracker.get_status()
        logger.info("=" * 50)
        logger.info("METAR Status — Cycle %d | Crossings: %d", self._cycle_count, self._total_crossings)
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

    def _print_trading_status(self) -> None:
        """Print trading status."""
        summary = self.portfolio.get_summary()
        logger.info("=" * 50)
        logger.info(
            "Trading Status — Cycle %d | Trades: %d | Open Positions: %d",
            self._trading_cycle_count,
            self._total_trades,
            summary["open_positions"],
        )
        logger.info(
            "  Capital: $%.2f | Cash: $%.2f | P&L: $%+.2f (%.1f%%)",
            summary["total_capital"],
            summary["current_cash"],
            summary["realized_pnl"],
            summary["roi_percent"],
        )
        logger.info(
            "  Win Rate: %.1f%% (%d/%d)",
            summary["win_rate"],
            summary["winning_trades"],
            summary["total_trades"] if summary["total_trades"] > 0 else 1,
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
