"""
fx_normalize.py
----------------
Currency normalization layer for the AML/KYC Compliance Flagging Agent.

USD is the AML compliance baseline currency: every transaction, regardless
of its original currency, is converted to a `usd_equivalent` BEFORE it
reaches the AML rules engine (rules_engine.py). All AML thresholds, rule
triggers, dashboard metrics, and reports operate on `usd_equivalent` -
never on the original (possibly non-USD) amount. This baseline is fixed
and not user-changeable, because the rules engine's thresholds (e.g. the
structuring alert threshold) are defined in USD.

The original amount and currency are always preserved alongside the USD
equivalent for auditability - nothing about the source transaction is
discarded or overwritten. A manual FX override changes only which rate is
used for the current session's USD conversion; it never modifies the
original_amount/original_currency fields.

Supported transaction currencies: USD, ZAR, ZWG (ZiG). Designed so
additional currencies can be added by extending SUPPORTED_CURRENCIES.
"""

from datetime import datetime

SUPPORTED_CURRENCIES = ["USD", "ZAR", "ZWG"]
CURRENCY_LABELS = {"USD": "US Dollar", "ZAR": "South African Rand", "ZWG": "Zimbabwe Gold (ZiG)"}
CURRENCY_ALIASES = {"ZIG": "ZWG"}  # tolerate the common colloquial spelling in uploaded data

# Illustrative starting points ONLY, used to pre-fill the manual-override
# input so the officer has a sane starting value - never presented as a
# live/authoritative rate. See fetch_live_rate() for the real live path.
FALLBACK_STARTING_RATE_TO_USD = {
    "USD": 1.0,
    "ZAR": 1 / 18.0,   # ~0.0556 USD per ZAR
    "ZWG": 1 / 27.0,   # ~0.0370 USD per ZiG
}


class MissingFXRateError(Exception):
    """Raised when normalize_transactions() is asked to convert a currency
    with no resolved rate. app.py is expected to prevent this by requiring
    every currency present in the uploaded data to have a rate (live or
    manual override) before the analysis can be run - this exception is a
    defensive backstop, not the primary control."""
    def __init__(self, missing_currencies):
        self.missing_currencies = missing_currencies
        super().__init__(f"Missing FX rate for: {', '.join(missing_currencies)}")


def fetch_live_rate(currency_code: str, timeout=6):
    """
    Best-effort attempt to fetch a REAL live USD exchange rate for
    `currency_code` from a free, no-API-key FX endpoint (open.er-api.com).

    Returns (rate_to_usd: float | None, info: dict). Never raises, and
    never fabricates a rate - if the live source doesn't publish this
    currency or the request fails for any reason, returns (None, info)
    with a clear explanation, so the caller can fall back to a manual
    override.
    """
    if currency_code == "USD":
        return 1.0, {"source": "Fixed", "message": "USD is the AML baseline currency (rate = 1 by definition)."}

    try:
        import requests
    except ImportError:
        return None, {"source": "Live (unavailable)", "message": "The 'requests' package is not installed."}

    try:
        resp = requests.get(
            "https://open.er-api.com/v6/latest/USD", timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AML-KYC-Agent-Coursework/1.0)"},
        )
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates", {})
        if currency_code not in rates or not rates[currency_code]:
            return None, {
                "source": "Live (unavailable)",
                "message": f"{currency_code} is not published by the live FX source (open.er-api.com) - "
                           f"use a manual override.",
            }
        usd_to_currency = float(rates[currency_code])
        if usd_to_currency <= 0:
            return None, {"source": "Live (unavailable)", "message": "Live source returned an invalid rate."}
        rate_to_usd = 1.0 / usd_to_currency
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return rate_to_usd, {
            "source": "Live",
            "message": f"Fetched live from open.er-api.com at {now}.",
            "timestamp": now,
        }
    except Exception as e:
        return None, {
            "source": "Live (unavailable)",
            "message": f"Live FX fetch failed: {e}. Use a manual override.",
        }


def normalize_currency_code(code: str) -> str:
    code = str(code).strip().upper()
    return CURRENCY_ALIASES.get(code, code)


def normalize_transactions(df, fx_rates: dict, fx_sources: dict):
    """
    df: raw transaction dataframe. May or may not have a 'currency' column;
        if absent, every transaction is treated as USD (backward compatible
        with pre-multi-currency CSVs, e.g. sample_transactions.csv).
    fx_rates: {currency_code: rate_to_usd} - 1 unit of currency_code = rate_to_usd USD.
        Must contain an entry for every currency present in df, or
        MissingFXRateError is raised (app.py is expected to prevent this
        by validating before calling).
    fx_sources: {currency_code: {"source": "Live"|"Manual Override"|"Fixed", "timestamp": str}}

    Returns a COPY of df with these columns added:
      original_amount, original_currency, usd_exchange_rate, usd_equivalent,
      fx_rate_source, fx_rate_timestamp, fx_conversion_status
    and 'amount' OVERWRITTEN to equal usd_equivalent, so the existing rules
    engine (which reads the 'amount' column) automatically evaluates every
    AML rule and threshold in USD without needing any internal changes.
    """
    df = df.copy()

    if "currency" not in df.columns:
        df["currency"] = "USD"
    df["currency"] = df["currency"].fillna("USD").apply(normalize_currency_code)

    present_currencies = sorted(df["currency"].unique().tolist())
    missing = [c for c in present_currencies if fx_rates.get(c) is None]
    if missing:
        raise MissingFXRateError(missing)

    original_amount = df["amount"].astype(float)
    original_currency = df["currency"]

    usd_rate = original_currency.map(fx_rates)
    usd_equiv = (original_amount * usd_rate).round(2)

    df["original_amount"] = original_amount
    df["original_currency"] = original_currency
    df["usd_exchange_rate"] = usd_rate
    df["usd_equivalent"] = usd_equiv
    df["fx_rate_source"] = original_currency.map(lambda c: fx_sources.get(c, {}).get("source", "Unknown"))
    df["fx_rate_timestamp"] = original_currency.map(lambda c: fx_sources.get(c, {}).get("timestamp", ""))
    df["fx_conversion_status"] = original_currency.map(
        lambda c: "USD (AML baseline - no conversion needed)" if c == "USD" else "Normalized to USD"
    )

    # Everything downstream (rules_engine, dashboard, PDF, CSV) reads
    # 'amount' - point it at the USD equivalent so every AML rule and
    # threshold evaluates in USD, per the AML baseline requirement.
    df["amount"] = df["usd_equivalent"]

    return df
