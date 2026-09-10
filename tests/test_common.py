"""``sources/_common.py`` helpers — country parsing above all.

Enterprise boards (Eightfold, Oracle, Workday) post locations as "Hyderabad, TS, IN" or
"Redmond, WA, US". Before these tests those ended with ``country=None``, the rule filter sent
them to the AI screen as "on-site with unknown country", and Sonnet paid to reject them.
"""

from __future__ import annotations

import pytest

from jobscraper.sources._common import guess_country, guess_remote, is_cloudflare_challenge


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # ---- country names and bare codes (behaviour that already existed)
        ("Helsinki, Finland", "FI"),
        ("Espoo", "FI"),
        ("FI", "FI"),
        ("de", "DE"),
        ("Remote", None),
        (None, None),
        ("", None),
        # ---- "City, ST, CC": the LAST token is the country
        ("Hyderabad, TS, IN", "IN"),
        ("Noida, UP, IN", "IN"),
        ("Redmond, WA, US", "US"),
        ("Shanghai, SH, CN", "CN"),
        ("Sao Paulo, SP, BR", "BR"),
        ("São Paulo, SP, BR", "BR"),
        ("Cambridge, MA, US", "US"),
        ("Toronto, ON, CA", "CA"),
        ("Amsterdam, NH, NL", "NL"),
        ("Building 25, Redmond, WA, US", "US"),
        # ---- "City, CC" when the code cannot be a US state
        ("Tallinn, EE", "EE"),
        ("Cork, IE", "IE"),
        ("Krakow, PL", "PL"),
        ("Remote, US", "US"),
        # ---- state / province abbreviations on their own
        ("Cambridge, MA", "US"),
        ("San Jose, CA", "US"),
        ("Austin, TX", "US"),
        ("Washington, DC", "US"),
        ("Ottawa, ON", "CA"),
        ("Halifax, NS", "CA"),
        # ---- non-European tech hubs by city name alone
        ("Bengaluru", "IN"),
        ("Bangalore", "IN"),
        ("Pune", "IN"),
        ("Gurugram", "IN"),
        ("Shenzhen", "CN"),
        ("Tokyo", "JP"),
        ("Singapore", "SG"),
        ("Tel Aviv", "IL"),
        ("Mexico City", "MX"),
        ("Bogotá", "CO"),
        ("Sydney", "AU"),
        ("Cape Town", "ZA"),
        ("Mountain View", "US"),
        ("Vancouver", "CA"),
        # ---- what must stay unknown
        ("2 Locations", None),
        ("5 Locations", None),
        ("Multiple Locations", None),
        ("Nowhereville, QQ", None),  # QQ is not an ISO-2 code
        ("Somewhere, ZZ, QQ", None),
        ("QQ", None),  # not even on its own
        # The two codes where the country beats the US state it shares letters with, for the
        # cities the table does not know (both forms are real rows from the 2026-09-09 run).
        ("Ulm, DE", "DE"),
        ("coimbatore, in", "IN"),
    ],
)
def test_guess_country(text, expected):
    assert guess_country(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A country name beats a state abbreviation that happens to spell it.
        ("Tbilisi, Georgia", "GE"),
        ("Atlanta, GA", "US"),
        ("Georgia", "GE"),
        # A city we know beats a US state code that shares the letters ("DE" = Delaware).
        ("Berlin, DE", "DE"),
        ("Milano, MI", "IT"),
        # Codes only count as whole comma-separated tokens, never inside a word.
        ("Indianapolis, IN, US", "US"),
        ("Indiana, US", "US"),
        ("Instrumentation Lab", None),
        ("Maine Street Software", None),
    ],
)
def test_guess_country_ambiguous_codes(text, expected):
    assert guess_country(text) == expected


def test_guess_country_multi_location_strings():
    """Workday joins resolved multi-location postings with a pipe; the first one decides."""
    assert guess_country("Redmond, WA, US | Bellevue, WA, US") == "US"
    assert guess_country("Hyderabad, TS, IN | Noida, UP, IN") == "IN"
    assert guess_country("Espoo, FI | Berlin, DE") == "FI"


def test_guess_country_uses_the_first_text_that_resolves():
    assert guess_country(None, "", "Hyderabad, TS, IN") == "IN"
    # An earlier rule wins over a later text: the country name beats the other string's city.
    assert guess_country("Bengaluru", "Somewhere in Finland") == "FI"


def test_guess_remote_and_cloudflare_still_work():
    assert guess_remote("Remote - Europe") == "remote"
    assert guess_remote("Hybrid, Helsinki") == "hybrid"
    assert guess_remote("Helsinki", flag=False) == "onsite"
    assert guess_remote("Helsinki") == "unknown"
    assert is_cloudflare_challenge("<title>Just a moment...</title>") is True
    assert is_cloudflare_challenge(None) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Non-European country names spelled out — the same rows the AI screen used to pay for.
        ("Ramat Gan, Israel", "IL"),
        ("Bangkok, Thailand", "TH"),
        ("Kuala Lumpur, Malaysia", "MY"),
        ("Cyberjaya, Selangor, Malaysia", "MY"),
        ("Guadalupe, Mexico", "MX"),
        ("Hong Kong", "HK"),
        ("Shenzhen, China", "CN"),
        ("Sao Paulo, Brazil", "BR"),
        # US state names, as some boards write them.
        ("O'Fallon, Missouri", "US"),
        ("Bellevue, Washington", "US"),
        ("Portage, Michigan", "US"),
        # The longest place name wins, so New Mexico is not Mexico.
        ("Albuquerque, New Mexico", "US"),
        ("Tbilisi, Georgia", "GE"),
        # A spelled-out "Georgia" stays the country even after a US city: names are matched
        # before the city table, and the state is not in the name table. "Atlanta, GA" (the
        # code, above) is read as the US, and that is the form the boards actually use.
        ("Atlanta, Georgia", "GE"),
    ],
)
def test_guess_country_place_names(text, expected):
    assert guess_country(text) == expected
