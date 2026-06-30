"""
Currency conversion to EUR for the funnel's source-performance revenue
calculation.

No external API: rates are a **hardcoded approximate table** (EUR value of one
unit of the given currency). This is intentionally simple — funnel revenue is an
estimate for comparing sources, not accounting. Refresh the numbers manually when
they drift (see CURRENCY_TO_EUR below).

Last reviewed: 2026-06 (mid-market approximations).
"""
from decimal import Decimal
from typing import Optional

# EUR value of 1 unit of <currency>.  amount_in_eur = amount * CURRENCY_TO_EUR[ccy]
CURRENCY_TO_EUR = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "PLN": 0.23,
    "CHF": 1.06,
    "CAD": 0.68,
    "AUD": 0.61,
    "NZD": 0.56,
    "JPY": 0.0061,
    "SEK": 0.088,
    "NOK": 0.086,
    "DKK": 0.134,
    "CZK": 0.040,
    "HUF": 0.0025,
    "RON": 0.20,
    "BGN": 0.51,
    "TRY": 0.026,
    "BRL": 0.17,
    "MXN": 0.050,
    "INR": 0.011,
    "ZAR": 0.050,
    "SGD": 0.68,
    "HKD": 0.118,
    "AED": 0.25,
    "SAR": 0.245,
    "ILS": 0.25,
    "KRW": 0.00067,
    "CNY": 0.127,
    "RUB": 0.010,
    "UAH": 0.022,
    "THB": 0.026,
    "IDR": 0.000057,
    "MYR": 0.20,
    "PHP": 0.016,
    "VND": 0.000036,
    "CLP": 0.00097,
    "COP": 0.00023,
    "ARS": 0.0010,
    "PEN": 0.24,
    "EGP": 0.019,
    "NGN": 0.00060,
    "PKR": 0.0033,
    "BDT": 0.0078,
    "VES": 0.025,
}


def convert_to_eur(amount, currency: str) -> Optional[float]:
    """Return `amount` of `currency` expressed in EUR, or None if the currency is
    unknown (caller treats None as a 'conversion_failed' warning and skips it)."""
    currency = (currency or "").upper().strip()
    rate = CURRENCY_TO_EUR.get(currency)
    if rate is None:
        return None
    return float(Decimal(str(amount)) * Decimal(str(rate)))
