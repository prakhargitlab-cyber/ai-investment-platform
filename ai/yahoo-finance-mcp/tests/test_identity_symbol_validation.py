"""Focused regression tests for verifiedYahooSymbol validation
(Yahoo 401 / retry-storm fix task, item 9).

Root cause: the symbol allowlist regex excluded "&", rejecting genuine,
canonical NSE/Yahoo tickers that contain it -- confirmed via web search
that J&KBANK.NS (The Jammu and Kashmir Bank Limited) and M&M.NS (Mahindra
& Mahindra) are real, currently-traded symbols on NSE and Yahoo Finance,
not malformed input. verified_yahoo_symbol is passed as a Python string
argument to yfinance/httpx (never spliced into a raw URL by this
codebase), so allowing this one documented-safe character does not open
a URL-injection surface.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from yahoo_mcp_server.contracts import IdentityInput


def _identity(symbol: str) -> IdentityInput:
    return IdentityInput(globalInstrumentId=str(uuid4()), verifiedYahooSymbol=symbol, region="INDIA")


@pytest.mark.parametrize("symbol", ["J&KBANK.NS", "M&M.NS", "M&MFIN.NS"])
def test_valid_ampersand_containing_ticker_is_accepted(symbol):
    assert _identity(symbol).verified_yahoo_symbol == symbol


def test_ordinary_symbol_without_ampersand_still_accepted():
    assert _identity("INFY.NS").verified_yahoo_symbol == "INFY.NS"


@pytest.mark.parametrize("symbol", [
    "BAD<SYM>", "BAD SYM", "BAD;DROP", "BAD'SYM", '"BADSYM"', "../../etc/passwd",
    "", "A" * 41,
])
def test_malformed_input_still_rejected(symbol):
    with pytest.raises(ValidationError):
        _identity(symbol)


def test_leading_character_still_must_be_alphanumeric():
    """The allowlist still requires the symbol to START with a letter or
    digit -- broadening the body charset to include "&" does not loosen
    this first-character requirement."""
    with pytest.raises(ValidationError):
        _identity("&KBANK.NS")
