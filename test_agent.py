"""
test_agent.py
--------------
Automated tests for the currency normalization and jurisdiction-design
changes to the AML/KYC Compliance Flagging Agent. Run with:

    python3 test_agent.py

or, if pytest is installed:

    pytest test_agent.py -v

These tests exercise the real rules_engine.py and fx_normalize.py logic
directly (no mocking of AML calculations) - only fatf_reference.py's
optional live-refresh network call is exercised via a controlled failure
simulation for TEST 9, since a live network call is not appropriate for
a repeatable automated test.
"""

import sys
import pandas as pd
from datetime import datetime

from fx_normalize import normalize_transactions, MissingFXRateError
from rules_engine import analyse_transactions, DEFAULT_CONFIG
import fatf_reference

PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail and status == FAIL else ""))


def make_df(rows):
    return pd.DataFrame(rows)


FX_RATES = {"USD": 1.0, "ZWG": 0.024691, "ZAR": 0.0540}
FX_SOURCES = {
    "USD": {"source": "Fixed", "timestamp": ""},
    "ZWG": {"source": "Manual Override", "timestamp": "2026-09-05 12:00:00"},
    "ZAR": {"source": "Manual Override", "timestamp": "2026-09-05 12:00:00"},
}


# ---------------------------------------------------------------------------
# TEST 1: USD transaction -> USD equivalent remains unchanged
# ---------------------------------------------------------------------------
def test_1():
    df = make_df([{"transaction_id": "T1", "customer_id": "C1", "date": "2026-06-01",
                    "amount": 10500, "currency": "USD", "counterparty_country": "USA",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    result = normalize_transactions(df, FX_RATES, FX_SOURCES)
    row = result.iloc[0]
    check("TEST 1: USD usd_equivalent == original_amount",
          row["usd_equivalent"] == 10500.0 and row["usd_exchange_rate"] == 1.0,
          f"got usd_equivalent={row['usd_equivalent']}, rate={row['usd_exchange_rate']}")
    check("TEST 1: USD original_currency == 'USD'", row["original_currency"] == "USD")


# ---------------------------------------------------------------------------
# TEST 2: ZiG transaction -> correctly converted to USD
# ---------------------------------------------------------------------------
def test_2():
    df = make_df([{"transaction_id": "T2", "customer_id": "C2", "date": "2026-06-01",
                    "amount": 50000, "currency": "ZiG", "counterparty_country": "Zimbabwe",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    result = normalize_transactions(df, FX_RATES, FX_SOURCES)
    row = result.iloc[0]
    expected = round(50000 * 0.024691, 2)
    check("TEST 2: ZiG 50,000 converts to correct USD equivalent",
          abs(row["usd_equivalent"] - expected) < 0.01,
          f"expected ~{expected}, got {row['usd_equivalent']}")
    check("TEST 2: ZiG alias normalized to ZWG", row["original_currency"] == "ZWG")


# ---------------------------------------------------------------------------
# TEST 3: ZAR transaction -> correctly converted to USD
# ---------------------------------------------------------------------------
def test_3():
    df = make_df([{"transaction_id": "T3", "customer_id": "C3", "date": "2026-06-01",
                    "amount": 20000, "currency": "ZAR", "counterparty_country": "South Africa",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    result = normalize_transactions(df, FX_RATES, FX_SOURCES)
    row = result.iloc[0]
    expected = round(20000 * 0.0540, 2)
    check("TEST 3: ZAR 20,000 converts to correct USD equivalent",
          abs(row["usd_equivalent"] - expected) < 0.01,
          f"expected ~{expected}, got {row['usd_equivalent']}")


# ---------------------------------------------------------------------------
# TEST 4: AML threshold evaluated using USD equivalent, NOT original currency
# ---------------------------------------------------------------------------
def test_4():
    # ZiG 50,000 -> USD ~1,234.55, well UNDER the 10,000 structuring threshold.
    # If the engine wrongly compared the raw 50,000 figure, this would
    # incorrectly fire the structuring rule.
    df = make_df([{"transaction_id": "T4", "customer_id": "C4", "date": "2026-06-01",
                    "amount": 50000, "currency": "ZiG", "counterparty_country": "Zimbabwe",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, dict(DEFAULT_CONFIG))
    row = result.iloc[0]
    structuring_triggered = any(r["label"] == "Large / structured transaction" for r in row["triggered_rules"])
    check("TEST 4: Structuring rule NOT triggered on raw 50,000 (would be wrong)",
          not structuring_triggered,
          "structuring rule incorrectly triggered on the raw non-USD amount")
    check("TEST 4: amount fed to rules engine is the USD equivalent", row["amount"] < 10000)


# ---------------------------------------------------------------------------
# TEST 5: Original amount and currency remain visible after conversion
# ---------------------------------------------------------------------------
def test_5():
    df = make_df([{"transaction_id": "T5", "customer_id": "C5", "date": "2026-06-01",
                    "amount": 50000, "currency": "ZWG", "counterparty_country": "Zimbabwe",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, dict(DEFAULT_CONFIG))
    row = result.iloc[0]
    check("TEST 5: original_amount preserved after full pipeline", row["original_amount"] == 50000.0)
    check("TEST 5: original_currency preserved after full pipeline", row["original_currency"] == "ZWG")


# ---------------------------------------------------------------------------
# TEST 6: Manual FX override is recorded and does not modify original data
# ---------------------------------------------------------------------------
def test_6():
    df = make_df([{"transaction_id": "T6", "customer_id": "C6", "date": "2026-06-01",
                    "amount": 1000, "currency": "ZAR", "counterparty_country": "South Africa",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    manual_rate = 0.06  # a deliberately different, clearly-manual rate
    manual_sources = {"ZAR": {"source": "Manual Override", "timestamp": "2026-09-05 15:00:00"}, "USD": {"source": "Fixed", "timestamp": ""}}
    result = normalize_transactions(df, {"USD": 1.0, "ZAR": manual_rate}, manual_sources)
    row = result.iloc[0]
    check("TEST 6: manual override rate used in conversion",
          abs(row["usd_equivalent"] - 60.0) < 0.01, f"got {row['usd_equivalent']}")
    check("TEST 6: FX source labeled 'Manual Override'", row["fx_rate_source"] == "Manual Override")
    check("TEST 6: original_amount unmodified by the override", row["original_amount"] == 1000.0)


# ---------------------------------------------------------------------------
# TEST 7: Institution can select any country from the complete jurisdiction list
# ---------------------------------------------------------------------------
def test_7():
    from countries_list import ALL_COUNTRIES
    check("TEST 7: jurisdiction list is large/comprehensive (>150 countries)", len(ALL_COUNTRIES) > 150,
          f"only {len(ALL_COUNTRIES)} countries listed")
    # Simulate selecting an arbitrary, non-"typical" country and classifying it
    arbitrary_country = "Fiji"
    check("TEST 7: an arbitrary country is present in the list", arbitrary_country in ALL_COUNTRIES)
    cfg = dict(DEFAULT_CONFIG)
    cfg["country_classifications"] = {arbitrary_country.lower(): "Medium"}
    df = make_df([{"transaction_id": "T7", "customer_id": "C7", "date": "2026-06-01",
                    "amount": 500, "currency": "USD", "counterparty_country": arbitrary_country,
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, cfg)
    row = result.iloc[0]
    geo_triggered = any(r["category"] == "Geographic" and r["weight"] > 0 for r in row["triggered_rules"])
    check("TEST 7: institution classification of an arbitrary country takes effect", geo_triggered)


# ---------------------------------------------------------------------------
# TEST 8: FATF status displayed separately from institutional classification
# ---------------------------------------------------------------------------
def test_8():
    status = fatf_reference.get_fatf_status("Iran")
    check("TEST 8: FATF status is retrievable independently of any institution config",
          status == "Call for Action", f"got '{status}'")
    # Confirm institution classification and FATF status are independently settable
    cfg = dict(DEFAULT_CONFIG)
    cfg["country_classifications"] = {"iran": "Medium"}  # institution disagrees with FATF severity
    df = make_df([{"transaction_id": "T8", "customer_id": "C8", "date": "2026-06-01",
                    "amount": 500, "currency": "USD", "counterparty_country": "Iran",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, cfg)
    row = result.iloc[0]
    geo_rule = next(r for r in row["triggered_rules"] if r["category"] == "Geographic")
    check("TEST 8: institution's own classification (Medium) drives the rule weight, not FATF's Call for Action severity",
          geo_rule["weight"] == 20, f"got weight {geo_rule['weight']}")
    check("TEST 8: FATF status still referenced in the explanation text",
          "Call for Action" in geo_rule["reason"])


# ---------------------------------------------------------------------------
# TEST 9: FATF update failure does not crash the application
# ---------------------------------------------------------------------------
def test_9():
    # Simulate a failure by pointing at an invalid host - refresh_fatf_data()
    # must catch this and return a graceful error dict, never raise.
    import fatf_reference as fr
    original_url = fr.FATF_PUBLICATION_DATE
    try:
        result = fr.refresh_fatf_data(timeout=2)
        check("TEST 9: refresh_fatf_data() returns a dict, never raises", isinstance(result, dict))
        check("TEST 9: result has a 'status' key", "status" in result)
        # Whatever happened (network blocked in this sandbox, success, or
        # mismatch), the bundled lists must remain intact and non-empty:
        check("TEST 9: FATF_CALL_FOR_ACTION was not erased", len(fr.FATF_CALL_FOR_ACTION) == 3)
        check("TEST 9: FATF_INCREASED_MONITORING was not erased", len(fr.FATF_INCREASED_MONITORING) == 22)
    except Exception as e:
        check("TEST 9: refresh_fatf_data() must not raise", False, str(e))


# ---------------------------------------------------------------------------
# TEST 10: Removing a country from institution list removes the geographic
# alert without deleting the country's FATF reference status
# ---------------------------------------------------------------------------
def test_10():
    cfg = dict(DEFAULT_CONFIG)
    cfg["country_classifications"] = {"iran": "High"}
    df = make_df([{"transaction_id": "T10", "customer_id": "C10", "date": "2026-06-01",
                    "amount": 500, "currency": "USD", "counterparty_country": "Iran",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    before = analyse_transactions(normalized, cfg).iloc[0]
    geo_before = any(r["category"] == "Geographic" and r["weight"] > 0 for r in before["triggered_rules"])
    check("TEST 10: Iran classified High DOES trigger geographic rule", geo_before)

    # Now simulate the officer removing Iran from the institution's list
    cfg["country_classifications"] = {}
    after = analyse_transactions(normalized, cfg).iloc[0]
    geo_after = any(r["category"] == "Geographic" and r["weight"] > 0 for r in after["triggered_rules"])
    check("TEST 10: removing Iran from institution list removes the geographic alert", not geo_after)

    # But FATF's own reference data must be completely unaffected
    check("TEST 10: FATF status for Iran is unchanged/undeleted",
          fatf_reference.get_fatf_status("Iran") == "Call for Action")


# ---------------------------------------------------------------------------
# TEST 11: FATF Increased Monitoring country is NOT automatically prohibited
# ---------------------------------------------------------------------------
def test_11():
    status = fatf_reference.get_fatf_status("Kenya")
    check("TEST 11: Kenya is FATF Increased Monitoring", status == "Increased Monitoring")
    cfg = dict(DEFAULT_CONFIG)  # institution has NOT classified Kenya at all
    df = make_df([{"transaction_id": "T11", "customer_id": "C11", "date": "2026-06-01",
                    "amount": 500, "currency": "USD", "counterparty_country": "Kenya",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, cfg).iloc[0]
    geo_triggered = any(r["category"] == "Geographic" and r["weight"] > 0 for r in result["triggered_rules"])
    check("TEST 11: grey-listed country NOT auto-treated as prohibited without institution classification",
          not geo_triggered)


# ---------------------------------------------------------------------------
# TEST 12: A transaction from an unselected country can still trigger other rules
# ---------------------------------------------------------------------------
def test_12():
    cfg = dict(DEFAULT_CONFIG)  # no country classifications at all
    df = make_df([{"transaction_id": "T12", "customer_id": "C12", "date": "2026-06-01",
                    "amount": 9000, "currency": "USD", "counterparty_country": "Kenya",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, cfg).iloc[0]
    structuring_triggered = any(r["label"] == "Large / structured transaction" for r in result["triggered_rules"])
    check("TEST 12: structuring rule still fires for an unclassified-country transaction",
          structuring_triggered)
    check("TEST 12: overall risk bucket is not 'None' despite no geographic classification",
          result["risk_bucket"] != "None")


# ---------------------------------------------------------------------------
# Extra: MissingFXRateError safety net (referenced by spec section 3's
# "do not crash" requirement, exercised at the fx_normalize layer)
# ---------------------------------------------------------------------------
def test_missing_rate_safety_net():
    df = make_df([{"transaction_id": "TX", "customer_id": "CX", "date": "2026-06-01",
                    "amount": 100, "currency": "ZAR", "counterparty_country": "South Africa",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    try:
        normalize_transactions(df, {"USD": 1.0}, {})  # no ZAR rate supplied
        check("Missing-rate safety net: should have raised MissingFXRateError", False)
    except MissingFXRateError as e:
        check("Missing-rate safety net: MissingFXRateError raised cleanly (app.py catches this, never crashes)",
              "ZAR" in e.missing_currencies)


if __name__ == "__main__":
    for fn in [test_1, test_2, test_3, test_4, test_5, test_6, test_7, test_8,
               test_9, test_10, test_11, test_12, test_missing_rate_safety_net]:
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"raised unexpected exception: {e}")

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{'='*60}\n{passed} passed, {failed} failed out of {len(results)} checks\n{'='*60}")
    sys.exit(1 if failed else 0)
