from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd


NOW = datetime.now(timezone.utc).replace(microsecond=0)


class FakeYahooTicker:
    def __init__(self, symbol: str, scenario: str = "SUCCESS") -> None:
        self.symbol = symbol
        self.scenario = scenario
        self._exchange, self._currency = identity_for(symbol)
        annual_periods = (pd.Timestamp("2025-03-31"), pd.Timestamp("2024-03-31"))
        quarterly_periods = (pd.Timestamp("2026-06-30"), pd.Timestamp("2026-03-31"))
        self.income_stmt = pd.DataFrame(
            {
                annual_periods[0]: {"Total Revenue": 1000, "Net Income": 100, "EBITDA": 180},
                annual_periods[1]: {"Total Revenue": 900, "Net Income": 80, "EBITDA": 150},
            }
        )
        self.balance_sheet = pd.DataFrame(
            {
                annual_periods[0]: {"Total Debt": 200, "Stockholders Equity": 800, "Total Assets": 1400},
                annual_periods[1]: {"Total Debt": 220, "Stockholders Equity": 720, "Total Assets": 1300},
            }
        )
        self.cashflow = pd.DataFrame(
            {
                annual_periods[0]: {"Operating Cash Flow": 160, "Capital Expenditure": -40},
                annual_periods[1]: {"Operating Cash Flow": 130, "Capital Expenditure": -35},
            }
        )
        self.quarterly_income_stmt = pd.DataFrame(
            {
                quarterly_periods[0]: {"Total Revenue": 300, "Net Income": 35, "Diluted EPS": 3.5},
                quarterly_periods[1]: {"Total Revenue": 275, "Net Income": 30, "Diluted EPS": 3.0},
            }
        )
        self.quarterly_balance_sheet = pd.DataFrame(
            {
                quarterly_periods[0]: {"Total Debt": 190, "Stockholders Equity": 830},
                quarterly_periods[1]: {"Total Debt": 200, "Stockholders Equity": 800},
            }
        )
        self.quarterly_cashflow = pd.DataFrame(
            {
                quarterly_periods[0]: {"Operating Cash Flow": 45},
                quarterly_periods[1]: {"Operating Cash Flow": 40},
            }
        )

    @property
    def info(self):
        if self.scenario == "TIMEOUT":
            time.sleep(0.25)
        if self.scenario == "UPSTREAM_FAILURE":
            raise ConnectionError("sensitive upstream detail")
        if self.scenario == "INVALID_INFO":
            return ["not", "an", "object"]
        if self.scenario == "EMPTY":
            return {"symbol": self.symbol, "exchange": self._exchange, "currency": self._currency, "quoteType": "EQUITY"}
        symbol = "WRONG" if self.scenario == "WRONG_SYMBOL" else self.symbol
        exchange = "NYQ" if self.scenario == "WRONG_EXCHANGE" else self._exchange
        currency = "USD" if self.scenario == "WRONG_CURRENCY" else self._currency
        return {
            "symbol": symbol,
            "currentPrice": 250.50,
            "regularMarketTime": int(NOW.timestamp()),
            "currency": currency,
            "exchange": exchange,
            "quoteType": "EQUITY",
            "longName": f"{self.symbol} Company",
            "sector": "Industrials",
            "industry": "Aerospace & Defense",
            "trailingEps": 12.5,
            "forwardEps": 14.0,
            "trailingPE": 20.04,
            "forwardPE": 17.89,
            "priceToBook": 4.2,
            "enterpriseToEbitda": 15.0,
            "marketCap": float("nan") if self.scenario == "NAN_FACT" else 1000000,
            "returnOnEquity": 0.18,
            "revenueGrowth": 0.12,
            "earningsGrowth": 0.15,
            "totalDebt": 200,
            "totalCash": 300,
            "targetLowPrice": 220,
            "targetMedianPrice": 275,
            "targetMeanPrice": 280,
            "targetHighPrice": 320,
            "numberOfAnalystOpinions": 8,
            "recommendationMean": 1.8,
            "recommendationKey": "buy",
        }

    @property
    def news(self):
        if self.scenario == "NO_NEWS":
            return []
        return [
            {
                "title": "Issuer publishes results",
                "publisher": "Yahoo Finance",
                "link": "https://news.example/current",
                "providerPublishTime": int((NOW - timedelta(days=1)).timestamp()),
            },
            {
                "title": "Duplicate issuer result",
                "publisher": "Yahoo Finance",
                "link": "https://news.example/current",
                "providerPublishTime": int((NOW - timedelta(days=1)).timestamp()),
            },
            {
                "title": "Old issuer result",
                "publisher": "Yahoo Finance",
                "link": "https://news.example/old",
                "providerPublishTime": int((NOW - timedelta(days=31)).timestamp()),
            },
        ]

    def history(self, **_kwargs):
        if self.scenario == "UPSTREAM_FAILURE":
            raise ConnectionError("sensitive history detail")
        index = pd.DatetimeIndex([NOW - timedelta(days=2), NOW - timedelta(days=1), NOW])
        return pd.DataFrame({"Close": [248.0, float("nan"), 250.5]}, index=index)


def identity_for(symbol: str) -> tuple[str, str]:
    if symbol.endswith(".NS"):
        return "NSI", "INR"
    if symbol.endswith(".AS"):
        return "AMS", "EUR"
    return "NMS", "USD"


def factory(scenario: str = "SUCCESS"):
    return lambda symbol: FakeYahooTicker(symbol, scenario)
