"""ICAO -> country code -> flag emoji helpers.

Single source of truth for the dashboard. Both ``app.py`` and ``v2_data.py``
import from here so the prefix table can't drift between modules. Merged
from the previously-divergent tables in those two files.

Two-character prefixes take precedence over single-character ones; UK is
normalized to GB inside ``_country_to_flag`` so callers don't need to care
which form a given station produces.
"""

from __future__ import annotations

_ICAO_PREFIX_COUNTRY: dict[str, str] = {
    "K": "US", "C": "CA",
    # Europe
    "EG": "UK", "EH": "NL", "EF": "FI", "ED": "DE", "EP": "PL", "EI": "IE",
    "EV": "LV", "EY": "LT", "EE": "EE", "EN": "NO", "ES": "SE", "EK": "DK",
    "EL": "LU", "EB": "BE",
    "LF": "FR", "LE": "ES", "LI": "IT", "LT": "TR", "LL": "IL", "LP": "PT",
    "LO": "AT", "LS": "CH", "LH": "HU", "LR": "RO", "LB": "BG", "LG": "GR",
    "LK": "CZ", "LZ": "SK",
    # Asia-Pacific
    "RJ": "JP", "RK": "KR", "RC": "TW", "RP": "PH", "RO": "JP",
    "Z": "CN", "W": "ID",  # defaults; specific 2-char overrides below
    "WS": "SG", "WM": "MY", "WI": "ID", "WB": "BN", "WA": "ID", "WR": "ID",
    "VH": "HK", "VM": "MO", "VT": "TH", "VV": "VN", "VY": "MM",
    "VI": "IN", "VE": "IN", "VO": "IN", "VA": "IN",
    "VN": "NP", "VQ": "BT", "VC": "LK",
    # Americas
    "S": "BR",  # default; specific 2-char overrides below
    "SA": "AR", "SC": "CL", "SK": "CO", "SP": "PE", "SV": "VE", "SU": "UY",
    "SB": "BR", "SG": "EC", "SE": "EC",
    "MM": "MX", "MP": "PA", "MK": "JM", "MU": "CU", "MR": "DO",
    "MD": "DO", "MS": "SV", "MH": "HN", "MG": "GT", "MN": "NI", "MZ": "BZ",
    # Middle East / Africa
    "OE": "SA", "OP": "PK", "OI": "IR", "OJ": "JO", "OA": "AF", "OB": "BH",
    "OK": "KW", "OM": "AE", "OO": "OM", "OT": "QA",
    "DN": "NG", "DA": "DZ",
    "FA": "ZA", "FN": "AO", "FK": "CM", "FJ": "MG", "FT": "TD",
    "GM": "MA", "GO": "SN",
    "HB": "ET", "HK": "KE", "HA": "EG", "HE": "EG", "HR": "RW", "HU": "UG",
    # CIS
    "U": "RU",  # default
    "UA": "KZ", "UK": "UA", "UT": "UZ",
    # Oceania
    "Y": "AU", "NZ": "NZ",
}


def _icao_to_country(icao: str) -> str:
    """Derive ISO country code from ICAO identifier.

    Tries exact match, then 2-char prefix, then 1-char prefix.
    """
    if not icao:
        return ""
    if icao in _ICAO_PREFIX_COUNTRY:
        return _ICAO_PREFIX_COUNTRY[icao]
    if icao[:2] in _ICAO_PREFIX_COUNTRY:
        return _ICAO_PREFIX_COUNTRY[icao[:2]]
    if icao[:1] in _ICAO_PREFIX_COUNTRY:
        return _ICAO_PREFIX_COUNTRY[icao[:1]]
    return ""


def _country_to_flag(cc: str) -> str:
    """Convert ISO country code to flag emoji (regional indicator symbols)."""
    if not cc or len(cc) < 2:
        return ""
    if cc == "UK":
        cc = "GB"
    return chr(0x1F1E6 + ord(cc[0]) - ord("A")) + chr(0x1F1E6 + ord(cc[1]) - ord("A"))


def _icao_to_flag(icao: str) -> str:
    return _country_to_flag(_icao_to_country(icao))
