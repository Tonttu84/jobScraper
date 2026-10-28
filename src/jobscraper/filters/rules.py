"""Rule-based auto-filter. Cheap, deterministic, permissive.

Philosophy (from the candidate): it is worse to lose a good job than to let a bad one through,
because an AI stage reviews everything that survives. So a rule may *drop* only on strong,
unambiguous signals; anything softer becomes ``review`` with the signal recorded for the AI.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import NamedTuple

from jobscraper.config import LocationPolicy, Profile
from jobscraper.filters.deadline import find_deadline
from jobscraper.filters.evergreen import is_evergreen
from jobscraper.filters.language import detect_language, find_language_requirements
from jobscraper.models import FilterResult, Job, LivenessCheck
from jobscraper.sources._common import guess_country

RULES_VERSION = "2026-10-28a"

#: A deadline this near is worth putting in front of the reader (report bullet, UI tag).
CLOSING_SOON_DAYS = 7

#: A language drop whose detection confidence is less than this far above
#: ``languages.min_confidence`` is reported as borderline (it is still dropped).
LANGUAGE_BORDERLINE_MARGIN = 0.10


class LanguageBorderline(NamedTuple):
    """A posting dropped for its language alone, on a detection just over the threshold."""

    job_id: str
    source: str
    language: str
    confidence: float

_YEARS_RE = re.compile(
    r"(?:(?:at least|minimum|min\.?|minimum of|over|more than|vähintään|yli|mindestens|mind\.|über|"
    r"minst|co najmniej|minimaal|au moins|al menos)\s+)?"
    r"(\d{1,2})(?:\s*[-–to]+\s*(\d{1,2}))?\s*\+?\s*"
    r"(?:years?|yrs?|vuotta|vuoden|v\.|jahre?n?|år|lat|jaar|ans|años)\b"
    r"(?![^.\n]{0,25}\b(?:old|age|ikä|alt|company|history|founded|market|experience in the industry|"
    r"track record of the company|of operation))",
    re.I,
)
_EXPERIENCE_CONTEXT = re.compile(
    r"(experience|kokemus|kokemusta|erfahrung|erfahrene|professional|työkokemus|berufserfahrung|"
    r"doświadczen|ervaring|expérience|experiencia|background|working|in a similar|of software|of development)",
    re.I,
)
_REMOTE_US_ONLY = re.compile(r"\b(us|usa|u\.s\.|united states|north america|canada)[- ]?(only|based|residents?|citizens?|time ?zones?)\b|\bmust (be )?(located|reside|live) in the (us|usa|united states)\b|\bwork authorization in the (us|united states)\b", re.I)
_REMOTE_EU = re.compile(r"\b(europe|eu|eea|emea|european union|cet|cest|central european|nordic|finland|germany|netherlands|poland|estonia)\b", re.I)
_REMOTE_WORLD = re.compile(r"\b(worldwide|anywhere|global(ly)?|any location|all countries|international)\b", re.I)


def _compile_terms(terms: list[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    # Plain terms match at a word start only ("develop" → Developer/Development, "lead" → Lead Developer);
    # anything with regex syntax is used verbatim so the profile can demand exact boundaries.
    return re.compile("|".join(f"(?:\\b{t})" if re.fullmatch(r"[\w\\. ]+", t) else f"(?:{t})" for t in terms), re.I)


def _years_required(text: str) -> int | None:
    """First 'N years' figure (in document order) next to experience words. None if none found.

    Postings state the headline requirement first ("Minimum 5 years of engineering experience")
    and per-tool figures after it ("1 year of experience with LangSmith"); taking the smallest
    figure read such a posting as a 1-year role. A range ("1-3 years") yields its lower bound.
    """
    for m in _YEARS_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80]
        if not _EXPERIENCE_CONTEXT.search(window):
            continue
        n = int(m.group(1))
        if 0 < n <= 20:
            return n
    return None


_MID_LABEL_RE = re.compile(r"(mid|middle|medior|regular|intermediate|experienced)", re.I)
_REMOTE_COUNTRY_ONLY = re.compile(r"\b([A-Za-z][A-Za-z .]{1,30}?)\s+only\b", re.I)


def classify_remote_region(text: str | None) -> str:
    if not text:
        return "unknown"
    if _REMOTE_US_ONLY.search(text):
        return "us_only"
    if _REMOTE_EU.search(text):
        return "europe"
    if _REMOTE_WORLD.search(text):
        return "worldwide"
    # "GB only", "UK only", "Germany only": remote in name, but closed to applicants elsewhere.
    match = _REMOTE_COUNTRY_ONLY.search(text)
    if match:
        code = guess_country(match.group(1))
        if code:
            return f"country_only:{code}"
    return "unknown"


def _country_tier(country: str | None, profile: Profile) -> int | None:
    loc = profile.location
    if country in loc.tier1:
        return 1
    if country in loc.tier2:
        return 2
    if country in loc.tier3:
        return 3
    return None


def _location_tier(job: Job, profile: Profile) -> int | None:
    return _country_tier(job.country, profile)


def liveness_reason(check: LivenessCheck | None) -> str | None:
    """The drop reason a closed or gone page check stands for (None for open/unknown/no check).

    Built here rather than inline in :func:`evaluate` because ``tailor`` refuses a closed job
    with the same words; ``tests/test_audit.py`` covers both wordings explicitly.
    """
    if check is None:
        return None
    day = check.checked_at.date().isoformat()
    if check.status == "closed":
        return f"posting closed (checked {day})"
    if check.status == "gone":
        return f"posting gone ({check.reason}, checked {day})"
    return None


def today_utc() -> date:
    """The date every age and deadline rule counts from (tests freeze it here)."""
    return datetime.now(UTC).date()


def evaluate(job: Job, profile: Profile, today: date | None = None, *,
             borderline: list[LanguageBorderline] | None = None,
             liveness: LivenessCheck | None = None) -> FilterResult:
    """The rule filter's verdict on one job.

    ``borderline``, when given, collects the job if the posting language alone dropped it on a
    detection within :data:`LANGUAGE_BORDERLINE_MARGIN` of the threshold. It only listens: the
    verdict is the same with or without it. ``liveness`` is the job's latest conclusive page
    check (:meth:`Store.liveness`); a closed or gone one drops the job.
    """
    reasons: list[str] = []
    review: list[str] = []
    signals: dict = {}
    title = job.title or ""
    text = job.text
    today = today or today_utc()

    # -------------------------------------------------------------- liveness
    # The posting's own page said it takes no more applications, or is not there at all
    # (`jobscraper check-live`). The hardest evidence the filter gets, so it is the first reason;
    # an open or an inconclusive check says nothing and leaves the verdict alone.
    dead = liveness_reason(liveness)
    if dead is not None:
        reasons.append(dead)
        signals["liveness"] = liveness.status
        signals["liveness_checked"] = liveness.checked_at.date().isoformat()

    # ---------------------------------------------------------------- role
    role = profile.role
    keep_role = _compile_terms(role.title_terms)
    excl_role = _compile_terms(role.exclude_title_terms)
    always_role = _compile_terms(role.drop_always_title_terms)
    if always_role and always_role.search(title):
        # Closed to this candidate whatever the keep lists say (e.g. "Werkstudent", "Master's
        # thesis" when the role requires current enrolment): one reason, no role-gate verdict.
        reasons.append(f"title excluded by profile regardless of role/level: {title!r}")
    elif excl_role and excl_role.search(title) and not (keep_role and keep_role.search(title)):
        reasons.append(f"title looks like a non-software role: {title!r}")
    elif keep_role and not keep_role.search(title):
        # Not obviously a dev title; give the description one chance before dropping.
        if job.description and keep_role.search(job.description[:600]):
            review.append("title not obviously software, description mentions it")
        else:
            reasons.append(f"title not a software/IT role: {title!r}")

    # ------------------------------------------------------------ seniority
    sen = profile.seniority
    drop_sen = _compile_terms(sen.drop_title_terms)
    keep_sen = _compile_terms(sen.keep_title_terms)
    drop_label = _compile_terms(sen.drop_label_terms)
    if keep_sen and keep_sen.search(title):
        signals["seniority"] = "kept_by_title"
    elif drop_sen and drop_sen.search(title):
        reasons.append(f"title level excluded by profile: {title!r}")
        signals["seniority"] = "excluded_by_title"
    else:
        signals["seniority"] = "unlabeled"
        # Boards label seniority separately from the title (justjoin "mid", nofluffjobs "Mid",
        # devitjobs "Regular"): an excluded label on an unlabelled title is as strong as an
        # excluded title. ``keep_title_terms`` wins over both lists, as it does for titles.
        label = job.seniority_raw or ""
        if label and not (keep_sen and keep_sen.search(label)) and (
            (drop_sen and drop_sen.search(label)) or (drop_label and drop_label.search(label))
        ):
            reasons.append(f"seniority label {label!r} excluded by profile")
            signals["seniority"] = "excluded_by_label"
    years = _years_required(text)
    if years is not None:
        signals["years_required"] = years
        if years > sen.max_years_review:
            if signals["seniority"] != "kept_by_title":
                reasons.append(f"asks for {years}+ years of experience")
            else:
                review.append(f"title matches the profile level terms but mentions {years} years")
        elif years > sen.max_years_keep:
            review.append(f"asks for {years} years of experience")

    # ------------------------------------------------------------- language
    lang = profile.languages
    code, conf = detect_language(job.description or job.title, lang.min_confidence)
    language_reason = None
    if code:
        job.posting_language = code
        signals["posting_language"] = code
        if code in lang.drop_if_written_in:
            language_reason = f"posting written in {code} (confidence {conf:.2f})"
            reasons.append(language_reason)
        elif code in lang.weak:
            review.append(f"posting written in {code}")
    req = find_language_requirements(text)
    if req.required:
        signals["languages_required"] = sorted(req.required)
    if req.optional:
        signals["languages_optional"] = sorted(req.optional)
    for lc in sorted(req.required):
        if lc in lang.ok:
            continue
        evidence = req.evidence.get(lc, "")
        if lc in lang.weak:
            review.append(f"requires {lc}: {evidence!r}")
        else:
            reasons.append(f"requires {lc}: {evidence!r}")

    # ------------------------------------------------------------- location
    tier = _location_tier(job, profile)
    remote_region = classify_remote_region(" ".join(p for p in (job.remote_region, job.location_raw) if p))
    if remote_region == "unknown" and job.remote == "remote":
        remote_region = classify_remote_region(job.description)
    signals["remote"] = job.remote
    signals["remote_region"] = remote_region
    if job.remote == "remote":
        tier = tier or 0
        if remote_region == "us_only":
            review.append("remote but text suggests US-only; worth checking (high pay)")
        elif remote_region.startswith("country_only:"):
            only = remote_region.split(":", 1)[1]
            if _country_tier(only, profile) is None:
                review.append(f"remote but restricted to {only}, outside target countries")
    elif tier is None:
        if job.country:
            reasons.append(f"on-site in {job.country}, outside target countries")
        elif job.remote == "unknown":
            review.append("location unknown")
        else:
            # Permissive: an on-site job whose country we could not parse (LinkedIn rows queried as
            # "European Union" often have no location) goes to the AI stage instead of the bin.
            review.append("on-site with unknown country")
    if tier == 3 and job.country in profile.location.notes:
        signals["work_rights_note"] = profile.location.notes[job.country]

    # ------------------------------------------------------------- staleness
    if job.posted_at is not None:
        posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=UTC)
        age_days = (today - posted.date()).days
        signals["age_days"] = age_days
        if age_days > profile.max_age_days:
            reasons.append(f"posting is {age_days} days old (max {profile.max_age_days})")

    # -------------------------------------------------------------- deadline
    # A closing date the posting states itself is harder evidence than its age: past it, the
    # vacancy is shut whatever the board still shows. Ahead of it, the days left are a signal
    # for the AI stages and something the reader can act on.
    deadline = find_deadline(text, today)
    if deadline is not None:
        signals["deadline"] = deadline.isoformat()
        if deadline < today:
            reasons.append(f"application deadline passed on {deadline.isoformat()}")
        else:
            signals["closes_in_days"] = (deadline - today).days

    # ------------------------------------------------------------- evergreen
    # A pipeline advert, a talent pool or a register-your-interest form is not a vacancy anyone
    # can start on Monday. Never a drop and never a review: the same wording turns up on pages
    # that do list a real opening, and the AI stages read a posting better than a phrase list.
    # It travels as a signal so the ranker, the refiner, the report and the UI agree on it.
    cue = is_evergreen(title, text)
    if cue:
        signals["evergreen"] = cue

    status = "drop" if reasons else ("review" if review else "keep")
    if (borderline is not None and language_reason is not None and reasons == [language_reason]
            and conf < lang.min_confidence + LANGUAGE_BORDERLINE_MARGIN):
        borderline.append(LanguageBorderline(job.id, job.source, code, conf))
    return FilterResult(job_id=job.id, status=status, reasons=reasons + review, signals=signals, location_tier=tier)


#: Legal forms dropped from the end of a company name (and "oy"/"oyj" from its start too, as in
#: "Oy Hartwall Ab") before dedupe compares it. Multi-word forms are the tokens left after
#: punctuation is turned into spaces ("A/S" -> "a s", "Sp. z o.o." -> "sp z o o").
COMPANY_LEGAL_FORMS: tuple[str, ...] = (
    "oy", "oyj", "ab", "ltd", "limited", "gmbh", "ag", "se", "kg", "inc", "llc", "corp",
    "corporation", "co", "as", "a s", "aps", "sa", "sas", "sarl", "srl", "sro", "s r o",
    "sp z o o", "bv", "nv", "plc", "ou", "group", "holding",
)
#: Country or region words dropped from the end of a company name ("Academic Work Finland Oy").
COMPANY_REGION_WORDS: tuple[str, ...] = (
    "finland", "suomi", "sweden", "sverige", "norway", "norge", "denmark", "danmark", "estonia",
    "eesti", "germany", "deutschland", "nordics", "nordic", "baltics", "europe", "international",
    "global", "uk",
)
_COMPANY_LEADING_FORMS: tuple[str, ...] = ("oy", "oyj")
_COMPANY_TRAILING = sorted((tuple(w.split()) for w in COMPANY_LEGAL_FORMS + COMPANY_REGION_WORDS),
                           key=len, reverse=True)


def company_key(name: str | None) -> str:
    """Normalized company name for dedupe: lowercase ASCII words, legal forms and a trailing
    country/region word removed. Never strips the last word, so a name is never emptied."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).split()
    while len(words) > 1 and words[0] in _COMPANY_LEADING_FORMS:
        words = words[1:]
    stripped = True
    while stripped:
        stripped = False
        for form in _COMPANY_TRAILING:
            if len(words) > len(form) and tuple(words[-len(form):]) == form:
                words = words[:-len(form)]
                stripped = True
                break
    return " ".join(words)


