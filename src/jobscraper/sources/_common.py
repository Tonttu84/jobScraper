"""Helpers shared by adapters: date parsing, country normalization, remote detection."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

COUNTRY_NAMES: dict[str, str] = {
    # English / native names → ISO2. Extend freely; matching is case-insensitive.
    "finland": "FI", "suomi": "FI", "estonia": "EE", "eesti": "EE", "sweden": "SE", "sverige": "SE",
    "norway": "NO", "norge": "NO", "denmark": "DK", "danmark": "DK", "iceland": "IS", "germany": "DE",
    "deutschland": "DE", "austria": "AT", "österreich": "AT", "switzerland": "CH", "schweiz": "CH",
    "netherlands": "NL", "nederland": "NL", "the netherlands": "NL", "belgium": "BE", "luxembourg": "LU",
    "france": "FR", "spain": "ES", "españa": "ES", "portugal": "PT", "italy": "IT", "italia": "IT",
    "ireland": "IE", "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "poland": "PL", "polska": "PL", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK",
    "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR", "croatia": "HR", "slovenia": "SI",
    "lithuania": "LT", "latvia": "LV", "malta": "MT", "cyprus": "CY", "ukraine": "UA", "georgia": "GE",
    "azerbaijan": "AZ", "armenia": "AM", "serbia": "RS", "bosnia and herzegovina": "BA", "bosnia": "BA",
    "montenegro": "ME", "north macedonia": "MK", "albania": "AL", "moldova": "MD", "kosovo": "XK",
    "kazakhstan": "KZ", "uzbekistan": "UZ", "kyrgyzstan": "KG", "belarus": "BY", "turkey": "TR",
    "türkiye": "TR", "united arab emirates": "AE", "uae": "AE", "dubai": "AE", "abu dhabi": "AE",
    "united states": "US", "usa": "US", "u.s.": "US", "canada": "CA", "india": "IN", "australia": "AU",
}

CITY_COUNTRY: dict[str, str] = {
    "helsinki": "FI", "espoo": "FI", "vantaa": "FI", "tampere": "FI", "turku": "FI", "oulu": "FI",
    "jyväskylä": "FI", "kuopio": "FI", "lahti": "FI", "tallinn": "EE", "tartu": "EE", "stockholm": "SE",
    "gothenburg": "SE", "göteborg": "SE", "malmö": "SE", "oslo": "NO", "bergen": "NO", "copenhagen": "DK",
    "københavn": "DK", "aarhus": "DK", "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE",
    "frankfurt": "DE", "cologne": "DE", "köln": "DE", "stuttgart": "DE", "düsseldorf": "DE", "vienna": "AT",
    "wien": "AT", "zurich": "CH", "zürich": "CH", "amsterdam": "NL", "rotterdam": "NL", "eindhoven": "NL",
    "brussels": "BE", "paris": "FR", "lyon": "FR", "madrid": "ES", "barcelona": "ES", "lisbon": "PT",
    "lisboa": "PT", "porto": "PT", "milan": "IT", "milano": "IT", "rome": "IT", "dublin": "IE",
    "london": "GB", "warsaw": "PL", "warszawa": "PL", "krakow": "PL", "kraków": "PL", "wrocław": "PL",
    "wroclaw": "PL", "gdańsk": "PL", "poznań": "PL", "prague": "CZ", "praha": "CZ", "brno": "CZ",
    "bratislava": "SK", "budapest": "HU", "bucharest": "RO", "sofia": "BG", "athens": "GR", "zagreb": "HR",
    "ljubljana": "SI", "vilnius": "LT", "riga": "LV", "tbilisi": "GE", "baku": "AZ", "yerevan": "AM",
    "belgrade": "RS", "sarajevo": "BA", "dubai": "AE", "abu dhabi": "AE", "kyiv": "UA", "kiev": "UA",
}

_REMOTE_RE = re.compile(r"\b(remote|etätyö|etänä|home ?office|fully distributed|work from anywhere|telecommute)\b", re.I)
_HYBRID_RE = re.compile(r"\b(hybrid|hybridi)\b", re.I)


def guess_country(*texts: str | None) -> str | None:
    """Best-effort ISO2 from location strings (country names first, then well-known cities)."""
    for t in texts:
        if not t:
            continue
        low = t.lower()
        if re.fullmatch(r"[a-z]{2}", low):
            return low.upper()
        for name, iso in COUNTRY_NAMES.items():
            if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
                return iso
    for t in texts:
        if not t:
            continue
        low = t.lower()
        for city, iso in CITY_COUNTRY.items():
            if re.search(rf"(?<![a-z]){re.escape(city)}(?![a-z])", low):
                return iso
    return None


def guess_remote(*texts: str | None, flag: bool | None = None) -> str:
    if flag is True:
        return "remote"
    joined = " ".join(t for t in texts if t)
    if _HYBRID_RE.search(joined):
        return "hybrid"
    if _REMOTE_RE.search(joined):
        return "remote"
    if flag is False:
        return "onsite"
    return "unknown"


def parse_date(value: Any) -> datetime | None:
    """Accepts ISO strings, RFC-2822 strings, epoch seconds/millis, or datetimes."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    s = str(value).strip()
    if re.fullmatch(r"\d{9,13}", s):
        return parse_date(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
