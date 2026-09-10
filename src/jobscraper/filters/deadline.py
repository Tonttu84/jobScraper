"""Application deadlines read out of a posting's own words.

Boards almost never publish a closing date as a field, but the description usually states one in
plain language ("Apply by 13 September 2026", "Hakuaika päättyy 13.9.2026"). Reading it is worth
the regexes twice over: a vacancy that can no longer be applied to becomes a cheap rule drop
instead of an AI bill, and one that is still open can tell the reader how long they have.

The parser is deliberately narrow. A date counts as a deadline only when it follows one of the
cue phrases below within :data:`WINDOW` characters, so "a deadline-driven environment" and the
release dates further down the page say nothing. When in doubt it returns ``None``: a missed
deadline costs one AI screening, an invented one throws a live job away.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

#: How far after a cue phrase a date still belongs to it. One clause, roughly.
WINDOW = 80

#: A date without a year is read as this year unless that puts it further than this behind us —
#: a closing date a fortnight past is a closed vacancy, one ten months past is next year's round.
ROLLOVER_DAYS = 30

#: Sanity bounds. Anything outside them was some other date the cue happened to sit next to.
MAX_AHEAD_DAYS = 730
MAX_BEHIND_DAYS = 365

_ENGLISH = "january february march april may june july august september october november december"
_GERMAN = "januar februar märz april mai juni juli august september oktober november dezember"
_SWEDISH = "januari februari mars april maj juni juli augusti september oktober november december"
_PORTUGUESE = ("janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro "
               "dezembro")
_FINNISH = ("tammikuu helmikuu maaliskuu huhtikuu toukokuu kesäkuu heinäkuu elokuu syyskuu "
            "lokakuu marraskuu joulukuu")


def _month_table() -> dict[str, int]:
    """Every month name the five posting languages use, lowercased, → its number."""
    table: dict[str, int] = {}
    for names in (_ENGLISH, _GERMAN, _SWEDISH, _PORTUGUESE, _FINNISH):
        for number, name in enumerate(names.split(), start=1):
            table.setdefault(name, number)
    for number, name in enumerate(_ENGLISH.split(), start=1):
        table.setdefault(name[:3], number)  # "13 Sep 2026"
    table["sept"] = 9
    for number, name in enumerate(_FINNISH.split(), start=1):
        table[name + "ta"] = number  # partitive: "13. syyskuuta 2026"
    return table


MONTHS: dict[str, int] = _month_table()

#: Cue phrases, per language. Everything here is matched case-insensitively and whole-word.
CUES: tuple[str, ...] = (
    # English
    "application deadline", "applications close", "closing date", "last day to apply",
    "apply by", "closes on", "deadline",
    # Finnish
    "hakuaika päättyy", "viimeinen hakupäivä", "hae viimeistään", "hakemukset viimeistään",
    # German
    "bewerben Sie sich bis", "bewerbungsschluss", "bewerbungsfrist", "bis zum",
    # Portuguese
    "prazo de candidatura", "candidaturas até",
    # Swedish
    "sista ansökningsdag", "ansök senast",
)

#: ``(?!-)`` keeps the bare "deadline" cue away from "deadline-driven" and its relatives, which
#: describe the team's working style rather than a date.
_CUE_RE = re.compile(
    r"\b(?:" + "|".join(r"\s+".join(re.escape(w) for w in cue.split()) for cue in CUES) + r")\b(?!-)",
    re.I,
)

# A day or a year must not be a slice of a longer number: without the lookahead "September 2026"
# reads as the 20th, and a phone number reads as a date.
_DAY = r"(\d{1,2})(?!\d)"
_YEAR = r"(\d{4})(?!\d)"
_MONTH = "|".join(sorted((re.escape(name) for name in MONTHS), key=len, reverse=True))

# "13 September 2026", "13. September 2026", "13 de setembro de 2026", "13 syyskuuta" (no year).
_DAY_MONTH = re.compile(rf"\b{_DAY}\.?\s*(?:de\s+)?({_MONTH})\b\.?(?:\s*,?\s*(?:de\s+)?{_YEAR})?", re.I)
# "September 13, 2026", "Sep 13th 2026", "September 13" (no year).
_MONTH_DAY = re.compile(rf"\b({_MONTH})\b\.?\s+{_DAY}(?:st|nd|rd|th)?(?:\s*,?\s*{_YEAR})?", re.I)
# "13.9.2026", "13/09/2026". The year is required here: without a month name to anchor it,
# "1/2" and "5.30" are far more often a fraction or a time than a date.
_NUMERIC = re.compile(rf"\b{_DAY}[./](\d{{1,2}})(?!\d)[./]{_YEAR}")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

#: ``(day, month, year_or_None)`` out of each pattern's own group order.
_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
    (_ISO, lambda m: (int(m.group(3)), int(m.group(2)), m.group(1))),
    (_NUMERIC, lambda m: (int(m.group(1)), int(m.group(2)), m.group(3))),
    (_DAY_MONTH, lambda m: (int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3))),
    (_MONTH_DAY, lambda m: (int(m.group(2)), MONTHS[m.group(1).lower()], m.group(3))),
)


def _guess_year(day: int, month: int, today: date) -> date | None:
    """A day and a month with no year: this year, or next when this year is well behind us."""
    try:
        found = date(today.year, month, day)
    except ValueError:  # 31 February and friends
        return None
    if found >= today - timedelta(days=ROLLOVER_DAYS):
        return found
    try:
        return found.replace(year=today.year + 1)
    except ValueError:  # 29 February, and next year is not a leap year
        return None


def _build(day: int, month: int, year: str | None, today: date) -> date | None:
    """One candidate as a real date, or ``None`` if it is impossible or absurd."""
    if year is None:
        found = _guess_year(day, month, today)
        if found is None:
            return None
    else:
        try:
            found = date(int(year), month, day)
        except ValueError:
            return None
    if not today - timedelta(days=MAX_BEHIND_DAYS) <= found <= today + timedelta(days=MAX_AHEAD_DAYS):
        return None
    return found


def _first_date(window: str, today: date) -> date | None:
    """The earliest readable date in ``window``, whichever format it is written in."""
    candidates = [(m.start(), read(m)) for pattern, read in _PATTERNS for m in pattern.finditer(window)]
    for _, (day, month, year) in sorted(candidates, key=lambda found: found[0]):
        built = _build(day, month, year, today)
        if built is not None:
            return built
    return None


def find_deadline(text: str | None, today: date) -> date | None:
    """The application deadline stated in ``text``, or ``None`` if it states none we can trust."""
    if not text:
        return None
    for cue in _CUE_RE.finditer(text):
        found = _first_date(text[cue.end(): cue.end() + WINDOW], today)
        if found is not None:
            return found
    return None
