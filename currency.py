"""
currency.py
-----------
Reporting-currency support for the AML/KYC Compliance Flagging Agent.

Transaction data is assumed to be recorded in USD. This module converts
USD amounts to a chosen reporting currency purely for DISPLAY purposes
(dashboard, review cards, exports) - AML rule thresholds are always
evaluated in USD internally, so switching currency never changes which
transactions get flagged.

Exchange rates are NOT fetched live (this agent has no external network
dependency by design - see README). Instead, the compliance officer
enters their own rate from the sidebar, with a sensible default offered
as a starting point. This keeps the agent honest: no rate is presented
as authoritative or current.
"""

CURRENCY_OPTIONS = {
    "USD": {"symbol": "$", "name": "US Dollar", "default_rate": 1.0},
    "ZAR": {"symbol": "R", "name": "South African Rand", "default_rate": 18.0},
    "ZWG": {"symbol": "ZiG", "name": "Zimbabwe Gold (ZiG)", "default_rate": 27.0},
    "GBP": {"symbol": "\u00A3", "name": "British Pound", "default_rate": 0.79},
    "EUR": {"symbol": "\u20AC", "name": "Euro", "default_rate": 0.92},
}


def convert(amount_usd: float, rate: float) -> float:
    return amount_usd * rate


def format_amount(amount_usd: float, currency_code: str, rate: float) -> str:
    symbol = CURRENCY_OPTIONS.get(currency_code, CURRENCY_OPTIONS["USD"])["symbol"]
    converted = convert(amount_usd, rate)
    return f"{symbol}{converted:,.2f}"
