"""
Kalshi API client for reading weather market data.

Public market data (prices, orderbook) requires no authentication.
We use this to:
1. Discover today's bracket definitions for each city
2. Fetch current prices when a bracket crossing is detected

The Kalshi API base: https://api.elections.kalshi.com/trade-api/v2
(Despite the "elections" subdomain, this serves ALL markets.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from config.settings import KALSHI, STATIONS, City, StationConfig
from engine.bracket_tracker import Bracket, BracketSet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketPrice:
    """Current price snapshot for a single Kalshi bracket market."""

    ticker: str
    title: str
    yes_price_cents: int  # 0-100
    no_price_cents: int  # 0-100
    volume: int
    open_interest: int


class KalshiClient:
    """
    Reads public market data from Kalshi's API.

    Usage:
        client = KalshiClient()
        brackets = await client.fetch_brackets(City.NYC)
        price = await client.fetch_price("KXHIGHNY-26FEB11-B3")
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=KALSHI.timeout_seconds,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def fetch_brackets(
        self, city: City, target_date: Optional[date] = None
    ) -> Optional[BracketSet]:
        """
        Fetch today's bracket definitions for a city from Kalshi.

        Returns a BracketSet with the actual bracket boundaries and tickers,
        or None if no active market is found.
        """
        config = STATIONS[city]
        url = KALSHI.markets_url(config.kalshi_series)
        logger.debug("Fetching Kalshi brackets: %s", url)

        client = await self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Kalshi API error for %s: %s", city.value, e)
            return None

        markets = data.get("markets", [])
        if not markets:
            logger.warning("No active markets found for %s", config.kalshi_series)
            return None

        # Parse market titles to extract bracket boundaries
        # Market titles follow patterns like:
        #   "Highest temperature in NYC on Feb 11: 62°F to 63°F"
        #   "Highest temperature in NYC on Feb 11: Below 60°F"
        #   "Highest temperature in NYC on Feb 11: 70°F or above"
        brackets = []
        for i, market in enumerate(markets):
            bracket = self._parse_market_to_bracket(market, i)
            if bracket:
                brackets.append(bracket)

        if not brackets:
            logger.warning("Could not parse any brackets for %s", city.value)
            return None

        # Sort by index
        brackets.sort(key=lambda b: b.index)

        event_date = target_date or date.today()
        bracket_set = BracketSet(
            brackets=brackets, event_date=event_date, city=city.value
        )

        logger.info(
            "%s: Loaded %d brackets from Kalshi — %s",
            city.value,
            len(brackets),
            [b.label for b in brackets],
        )
        return bracket_set

    async def fetch_market_price(self, ticker: str) -> Optional[MarketPrice]:
        """Fetch the current price for a specific market ticker."""
        url = f"{KALSHI.base_url}/markets/{ticker}"
        client = await self._get_client()

        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Kalshi price fetch error for %s: %s", ticker, e)
            return None

        market = data.get("market", {})
        # Kalshi uses yes_ask/no_ask for buy prices (what we'd pay)
        # Use last_price as fallback, then mid-price
        yes_price = market.get("yes_ask") or market.get("last_price") or 0
        no_price = market.get("no_ask") or (100 - yes_price) if yes_price > 0 else 0

        return MarketPrice(
            ticker=market.get("ticker", ticker),
            title=market.get("title", ""),
            yes_price_cents=yes_price,
            no_price_cents=no_price,
            volume=market.get("volume", 0),
            open_interest=market.get("open_interest", 0),
        )

    async def fetch_prices_for_series(
        self, series_ticker: str
    ) -> dict[str, MarketPrice]:
        """Fetch current prices for all markets in a series."""
        url = KALSHI.markets_url(series_ticker)
        client = await self._get_client()

        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Kalshi series fetch error for %s: %s", series_ticker, e)
            return {}

        prices = {}
        for market in data.get("markets", []):
            ticker = market.get("ticker", "")
            # Kalshi uses yes_ask/no_ask for buy prices (what we'd pay)
            yes_price = market.get("yes_ask") or market.get("last_price") or 0
            no_price = market.get("no_ask") or (100 - yes_price) if yes_price > 0 else 0

            prices[ticker] = MarketPrice(
                ticker=ticker,
                title=market.get("title", ""),
                yes_price_cents=yes_price,
                no_price_cents=no_price,
                volume=market.get("volume", 0),
                open_interest=market.get("open_interest", 0),
            )

        return prices

    def _parse_market_to_bracket(
        self, market: dict, fallback_index: int
    ) -> Optional[Bracket]:
        """
        Parse a Kalshi market JSON object into a Bracket.

        We extract bracket boundaries from the market title and floor/ceiling
        fields. The exact API shape may vary — this handles the known patterns
        and logs warnings for unexpected formats.
        """
        ticker = market.get("ticker", "")
        title = market.get("title", "")

        # Try to extract from structured fields first
        floor = market.get("floor_strike")
        ceiling = market.get("cap_strike")

        # Kalshi uses strike values in some market types
        if floor is not None or ceiling is not None:
            return Bracket(
                index=fallback_index,
                lower_f=float(floor) if floor is not None else None,
                upper_f=float(ceiling) if ceiling is not None else None,
                ticker=ticker,
            )

        # Fallback: parse from title string
        # This is fragile and should be replaced once we observe the actual
        # API response format. Logging the title helps us iterate.
        logger.debug("Parsing bracket from title: %s", title)

        lower_f = None
        upper_f = None

        title_lower = title.lower()
        if "below" in title_lower or "under" in title_lower:
            # Open-ended low bracket
            upper_f = self._extract_temp_from_title(title)
        elif "above" in title_lower or "or more" in title_lower:
            # Open-ended high bracket
            lower_f = self._extract_temp_from_title(title)
        elif "to" in title_lower or "-" in title_lower:
            # Range bracket — extract both bounds
            temps = self._extract_range_from_title(title)
            if temps:
                lower_f, upper_f = temps

        if lower_f is None and upper_f is None:
            logger.warning("Could not parse bracket from: %s", title)
            return None

        return Bracket(
            index=fallback_index,
            lower_f=lower_f,
            upper_f=upper_f,
            ticker=ticker,
        )

    @staticmethod
    def _extract_temp_from_title(title: str) -> Optional[float]:
        """Extract a single temperature value from a market title."""
        import re

        match = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*F", title)
        if match:
            return float(match.group(1))
        return None

    @staticmethod
    def _extract_range_from_title(title: str) -> Optional[tuple[float, float]]:
        """Extract a temperature range from a market title."""
        import re

        # Patterns like "62°F to 63°F" or "62-63°F"
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*°?\s*F?\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s*°?\s*F",
            title,
        )
        if match:
            return float(match.group(1)), float(match.group(2))
        return None

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
