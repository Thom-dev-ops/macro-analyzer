"""FRED (Federal Reserve Economic Data) market data provider.

Fetches live macro indicators from the FRED API and converts them
into MarketObservation objects for thesis validation.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime

import httpx

from macro_positioning.core.models import MarketObservation, Thesis
from macro_positioning.core.settings import settings
from macro_positioning.market.fred_history import FredObservation
from macro_positioning.market.providers import MarketDataProvider

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# ---------------------------------------------------------------------------
# Series catalogue – organised by category, matching Urban Kaoberg's dashboard
# ---------------------------------------------------------------------------

RATES_SERIES = {
    "DFEDTARU": ("rates", "Fed Funds Upper Target", "%"),
    "DGS10": ("rates", "10Y Treasury Yield", "%"),
    "DGS2": ("rates", "2Y Treasury Yield", "%"),
    "DGS30": ("rates", "30Y Treasury Yield", "%"),
    "T10Y2Y": ("rates", "10Y-2Y Spread", "bps"),
    "T10YIE": ("rates", "10Y Breakeven Inflation", "%"),
    "DFII10": ("rates", "10Y Real Yield", "%"),
}

INFLATION_SERIES = {
    "CPIAUCSL": ("inflation", "CPI All Urban Consumers", "index"),
    "PPIACO": ("inflation", "PPI All Commodities", "index"),
    "PCEPI": ("inflation", "PCE Price Index", "index"),
    "PCEPILFE": ("inflation", "Core PCE Price Index", "index"),
}

LABOR_SERIES = {
    "UNRATE": ("labor", "Unemployment Rate", "%"),
    "PAYEMS": ("labor", "Nonfarm Payrolls", "thousands"),
    "ICSA": ("labor", "Initial Jobless Claims", "claims"),
    "JTSJOL": ("labor", "JOLTS Job Openings", "thousands"),
}

GROWTH_SERIES = {
    "A191RL1Q225SBEA": ("growth", "Real GDP QoQ Annualised", "%"),
    "INDPRO": ("growth", "Industrial Production Index", "index"),
    "DGORDER": ("growth", "Durable Goods Orders", "millions $"),
    "BOPGSTB": ("growth", "Trade Balance Goods & Services", "millions $"),
}

CONSUMER_SERIES = {
    "RSAFS": ("consumer", "Retail Sales", "millions $"),
    "PI": ("consumer", "Personal Income", "billions $"),
    "PCE": ("consumer", "Personal Consumption Expenditures", "billions $"),
    "UMCSENT": ("consumer", "UMich Consumer Sentiment", "index"),
    "MICH": ("consumer", "UMich Inflation Expectations", "%"),
}

HOUSING_SERIES = {
    "HOUST": ("housing", "Housing Starts", "thousands"),
    "PERMIT": ("housing", "Building Permits", "thousands"),
    "EXHOSLUSM495S": ("housing", "Existing Home Sales", "units"),
    "HSN1F": ("housing", "New Home Sales", "thousands"),
}

FISCAL_SERIES = {
    "MTSDS133FMS": ("fiscal", "Federal Surplus/Deficit", "millions $"),
}

FIN_CONDITIONS_SERIES = {
    "NFCI": ("financial_conditions", "Chicago Fed NFCI", "index"),
    "ANFCI": ("financial_conditions", "Adjusted NFCI", "index"),
    "STLFSI4": ("financial_conditions", "St. Louis Fed FSI", "index"),
    "VIXCLS": ("financial_conditions", "VIX", "index"),
    "DTWEXBGS": ("financial_conditions", "Trade-Weighted USD", "index"),
    "M2SL": ("financial_conditions", "M2 Money Supply", "$B"),
    "DJIA": ("financial_conditions", "Dow Jones Industrial Avg", "index"),
    "DFF": ("financial_conditions", "Effective Fed Funds Rate", "%"),
    "SOFR": ("financial_conditions", "SOFR", "%"),
    "TEDRATE": ("financial_conditions", "TED Spread", "%"),
    "BAMLH0A0HYM2": ("financial_conditions", "HY OAS Spread", "%"),
}

GEOPOLITICS_SERIES = {
    "USEPUINDXD": ("geopolitics", "US Economic Policy Uncertainty", "index"),
    "GEPUCURRENT": ("geopolitics", "Global EPU Index", "index"),
    "EPUTRADE": ("geopolitics", "Trade Policy Uncertainty", "index"),
    "EPUFISCAL": ("geopolitics", "Fiscal Policy Uncertainty", "index"),
    "EPUMONETARY": ("geopolitics", "Monetary Policy Uncertainty", "index"),
    "EMVNATSEC": ("geopolitics", "Equity Vol: National Security", "index"),
}

CORRELATION_SERIES = {
    "SP500": ("equities", "S&P 500", "index"),
    "NASDAQCOM": ("equities", "NASDAQ Composite", "index"),
    "DCOILWTICO": ("commodities", "WTI Crude Oil", "$/barrel"),
    "DCOILBRENTEU": ("commodities", "Brent Crude Oil", "$/barrel"),
    "DHHNGSP": ("commodities", "Natural Gas (Henry Hub)", "$/mmbtu"),
    "DEXJPUS": ("fx", "JPY/USD", "yen per dollar"),
    "DEXUSEU": ("fx", "USD/EUR", "dollars per euro"),
}

# All series combined
ALL_SERIES: dict[str, tuple[str, str, str]] = {}
for _group in (
    RATES_SERIES,
    INFLATION_SERIES,
    LABOR_SERIES,
    GROWTH_SERIES,
    CONSUMER_SERIES,
    HOUSING_SERIES,
    FISCAL_SERIES,
    FIN_CONDITIONS_SERIES,
    GEOPOLITICS_SERIES,
    CORRELATION_SERIES,
):
    ALL_SERIES.update(_group)


def _observation_id(series_id: str, date: str) -> str:
    return hashlib.sha1(f"fred|{series_id}|{date}".encode()).hexdigest()[:16]


class FREDMarketDataProvider(MarketDataProvider):
    """Fetches the latest values for all catalogued FRED series."""

    provider_name = "fred"

    def __init__(
        self,
        api_key: str | None = None,
        series: dict[str, tuple[str, str, str]] | None = None,
        limit: int = 1,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or settings.fred_api_key
        self.series = series or ALL_SERIES
        self.limit = limit
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "FRED API key is required. Set MPA_FRED_API_KEY in your .env file "
                "or pass api_key directly."
            )

    def gather(self, theses: list[Thesis]) -> list[MarketObservation]:
        """Fetch the latest observation for every catalogued FRED series."""
        observations: list[MarketObservation] = []

        try:
            client = httpx.Client(timeout=self.timeout)
        except Exception:
            logger.warning(
                "Failed to initialise FRED HTTP client; returning empty observations.",
                exc_info=True,
            )
            return observations

        with client:
            for series_id, (market, metric, unit) in self.series.items():
                try:
                    obs = self._fetch_series(client, series_id, market, metric, unit)
                    if obs:
                        observations.append(obs)
                except Exception:
                    logger.warning("Failed to fetch FRED series %s", series_id, exc_info=True)

        logger.info("FRED provider fetched %d/%d series", len(observations), len(self.series))
        return observations

    def _fetch_series(
        self,
        client: httpx.Client,
        series_id: str,
        market: str,
        metric: str,
        unit: str,
    ) -> MarketObservation | None:
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": self.limit,
        }
        response = client.get(FRED_BASE_URL, params=params)
        response.raise_for_status()
        data = response.json()

        obs_list = data.get("observations", [])
        if not obs_list:
            return None

        latest = obs_list[0]
        value = latest.get("value", ".")
        if value == ".":  # FRED uses "." for missing data
            return None

        date_str = latest.get("date", "")
        try:
            as_of = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            as_of = datetime.now(UTC)

        return MarketObservation(
            observation_id=_observation_id(series_id, date_str),
            market=market,
            metric=metric,
            value=f"{value} {unit}".strip(),
            as_of=as_of,
            interpretation=f"{metric}: {value} {unit} (as of {date_str})",
            source=f"FRED:{series_id}",
        )

    def fetch_history(
        self,
        series_id: str,
        start: str = "1900-01-01",
        end: str | None = None,
    ) -> list[FredObservation]:
        """Fetch the full available history for a series and return
        FredObservation rows. Skips FRED's missing-marker (".")."""
        params: dict[str, str | int] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start,
            "sort_order": "asc",
        }
        if end:
            params["observation_end"] = end

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(FRED_BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()

        fetched_at = datetime.now(UTC)
        out: list[FredObservation] = []
        for obs in data.get("observations", []):
            value = obs.get("value", ".")
            if value == "." or value is None or value == "":
                continue
            try:
                v = float(value)
            except (ValueError, TypeError):
                continue
            try:
                obs_date = date.fromisoformat(obs.get("date", ""))
            except ValueError:
                continue
            out.append(FredObservation(
                series_id=series_id,
                observation_date=obs_date,
                value=v,
                realtime_start=obs.get("realtime_start"),
                realtime_end=obs.get("realtime_end") or "9999-12-31",
                fetched_at=fetched_at,
            ))
        return out

    def fetch_category(self, category_series: dict[str, tuple[str, str, str]]) -> list[MarketObservation]:
        """Fetch only a specific category of series (e.g. RATES_SERIES)."""
        observations: list[MarketObservation] = []
        with httpx.Client(timeout=self.timeout) as client:
            for series_id, (market, metric, unit) in category_series.items():
                try:
                    obs = self._fetch_series(client, series_id, market, metric, unit)
                    if obs:
                        observations.append(obs)
                except Exception:
                    logger.warning("Failed to fetch FRED series %s", series_id, exc_info=True)
        return observations