def fold_aliases(aliases: dict[str, str] | None) -> dict[str, str]:
    """``company_aliases`` from sources.yaml with both sides put through :func:`company_key`."""
    return {company_key(k): company_key(v) for k, v in (aliases or {}).items()}


def posting_key(job: Job, alias_map: dict[str, str] | None = None) -> tuple[str, str]:
    """(company, title) normalised the way :func:`dedupe` compares postings across boards.

    ``alias_map`` is the output of :func:`fold_aliases`. The report reuses this to spot a
    duplicate that slipped past dedupe into the ranked section.
    """
    title = re.sub(r"[^a-z0-9]+", " ", (job.title or "").lower()).strip()
    company = company_key(job.company)
    return (alias_map or {}).get(company, company), title


def dedupe(jobs: list[Job], location: LocationPolicy | None = None, *,
           aliases: dict[str, str] | None = None) -> tuple[list[Job], dict[str, list[str]]]:
    """Collapse the same posting seen on several boards. Returns (unique_jobs, {kept_id: [dup_ids]}).

    Boards spell one employer differently ("Academic Work" / "Academic Work Finland Oy", "VOIS" /
    "Vodafone"), and on 2026-09-13 that cost two Opus verdicts for one job. So the company is
    normalized (legal forms, a trailing country word), mapped through ``aliases`` (the
    ``company_aliases:`` section of sources.yaml), and under the same title a longer name merges
    into one that is its word-prefix — but only when that shorter name has two words or more:
    "Polar" and "Polar Squad", "HSB" and "HSB Technical" are different employers.

    Where the job is comes before which board it came from, because dedupe runs *before* the rule
    filter: the losers are marked duplicates and never looked at again, so a copy kept for its
    source alone can then be dropped for being outside the target countries while the well-located
    sibling no longer exists to be kept in its place. A multinational advertises the same title in
    several countries at once (Oura's Android role in the US and in Finland, wolt's sales role
    across four), and picking the wrong one silently loses a job the candidate could take. An
    unknown country is not yet a reason to reject, so it outranks a country we know is out of
    reach; a remote posting is kept whatever its country says, when the profile keeps all remote.
    With no location given every copy ranks the same and the source priority decides, as before.
    """
    alias_map = fold_aliases(aliases)

    # Prefer direct company boards over aggregators when the same job appears twice.
    # valtiolle/kuntarekry outrank Työmarkkinatori although TMT carries the same public-sector
    # postings: those two serve the whole advert on the posting page with no rate limit, while
    # the TMT detail endpoint 403s after a few hundred calls and is capped at 150 per run, so
    # most of its copies reach the AI stage with no description at all.
    priority = {"teamtailor": 0, "ats_boards": 0, "duunitori": 1, "thehub": 1, "valtiolle": 1,
                "kuntarekry": 1, "tyomarkkinatori": 2, "cvee": 2, "cvkeskus": 2,
                "linkedin": 5, "indeed": 5}

    tiers = [{c.upper() for c in t} for t in (location.tier1, location.tier2, location.tier3)] if location else []

    def tier_rank(j: Job) -> int:
        if location is None:
            return 0
        country = (j.country or "").upper()
        for rank, codes in enumerate(tiers):
            if country in codes:
                return rank
        if not j.country or (j.remote == "remote" and location.keep_all_remote):
            return 3
        return 4

    groups: dict[tuple[str, ...], list[Job]] = defaultdict(list)
    for j in jobs:
        groups[posting_key(j, alias_map) if j.company else (j.id,)].append(j)

    # Prefix merge, compared only among groups that share a title. Sorted, a parent name comes
    # before the longer names that extend it; a one-word name is never a parent (the guard).
    by_title: dict[str, list[str]] = defaultdict(list)
    for k in groups:
        if len(k) == 2:
            by_title[k[1]].append(k[0])
    parent_of: dict[tuple[str, ...], tuple[str, ...]] = {}
    for title, companies in by_title.items():
        parents: list[str] = []
        for c in sorted(companies):
            parent = next((p for p in parents if c.startswith(p + " ")), None)
            if parent is not None:
                parent_of[(c, title)] = (parent, title)
            elif " " in c:
                parents.append(c)
    merged: dict[tuple[str, ...], list[Job]] = {}
    for k, members in groups.items():
        merged.setdefault(parent_of.get(k, k), []).extend(members)

    unique: list[Job] = []
    dups: dict[str, list[str]] = {}
    for members in merged.values():
        members.sort(key=lambda j: (tier_rank(j), priority.get(j.source, 3), j.id))
        unique.append(members[0])
        if len(members) > 1:
            dups[members[0].id] = [m.id for m in members[1:]]
    return unique, dups


def apply_rules(jobs: list[Job], profile: Profile, *,
                borderline: list[LanguageBorderline] | None = None,
                liveness: Mapping[str, LivenessCheck] | None = None) -> list[FilterResult]:
    """:func:`evaluate` over ``jobs``; ``borderline`` collects the near-threshold language drops,
    ``liveness`` holds each job's latest conclusive page check by job id."""
    live = liveness or {}
    return [evaluate(j, profile, borderline=borderline, liveness=live.get(j.id)) for j in jobs]
