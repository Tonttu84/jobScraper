"""Rule-based auto-filter. Cheap, deterministic, permissive.

Philosophy (from the candidate): it is worse to lose a good job than to let a bad one through,
because an AI stage reviews everything that survives. So a rule may *drop* only on strong,
unambiguous signals; anything softer becomes ``review`` with the signal recorded for the AI.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime

from jobscraper.config import Profile
from jobscraper.filters.language import detect_language, find_language_requirements
from jobscraper.models import FilterResult, Job
from jobscraper.sources._common import guess_country

RULES_VERSION = "2026-09-10"

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
    """Smallest 'N years' figure that appears next to experience words. None if none found."""
    found: list[int] = []
    for m in _YEARS_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80]
        if not _EXPERIENCE_CONTEXT.search(window):
            continue
        n = int(m.group(1))
        if 0 < n <= 20:
            found.append(n)
    return min(found) if found else None


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


def evaluate(job: Job, profile: Profile) -> FilterResult:
    reasons: list[str] = []
    review: list[str] = []
    signals: dict = {}
    title = job.title or ""
    text = job.text

    # ---------------------------------------------------------------- role
    role = profile.role
    keep_role = _compile_terms(role.title_terms)
    excl_role = _compile_terms(role.exclude_title_terms)
    if excl_role and excl_role.search(title) and not (keep_role and keep_role.search(title)):
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
    if code:
        job.posting_language = code
        signals["posting_language"] = code
        if code in lang.drop_if_written_in:
            reasons.append(f"posting written in {code} (confidence {conf:.2f})")
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
        age_days = (datetime.now(UTC) - posted).days
        signals["age_days"] = age_days
        if age_days > profile.max_age_days:
            reasons.append(f"posting is {age_days} days old (max {profile.max_age_days})")

    status = "drop" if reasons else ("review" if review else "keep")
    return FilterResult(job_id=job.id, status=status, reasons=reasons + review, signals=signals, location_tier=tier)


def dedupe(jobs: list[Job]) -> tuple[list[Job], dict[str, list[str]]]:
    """Collapse the same posting seen on several boards. Returns (unique_jobs, {kept_id: [dup_ids]})."""
    def key(j: Job) -> str:
        t = re.sub(r"[^a-z0-9]+", " ", (j.title or "").lower()).strip()
        c = re.sub(r"[^a-z0-9]+", " ", (j.company or "").lower()).strip()
        c = re.sub(r"\b(oy|ab|oyj|ltd|gmbh|inc|llc|as|sa|bv|plc)\b", "", c).strip()
        return f"{c}|{t}"

    # Prefer direct company boards over aggregators when the same job appears twice.
    priority = {"teamtailor": 0, "ats_boards": 0, "duunitori": 1, "thehub": 1, "tyomarkkinatori": 2,
                "cvee": 2, "cvkeskus": 2, "linkedin": 5, "indeed": 5}
    groups: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        groups[key(j) if j.company else j.id].append(j)
    unique: list[Job] = []
    dups: dict[str, list[str]] = {}
    for members in groups.values():
        members.sort(key=lambda j: (priority.get(j.source, 3), j.id))
        unique.append(members[0])
        if len(members) > 1:
            dups[members[0].id] = [m.id for m in members[1:]]
    return unique, dups


def apply_rules(jobs: list[Job], profile: Profile) -> list[FilterResult]:
    return [evaluate(j, profile) for j in jobs]
