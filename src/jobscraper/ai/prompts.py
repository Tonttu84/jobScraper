"""Prompt text for the two AI stages. Bump PROMPT_VERSION when you change wording so cached
verdicts are recomputed."""

from __future__ import annotations

from jobscraper.config import Profile
from jobscraper.models import FilterResult, Job

PROMPT_VERSION = "2026-09-07.3"


def _policy_block(profile: Profile) -> str:
    loc = profile.location
    return f"""CANDIDATE
Name: {profile.name}
Summary: {profile.summary.strip()}
Skills: {", ".join(profile.skills)}
Wants to learn / interests: {", ".join(profile.interests)}
Working languages: {", ".join(profile.languages.ok)} (fluent). Weak: {", ".join(profile.languages.weak)} (basic, not usable as the main working language).

WHAT COUNTS AS A MATCH
- Seniority: ONLY internship, trainee, graduate, junior, entry-level, or unlabelled roles asking for at
  most {profile.seniority.max_years_keep} years of experience. Mid-level roles ("experienced", "medior",
  "mid", 3+ years required) are out, as are senior/lead/architect: the candidate cannot apply everywhere
  and wants the list limited to entry-level. A university-degree requirement is NOT a blocker: 42/Hive
  graduates are routinely accepted.
- Role: software development / IT in the broad sense (backend, frontend, full-stack, embedded,
  DevOps, QA automation, data engineering, game dev, systems, cloud). Programming-language mismatch is
  a minor issue: the candidate learns fast and is especially keen on Go and Rust.
- Languages: the posting's working language must be English, Finnish, or German. A job on a Polish,
  German, French or Spanish board that only needs English is fine. If Swedish or another language is
  merely "a plus", fine. If it is clearly required as the working language, it's out.
- Location: preferred tier 1 = {", ".join(loc.tier1)}; tier 2 = EU/EEA + Norway; tier 3 = Dubai and
  {", ".join(c for c in loc.tier3 if c != "AE")} (work rights via employer). Remote roles are welcome
  from anywhere; note the stated region. Remote roles paying US salaries are worth extra effort even if
  the wording hints at US-only: mark them as "check eligibility" rather than rejecting, unless the
  text explicitly requires US work authorization or residence.
- Employment type: anything (full-time, part-time, contract, freelance, internship).
"""


PREFILTER_SYSTEM = """You are screening job postings for a specific junior software developer.
Be PERMISSIVE: this is a first pass and a stronger reviewer will look at everything you keep.
Reject only when you are confident the posting is not viable (clearly senior, clearly requires a
natural language the candidate can't work in, clearly on-site outside the target regions, or not a
software/IT job at all). When uncertain, keep it with a middling score and say why in concerns.
Score 0-100 = probability this is worth the candidate's time to read.

Don't deliberate over rejects: as soon as a posting fails one of those minimum requirements, mark it
relevant=false with score 0 immediately and move on — nobody reads the difference between a 15 and a
42. Spend your judgement only on postings that pass the minimum requirements. A programming language
mismatch is never a minimum-requirement failure (see below).

"""

RANK_SYSTEM = """You are a career advisor ranking job postings for a specific junior software developer.
Judge each posting on fit and on realistic chance of getting an interview. Be concrete: name the
skills that match, the gaps, the language/location/seniority risks, and how the candidate should
angle the application (which projects or background to emphasise). Score 0-100 where 80+ means
"apply this week", 50-79 "apply if time allows", below 50 "probably skip".

"""


def system_prompt(stage: str, profile: Profile) -> str:
    head = PREFILTER_SYSTEM if stage == "prefilter" else RANK_SYSTEM
    body = _policy_block(profile)
    if stage == "rank" and profile.cv_text.strip():
        body += "\nFULL CV\n" + profile.cv_text.strip() + "\n"
    return head + body


def job_prompt(job: Job, fr: FilterResult | None, max_chars: int) -> str:
    desc = (job.description or "")[:max_chars]
    if job.description and len(job.description) > max_chars:
        desc += "\n[…truncated…]"
    meta = [
        f"Title: {job.title}",
        f"Company: {job.company or 'unknown'}",
        f"Source: {job.source}",
        f"Location: {job.location_raw or 'unknown'} (country={job.country or '?'}, remote={job.remote}, remote_region={job.remote_region or '?'})",
        f"Employment type: {job.employment_type or 'unknown'}; seniority label: {job.seniority_raw or 'none'}",
        f"Posted: {job.posted_at.date().isoformat() if job.posted_at else 'unknown'}",
        f"Salary: {job.salary_text or 'not stated'}",
        f"Tags: {', '.join(job.tags) if job.tags else '-'}",
    ]
    if fr:
        meta.append(f"Rule-filter status: {fr.status}; notes: {'; '.join(fr.reasons) or '-'}")
        meta.append(f"Detected signals: {fr.signals}")
    return "JOB POSTING\n" + "\n".join(meta) + "\n\nDESCRIPTION\n" + (desc or "(no description available; judge from the title)")
