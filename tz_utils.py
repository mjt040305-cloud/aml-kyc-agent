"""
tz_utils.py
------------
Single source of truth for "now" across the whole application, in
Zimbabwe local time (Africa/Harare, UTC+2, no daylight saving).

Without this, every `datetime.now()` call in the app uses the SERVER's
local time - on Streamlit Community Cloud that's UTC, which is 2 hours
behind Zimbabwe. For a compliance application whose audit trail,
officer sign-off timestamps, and FX rate timestamps carry real
evidentiary weight, that 2-hour skew matters - every timestamp in this
app should say what time it actually was in Zimbabwe.

Uses Python's standard `zoneinfo` (stdlib since Python 3.9) - no extra
dependency.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ZIMBABWE_TZ = ZoneInfo("Africa/Harare")


def zim_now() -> datetime:
    """Current time as a timezone-aware datetime in Africa/Harare."""
    return datetime.now(ZIMBABWE_TZ)


def zim_now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Current Zimbabwe time, formatted - the string form used in almost
    every timestamp written by this app (audit log, case updated_at,
    report 'Prepared:' lines, FX rate timestamps)."""
    return zim_now().strftime(fmt)
