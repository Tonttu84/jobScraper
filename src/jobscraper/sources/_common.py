"""Helpers shared by adapters: date parsing, country normalization, remote detection,
and the sanity check that decides whether a posting body is a description at all."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from jobscraper.http import strip_html


def _words(text: str) -> tuple[str, ...]:
    """The whitespace-separated words of a block literal, so the tables below stay readable."""
    return tuple(text.split())


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
    # Non-European countries, so an enterprise board's "Bangkok, Thailand" is filtered by the
    # rules instead of being sent to the AI screen as an on-site job of unknown country.
    "israel": "IL", "thailand": "TH", "malaysia": "MY", "china": "CN", "hong kong": "HK",
    "japan": "JP", "singapore": "SG", "singapur": "SG", "south korea": "KR", "korea": "KR",
    "taiwan": "TW", "vietnam": "VN", "viet nam": "VN", "philippines": "PH", "indonesia": "ID",
    "brazil": "BR", "brasil": "BR", "mexico": "MX", "méxico": "MX", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "perú": "PE", "costa rica": "CR",
    "egypt": "EG", "morocco": "MA", "tunisia": "TN", "senegal": "SN", "south africa": "ZA",
    "nigeria": "NG", "kenya": "KE", "new zealand": "NZ", "saudi arabia": "SA", "qatar": "QA",
    "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK",
}

#: US state names, as several boards spell the location ("O'Fallon, Missouri"). Georgia is
#: deliberately missing: as a bare word it is the country on a European job board, and
#: ``COUNTRY_NAMES`` already claims it. Maine is missing for the same reason ("Maine Street
#: Software" is not a location). The two-letter codes live in ``_US_STATES``.
US_STATE_NAMES: dict[str, str] = dict.fromkeys(
    (
        *_words(
            """alabama alaska arizona arkansas california colorado connecticut delaware florida
            hawaii idaho illinois indiana iowa kansas kentucky louisiana maryland massachusetts
            michigan minnesota mississippi missouri montana nebraska nevada ohio oklahoma oregon
            pennsylvania tennessee texas utah vermont virginia washington wisconsin wyoming"""
        ),
        "new hampshire", "new jersey", "new mexico", "new york state", "north carolina",
        "north dakota", "rhode island", "south carolina", "south dakota", "west virginia",
    ),
    "US",
)

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
    # Non-European hubs. They are here for the *filter*: an enterprise board posting
    # "Bengaluru" or "Redmond" with no country field used to look like an on-site job of
    # unknown country, which the rules pass on permissively and the AI screen then pays to reject.
    "hyderabad": "IN", "bangalore": "IN", "bengaluru": "IN", "noida": "IN", "pune": "IN",
    "gurgaon": "IN", "gurugram": "IN", "chennai": "IN", "mumbai": "IN", "delhi": "IN",
    "shanghai": "CN", "shenzhen": "CN", "beijing": "CN", "hangzhou": "CN", "tokyo": "JP",
    "osaka": "JP", "seoul": "KR", "taipei": "TW", "hsinchu": "TW", "singapore": "SG",
    "sydney": "AU", "melbourne": "AU", "são paulo": "BR", "sao paulo": "BR", "mexico city": "MX",
    "buenos aires": "AR", "bogotá": "CO", "bogota": "CO", "tel aviv": "IL", "cairo": "EG",
    "johannesburg": "ZA", "cape town": "ZA", "toronto": "CA", "vancouver": "CA",
    "montreal": "CA", "montréal": "CA", "redmond": "US", "seattle": "US", "austin": "US",
    "mountain view": "US", "san jose": "US", "san francisco": "US", "new york": "US",
    "boston": "US", "chicago": "US", "atlanta": "US", "dallas": "US", "raleigh": "US",
    "phoenix": "US", "denver": "US",
}

#: Real ISO 3166-1 alpha-2 codes (tzdata's ``iso3166.tab``). A two-letter location token that
#: is not in here is a state, a postcode fragment or noise — never a country.
ISO2_CODES: frozenset[str] = frozenset(_words("""
    ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn bo bq
    br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm
    do dz ec ee eg eh er es et fi fj fk fm fo fr ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs
    gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn
    kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq
    mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm
    pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv
    sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug um us uy uz va vc ve vg vi
    vn vu wf ws ye yt za zm zw
