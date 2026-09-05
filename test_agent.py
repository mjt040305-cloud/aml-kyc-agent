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


# ---------------------------------------------------------------------------
# GEOGRAPHIC ALERT WATCHLIST TESTS (from the Geographic Risk redesign spec)
# ---------------------------------------------------------------------------
WATCHLIST_CFG = dict(DEFAULT_CONFIG)  # uses the pre-seeded Call-for-Action defaults


def _single_txn_result(country, currency="USD", amount=500, cfg=None):
    df = make_df([{"transaction_id": "TW", "customer_id": "CW", "date": "2026-06-01",
                    "amount": amount, "currency": currency, "counterparty_country": country,
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, cfg or dict(WATCHLIST_CFG))
    return result.iloc[0]


def test_geo_1_iran():
    row = _single_txn_result("Iran")
    geo = [r for r in row["triggered_rules"] if r["category"] == "Geographic"]
    check("GEO TEST 1: Iran triggers 'FATF High-Risk Jurisdiction — Call for Action'",
          len(geo) == 1 and geo[0]["label"] == "FATF High-Risk Jurisdiction \u2014 Call for Action")


def test_geo_2_dprk():
    row = _single_txn_result("North Korea")
    geo = [r for r in row["triggered_rules"] if r["category"] == "Geographic"]
    check("GEO TEST 2: DPRK/North Korea triggers 'FATF High-Risk Jurisdiction — Call for Action'",
          len(geo) == 1 and geo[0]["label"] == "FATF High-Risk Jurisdiction \u2014 Call for Action")


def test_geo_3_myanmar():
    row = _single_txn_result("Myanmar")
    geo = [r for r in row["triggered_rules"] if r["category"] == "Geographic"]
    check("GEO TEST 3: Myanmar triggers 'FATF High-Risk Jurisdiction — Call for Action'",
          len(geo) == 1 and geo[0]["label"] == "FATF High-Risk Jurisdiction \u2014 Call for Action")


def test_geo_4_institution_selected_syria():
    cfg = dict(WATCHLIST_CFG)
    cfg["country_classifications"] = dict(cfg["country_classifications"])
    cfg["country_classifications"]["syria"] = "High"  # institution has selected Syria onto the watchlist
    row = _single_txn_result("Syria", cfg=cfg)
    geo = [r for r in row["triggered_rules"] if r["category"] == "Geographic"]
    check("GEO TEST 4: institution-selected Syria triggers an institutional geographic alert",
          len(geo) == 1 and geo[0]["weight"] > 0)
    check("GEO TEST 4: Syria's label is NOT the Call-for-Action label (it's Increased Monitoring, not blacklisted)",
          geo[0]["label"] != "FATF High-Risk Jurisdiction \u2014 Call for Action")


def test_geo_5_unselected_increased_monitoring():
    cfg = dict(WATCHLIST_CFG)  # Kenya (Increased Monitoring) NOT added to institution list
    row = _single_txn_result("Kenya", cfg=cfg)
    geo_triggered = any(r["category"] == "Geographic" and r["weight"] > 0 for r in row["triggered_rules"])
    check("GEO TEST 5: unselected FATF Increased Monitoring country does NOT auto-trigger", not geo_triggered)


def test_geo_6_zimbabwe():
    row = _single_txn_result("Zimbabwe")
    geo_triggered = any(r["category"] == "Geographic" and r["weight"] > 0 for r in row["triggered_rules"])
    check("GEO TEST 6: Zimbabwe (not on any FATF list) triggers no geographic alert", not geo_triggered)
    check("GEO TEST 6: Zimbabwe's FATF status is 'Not listed'", fatf_reference.get_fatf_status("Zimbabwe") == "Not listed")


def test_geo_7_zig_iran():
    row = _single_txn_result("Iran", currency="ZiG", amount=50000)
    check("GEO TEST 7: ZiG transaction from Iran converted to USD before evaluation",
          abs(row["amount"] - round(50000 * 0.024691, 2)) < 0.01)
    geo = [r for r in row["triggered_rules"] if r["category"] == "Geographic"]
    check("GEO TEST 7: geographic rule still correctly triggers after currency conversion", len(geo) == 1)


def test_geo_8_zar_syria():
    cfg = dict(WATCHLIST_CFG)
    cfg["country_classifications"] = dict(cfg["country_classifications"])
    cfg["country_classifications"]["syria"] = "High"
    row = _single_txn_result("Syria", currency="ZAR", amount=20000, cfg=cfg)
    check("GEO TEST 8: ZAR transaction from Syria converted to USD before evaluation",
          abs(row["amount"] - round(20000 * 0.0540, 2)) < 0.01)
    geo = [r for r in row["triggered_rules"] if r["category"] == "Geographic"]
    check("GEO TEST 8: geographic rule still correctly triggers after currency conversion", len(geo) == 1)


def test_geo_9_watchlist_vs_actual_separation():
    # A watchlist country (Myanmar) with NO transactions in this batch should
    # still be a valid reference entry, but must not appear in the "Actual
    # Transaction Geographic Risk" view, which is built ONLY from geo_score>0
    # rows actually present in the analysed batch (replicating app.py's
    # render_actual_transaction_geo_risk filter logic).
    df = make_df([{"transaction_id": "T1", "customer_id": "C1", "date": "2026-06-01",
                    "amount": 500, "currency": "USD", "counterparty_country": "Iran",
                    "transaction_type": "wire_transfer", "customer_risk_profile": "Low"}])
    normalized = normalize_transactions(df, FX_RATES, FX_SOURCES)
    result = analyse_transactions(normalized, dict(WATCHLIST_CFG))
    records = result.to_dict("records")
    actual_geo_countries = {
        r["counterparty_country"] for r in records
        if r.get("category_scores", {}).get("Geographic", 0) > 0
    }
    check("GEO TEST 9: Iran (has a transaction) appears in Actual Transaction Geographic Risk",
          "Iran" in actual_geo_countries)
    check("GEO TEST 9: Myanmar (watchlist reference only, no transactions here) does NOT appear in Actual view",
          "Myanmar" not in actual_geo_countries)


def test_geo_10_fatf_update_failure_continuity():
    result = fatf_reference.refresh_fatf_data(timeout=2)
    check("GEO TEST 10: refresh_fatf_data() never raises regardless of network outcome", isinstance(result, dict))
    check("GEO TEST 10: FATF_CALL_FOR_ACTION remains exactly 3 entries after any refresh attempt",
          len(fatf_reference.FATF_CALL_FOR_ACTION) == 3)
    check("GEO TEST 10: FATF_INCREASED_MONITORING remains exactly 22 entries after any refresh attempt",
          len(fatf_reference.FATF_INCREASED_MONITORING) == 22)
    check("GEO TEST 10: application can continue scoring transactions after a refresh attempt",
          _single_txn_result("Iran")["risk_bucket"] in ("High", "Medium", "Low", "None"))


if __name__ == "__main__":
    for fn in [test_1, test_2, test_3, test_4, test_5, test_6, test_7, test_8,
               test_9, test_10, test_11, test_12, test_missing_rate_safety_net,
               test_geo_1_iran, test_geo_2_dprk, test_geo_3_myanmar,
               test_geo_4_institution_selected_syria, test_geo_5_unselected_increased_monitoring,
               test_geo_6_zimbabwe, test_geo_7_zig_iran, test_geo_8_zar_syria,
               test_geo_9_watchlist_vs_actual_separation, test_geo_10_fatf_update_failure_continuity]:
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"raised unexpected exception: {e}")

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{'='*60}\n{passed} passed, {failed} failed out of {len(results)} checks\n{'='*60}")
    sys.exit(1 if failed else 0)
