"""Region → IANA time zone(s) for quiet-hours evaluation.

A recipient ``region`` is an ISO 3166-1 alpha-2 country code (what the
CALL-E recipient object carries) or, for precision, an IANA zone name such as
``Asia/Kolkata``. Countries spanning several zones list every populated zone;
the quiet-hours gate treats the recipient as inside quiet hours if *any* of
them is — over-refusing is the safe direction. Unknown regions resolve to
nothing, and the gate refuses a live call it cannot place in local time.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COUNTRY_ZONES: dict[str, tuple[str, ...]] = {
    "AE": ("Asia/Dubai",),
    "AR": ("America/Argentina/Buenos_Aires",),
    "AT": ("Europe/Vienna",),
    "AU": (
        "Australia/Sydney",
        "Australia/Brisbane",
        "Australia/Adelaide",
        "Australia/Darwin",
        "Australia/Perth",
    ),
    "BD": ("Asia/Dhaka",),
    "BE": ("Europe/Brussels",),
    "BR": ("America/Sao_Paulo", "America/Manaus", "America/Rio_Branco", "America/Noronha"),
    "CA": (
        "America/Toronto",
        "America/Vancouver",
        "America/Edmonton",
        "America/Winnipeg",
        "America/Halifax",
        "America/St_Johns",
    ),
    "CH": ("Europe/Zurich",),
    "CL": ("America/Santiago",),
    "CN": ("Asia/Shanghai",),
    "CO": ("America/Bogota",),
    "CZ": ("Europe/Prague",),
    "DE": ("Europe/Berlin",),
    "DK": ("Europe/Copenhagen",),
    "EG": ("Africa/Cairo",),
    "ES": ("Europe/Madrid", "Atlantic/Canary"),
    "FI": ("Europe/Helsinki",),
    "FR": ("Europe/Paris",),
    "GB": ("Europe/London",),
    "GR": ("Europe/Athens",),
    "HK": ("Asia/Hong_Kong",),
    "HU": ("Europe/Budapest",),
    "ID": ("Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura"),
    "IE": ("Europe/Dublin",),
    "IL": ("Asia/Jerusalem",),
    "IN": ("Asia/Kolkata",),
    "IT": ("Europe/Rome",),
    "JP": ("Asia/Tokyo",),
    "KE": ("Africa/Nairobi",),
    "KR": ("Asia/Seoul",),
    "LK": ("Asia/Colombo",),
    "MX": ("America/Mexico_City", "America/Cancun", "America/Tijuana"),
    "MY": ("Asia/Kuala_Lumpur",),
    "NG": ("Africa/Lagos",),
    "NL": ("Europe/Amsterdam",),
    "NO": ("Europe/Oslo",),
    "NP": ("Asia/Kathmandu",),
    "NZ": ("Pacific/Auckland",),
    "PH": ("Asia/Manila",),
    "PK": ("Asia/Karachi",),
    "PL": ("Europe/Warsaw",),
    "PT": ("Europe/Lisbon", "Atlantic/Azores"),
    "QA": ("Asia/Qatar",),
    "RU": (
        "Europe/Moscow",
        "Europe/Kaliningrad",
        "Asia/Yekaterinburg",
        "Asia/Novosibirsk",
        "Asia/Vladivostok",
    ),
    "SA": ("Asia/Riyadh",),
    "SE": ("Europe/Stockholm",),
    "SG": ("Asia/Singapore",),
    "TH": ("Asia/Bangkok",),
    "TR": ("Europe/Istanbul",),
    "TW": ("Asia/Taipei",),
    "UA": ("Europe/Kyiv",),
    "US": (
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Phoenix",
        "America/Los_Angeles",
        "America/Anchorage",
        "Pacific/Honolulu",
    ),
    "VN": ("Asia/Ho_Chi_Minh",),
    "ZA": ("Africa/Johannesburg",),
}


def zones_for_region(region: str | None) -> list[ZoneInfo]:
    """Every zone a region may be in; empty when the region is missing or unknown."""
    if region is None or not region.strip():
        return []
    token = region.strip()
    names = (token,) if "/" in token else COUNTRY_ZONES.get(token.upper(), ())
    zones: list[ZoneInfo] = []
    for name in names:
        try:
            zones.append(ZoneInfo(name))
        except ZoneInfoNotFoundError:
            continue
    return zones