"""))

#: Two-letter tokens that name a country without being its ISO-2 code.
_CODE_ALIASES: dict[str, str] = {"uk": "GB"}

#: US state (+ DC) abbreviations, as boards write them: "Redmond, WA, US", "Cambridge, MA".
_US_STATES: frozenset[str] = frozenset(_words("""
    al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh
    nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc
"""))

#: Canadian provinces, minus the five that are also ISO-2 countries (NL, PE, SK, NU, YT):
#: "Utrecht, NL" is the Netherlands, not Newfoundland, on every board we scrape.
_CA_PROVINCES: frozenset[str] = frozenset(_words("ab bc mb nb ns nt on qc"))

#: Codes that are both an ISO-2 country and a US state, where the country still wins.
#: Chosen from the run of 2026-09-09: 111 postings ended in ", DE" (Germany) and 135 in
#: ", IN" (India), none in Delaware or Indiana. For every other collision (MA, CA, GA, VA,
#: MD, ME, MT, CO, PA, AL, …) the state wins, so "Cambridge, MA" is Massachusetts, not Morocco.
_STATE_CODE_IS_COUNTRY: dict[str, str] = {"de": "DE", "in": "IN"}

# "etäty\w*" so the Finnish stem matches inflected forms too ("etätyönä", "etätyömahdollisuus").
_REMOTE_RE = re.compile(r"\b(remote|etäty\w*|etänä|home ?office|fully distributed|work from anywhere|telecommute)\b", re.I)
_HYBRID_RE = re.compile(r"\b(hybrid|hybridi)\b", re.I)

#: Country and state names as patterns, longest name first: that is what keeps "New Mexico"
#: out of Mexico and "South Dakota" out of Dakota, whatever order the dicts were written in.
_PLACE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![a-z]){re.escape(name)}(?![a-z])"), iso)
    for name, iso in sorted(
        {**COUNTRY_NAMES, **US_STATE_NAMES}.items(), key=lambda kv: -len(kv[0])
    )
)

#: Same for the city table.
_CITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![a-z]){re.escape(city)}(?![a-z])"), iso)
    for city, iso in sorted(CITY_COUNTRY.items(), key=lambda kv: -len(kv[0]))
)

#: Markers of a Cloudflare interstitial ("Just a moment…"), as served instead of the page.
#: Deliberately specific: the bare word "cloudflare" appears in plenty of real job ads.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl",
    "challenge-platform",
    "cf_chl_opt",
    "checking your browser",
    "cf-browser-verification",
)


#: Adapters get descriptions as text or as assembled HTML — strip only when there are tags.
_HTML_RE = re.compile(
    r"<(?:br|p|div|ul|ol|li|h[1-6]|strong|em|a|span|table|html|body|style|script)\b|</[a-z]+>",
    re.IGNORECASE,
)
#: A CSS/JS rule block: ``{ … prop: value … }``. Non-greedy and brace-free inside, so the
#: inner blocks of an ``@keyframes`` rule match one by one.
_RULE_BLOCK_RE = re.compile(r"\{[^{}]*?[A-Za-z-]+\s*:[^{}]*?\}")
#: Above this share of the text sitting inside such blocks it is a page shell, not a posting.
_MAX_RULE_SHARE = 0.25
#: Fewer letters than this and there is nothing for the AI stage to read.
MIN_DESCRIPTION_LETTERS = 40


def _letters(text: str) -> int:
    return sum(1 for c in text if c.isalpha())


def clean_description(text: str | None) -> str | None:
    """A posting body (HTML or plain text) → readable prose, or ``None`` when there is none.

    Two kinds of non-description are turned into ``None`` so the AI prompt says "no
    description available" instead of feeding the model junk:

    * **page-shell boilerplate** — a career site that serves its SPA loader, or a tenant who
      pasted a whole HTML page into the description field and had the tags stripped for them,
      leaves a wall of CSS declarations and selectors behind;
    * **placeholders** — ``"..."``, ``"n/a"``, an empty paragraph: anything with almost no
      prose in it.
    """
    if not text:
        return None
    body = (strip_html(text) if _HTML_RE.search(text) else text) or ""
    body = body.strip()
    if _letters(body) < MIN_DESCRIPTION_LETTERS:
        return None
    covered = sum(len(m.group(0)) for m in _RULE_BLOCK_RE.finditer(body))
    if covered / len(body) > _MAX_RULE_SHARE:
        return None
    return body


def is_cloudflare_challenge(html: str | None) -> bool:
    """True when ``html`` is Cloudflare's bot check rather than the page that was asked for."""
    if not html:
        return False
    low = html[:4000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _iso2(token: str) -> str | None:
    """The country for a token that can only denote a country (a state code is not one)."""
    if token in ISO2_CODES:
        return token.upper()
    return _CODE_ALIASES.get(token)


def _code_country(token: str) -> str | None:
    """The country for a standalone two-letter location token, state codes included."""
    if len(token) != 2 or not token.isalpha():
        return None
    if token in _STATE_CODE_IS_COUNTRY:
        return _STATE_CODE_IS_COUNTRY[token]
    if token in _US_STATES:
        return "US"
    if token in _CA_PROVINCES:
        return "CA"
    return _iso2(token)


def _country_from_codes(segment: str) -> str | None:
    """Read the country out of a comma-separated location string.

    "City, ST, CC" (Eightfold, Oracle, Workday) puts the country last, so a three-or-more
    part string is decided by its last token and nothing else — that is what makes
    "Amsterdam, NH, NL" the Netherlands and not New Hampshire. A two-part "City, XX" is
    ambiguous, so a city we know wins first ("Milano, MI" is Milan, not Michigan). Anything
    else is scanned token by token, which catches the trailing-postcode shape
    ("Utrecht, NL, 3584 AB") while never matching a code inside a word.
    """
    tokens = [t.strip().lower() for t in segment.split(",") if t.strip()]
    if len(tokens) >= 3:
        iso = _iso2(tokens[-1])
        if iso:
            return iso
    if len(tokens) == 2:
        city = CITY_COUNTRY.get(tokens[0])
        if city:
            return city
    for token in tokens if len(tokens) != 2 else tokens[1:]:
        code = _code_country(token)
        if code:
            return code
    return None


def _location_segments(texts: tuple[str | None, ...]) -> list[str]:
    """The location strings to try, in order. Workday joins several with " | "."""
    segments: list[str] = []
    for text in texts:
        if not text:
            continue
        segments.extend(s for s in (part.strip() for part in str(text).split("|")) if s)
    return segments


def guess_country(*texts: str | None) -> str | None:
    """Best-effort ISO2 from location strings.

    Three rules, each tried against every location string before the next one starts:
    a country or US state name (or a bare country code), then comma-separated
    country/state codes, then a well-known city.
    """
    segments = _location_segments(texts)
    for segment in segments:
        low = segment.lower()
        if len(low) == 2:
            iso = _iso2(low)
            if iso:
                return iso
        for pattern, iso in _PLACE_PATTERNS:
            if pattern.search(low):
                return iso
    for segment in segments:
        code = _country_from_codes(segment)
        if code:
            return code
    for segment in segments:
        low = segment.lower()
        for pattern, iso in _CITY_PATTERNS:
            if pattern.search(low):
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
