"""
fatf_reference.py
------------------
FATF jurisdiction reference data - REGULATORY REFERENCE INFORMATION ONLY.

This module deliberately does NOT decide which countries are "high risk"
for this institution. It only tells the compliance officer what FATF's
own published status is for a country, so the officer can make an
informed, documented institutional risk classification (see
COUNTRY_CLASSIFICATIONS handling in app.py / rules_engine.py).

Two FATF lists exist:
  - Call for Action ("black list"): jurisdictions with the most severe,
    persistent deficiencies. FATF calls for enhanced due diligence and,
    in the most serious cases, countermeasures.
  - Increased Monitoring ("grey list"): jurisdictions actively working
    with FATF on an action plan. FATF explicitly does NOT call for
    enhanced due diligence on these jurisdictions and does not envisage
    de-risking - being on this list is not equivalent to being
    prohibited or automatically high-risk.

Data below was verified directly against FATF's own published statements
(fatf-gafi.org) dated 19 June 2026 - the most recent FATF Plenary as of
this agent's last verification. See `refresh_fatf_data()` for the
optional, best-effort live-refresh path.
"""

from datetime import datetime

FATF_PUBLICATION_DATE = "2026-06-19"
FATF_VERIFIED_AT = "2026-09-05"  # date this snapshot was last confirmed against fatf-gafi.org

# "Black list" - High-Risk Jurisdictions subject to a Call for Action.
# Verified against https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/call-for-action-june-2026.html
# Names below match countries_list.py's naming convention exactly so
# lookups never depend on alias matching for the primary case.
FATF_CALL_FOR_ACTION = [
    "North Korea",
    "Iran",
    "Myanmar",
]

# "Grey list" - Jurisdictions under Increased Monitoring (22 as of 19 June 2026).
# Verified against https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/increased-monitoring-june-2026.html
FATF_INCREASED_MONITORING = [
    "Angola", "Bolivia", "Bosnia and Herzegovina", "Bulgaria", "Cameroon",
    "Ivory Coast", "DR Congo", "Haiti", "Iraq",
    "Kenya", "Kuwait", "Laos", "Lebanon",
    "Monaco", "Nepal", "Papua New Guinea", "South Sudan", "Syria",
    "Venezuela", "Vietnam", "Virgin Islands (UK)", "Yemen",
]


def get_fatf_status(country: str) -> str:
    """
    Returns one of: "Call for Action", "Increased Monitoring", "Not listed".
    Matching is case-insensitive and tolerates a few common alternative
    names/spellings not used in countries_list.py's canonical list.
    """
    c = country.strip().lower()
    aliases = {
        "dprk": "north korea",
        "democratic people's republic of korea": "north korea",
        "lao pdr": "laos",
        "lao people's democratic republic": "laos",
        "cote d'ivoire": "ivory coast",
        "côte d'ivoire": "ivory coast",
        "drc": "dr congo",
        "congo (drc)": "dr congo",
        "democratic republic of congo": "dr congo",
        "democratic republic of the congo": "dr congo",
        "british virgin islands": "virgin islands (uk)",
    }
    c = aliases.get(c, c)

    for entry in FATF_CALL_FOR_ACTION:
        if entry.lower() == c:
            return "Call for Action"
    for entry in FATF_INCREASED_MONITORING:
        if entry.lower() == c:
            return "Increased Monitoring"
    return "Not listed"


def refresh_fatf_data(timeout=8):
    """
    Best-effort attempt to confirm the FATF lists above are still current
    by fetching FATF's own high-risk-jurisdictions index page and checking
    it still references the same publication date this snapshot was
    verified against.

    This does NOT auto-update the bundled lists (FATF list changes involve
    nuanced country statements that should not be auto-parsed and trusted
    blindly) - it only tells the officer whether a newer FATF publication
    date is visible, so they know to check for an update manually.

    NEVER erases or empties the existing bundled data, and NEVER crashes -
    always returns a dict:
      {"status": "current", "checked_at": ..., "message": ...}
      {"status": "possibly_outdated", "checked_at": ..., "message": ...}
      {"status": "error", "message": ...}
    """
    try:
        import requests
    except ImportError:
        return {"status": "error", "message": "The 'requests' package is not installed in this environment."}

    url = "https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions.html"
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return {
            "status": "error",
            "message": f"Could not reach FATF's site right now: {e}. Continuing to use the last "
                       f"successfully cached dataset (verified {FATF_VERIFIED_AT}, publication date "
                       f"{FATF_PUBLICATION_DATE}) - no data was erased.",
        }

    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # We look for a publication date string on the index page. If we can't
    # find any recognisable date reference, we report "possibly_outdated"
    # rather than assuming everything is fine - but again, we change
    # nothing about the bundled lists either way.
    if FATF_PUBLICATION_DATE.replace("-", "") in html.replace("-", "").replace(" ", "").lower():
        return {
            "status": "current",
            "checked_at": checked_at,
            "message": f"FATF's site still references the {FATF_PUBLICATION_DATE} publication - "
                       f"the bundled snapshot (verified {FATF_VERIFIED_AT}) appears current.",
        }

    return {
        "status": "possibly_outdated",
        "checked_at": checked_at,
        "message": f"Fetched FATF's site successfully but could not confirm the "
                   f"{FATF_PUBLICATION_DATE} publication date is still the latest. FATF may have "
                   f"published a newer Plenary statement - please verify manually at fatf-gafi.org "
                   f"and update this agent's bundled lists if so. Continuing to use the last "
                   f"successfully cached dataset - nothing was erased.",
    }
