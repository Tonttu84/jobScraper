"""``sources/_common.py`` helpers — country parsing above all.

Enterprise boards (Eightfold, Oracle, Workday) post locations as "Hyderabad, TS, IN" or
"Redmond, WA, US". Before these tests those ended with ``country=None``, the rule filter sent
them to the AI screen as "on-site with unknown country", and Sonnet paid to reject them.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobscraper.sources._common import (
    ISO2_CODES,
    guess_country,
    guess_remote,
    is_cloudflare_challenge,
    parse_date,
)
from jobscraper.sources._iso3 import ISO3_TO_ISO2


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Amazon's careers board sends a bare ISO 3166-1 alpha-3 code (probe of 2026-10-10).
        ("SGP", "SG"),
        ("FIN", "FI"),
        ("Seattle, WA, USA", "US"),
        ("Berlin, DEU", "DE"),
        ("Dublin, IRL", "IE"),
        ("London, GBR", "GB"),
        # Cities the table does not know, so only the code can decide.
        ("Cork, IRL", "IE"),
        ("Tartu, EST", "EE"),
        # "City, ST, CCC": the last token decides, as it does for ISO-2 (BW is Botswana).
        ("Ulm, BW, DEU", "DE"),
        ("Somewhere, XX, ARE", "AE"),
        # Not ISO-3 codes: airport and city abbreviations stay unknown.
        ("NYC", None),
        ("LAX", None),
        ("SFO", None),
        ("Somewhere, NYC", None),
        # Only an upper-case token counts: "can" is a word, not Canada.
        ("can", None),
        ("Can", None),
        ("Visa and relocation", None),
        ("Somewhere, and", None),
        ("Somewhere, fin", None),
        # Exactly three letters.
        ("Somewhere, DEUX", None),
        ("Somewhere, D3U", None),
    ],
)
def test_guess_country_iso3_codes(text, expected):
    assert guess_country(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "City, State, CC": a country code in capitals as the last of three or more parts beats
        # a place name earlier in the string. Amazon's 189 "…, Georgia, USA" rows were GE.
        ("Atlanta, Georgia, USA", "US"),
        ("Atlanta, Georgia, US", "US"),
        ("Covington, Georgia, USA", "US"),
        ("Belfast, Northern Ireland, GBR", "GB"),
        ("Craigavon, Northern Ireland, GB", "GB"),
        ("Batumi, Adjara, Georgia", "GE"),  # a name as the last part is not a code
        # Two parts keep the old order: the name decides.
        ("Tbilisi, Georgia", "GE"),
        ("Atlanta, Georgia", "GE"),
        # A last token that is also a US state / Canadian province code is not overriding:
        # the name rule still runs first, then the codes rule as before.
        ("Atlanta, Georgia, GA", "GE"),
        ("Indianapolis, Indiana, IN", "US"),
        ("Pune, Maharashtra, IN", "IN"),
        ("Toronto, ON, CA", "CA"),
        # Only capitals override a name; a lower-case last word is left to the old rules.
        ("Helsinki, Finland, no", "FI"),
        # Capitals that are no country code leave the string to the old rules too.
        ("Helsinki, Finland, QQ", "FI"),
    ],
)
def test_guess_country_trailing_code_beats_place_names(text, expected):
    assert guess_country(text) == expected


def test_iso3_table_is_complete_and_maps_to_real_iso2_codes():
    assert len(ISO3_TO_ISO2) == 249
    assert {iso2.lower() for iso2 in ISO3_TO_ISO2.values()} == ISO2_CODES
    assert all(len(k) == 3 and k.isupper() and k.isalpha() for k in ISO3_TO_ISO2)
    assert ISO3_TO_ISO2["FIN"] == "FI" and ISO3_TO_ISO2["USA"] == "US"
    assert "XKX" not in ISO3_TO_ISO2  # Kosovo's code is user-assigned, not ISO


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
    assert guess_remote("Hybridityö Helsingissä") == "hybrid"  # Finnish compound
    assert guess_remote("Meillä on hybridimalli") == "hybrid"
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


def test_parse_date_reads_the_shapes_the_boards_send():
    naive = datetime(2026, 9, 7, 12, 0, 0)
    assert parse_date(naive) == naive.replace(tzinfo=UTC)  # a naive datetime is read as UTC
    assert parse_date(naive.replace(tzinfo=UTC)) == naive.replace(tzinfo=UTC)
    # epoch seconds and milliseconds, both as numbers and as the strings some feeds send
    assert parse_date("1787735594") == datetime.fromtimestamp(1787735594, tz=UTC)
    assert parse_date("1787735594863") == datetime.fromtimestamp(1787735594.863, tz=UTC)
    assert parse_date(1787735594) == datetime.fromtimestamp(1787735594, tz=UTC)
    assert parse_date("2026-09-07T12:00:00Z") == naive.replace(tzinfo=UTC)
    assert parse_date("Mon, 07 Sep 2026 12:00:00 +0000") == naive.replace(tzinfo=UTC)
    assert parse_date("07.09.2026") == datetime(2026, 9, 7, tzinfo=UTC)
    assert parse_date(None) is None and parse_date("") is None
    assert parse_date("not a date at all") is None
