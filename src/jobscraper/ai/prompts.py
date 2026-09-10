"""Prompt text for the two AI stages. Bump PROMPT_VERSION when you change wording so cached
verdicts are recomputed.

Nothing here knows who the candidate is: the role label, the seniority and role rules and any
extra bullets come from ``profile.prompt`` (:class:`~jobscraper.config.PromptPolicy`), and the
language and location bullets are generated from the same policies the rule filter uses.
"""

from __future__ import annotations

from jobscraper.config import LanguagePolicy, LocationPolicy, Profile
from jobscraper.filters.language import LANGUAGE_NAMES
from jobscraper.models import AIVerdict, FilterResult, Job

PROMPT_VERSION = "2026-09-09.1"


def _language_name(code: str) -> str:
    """"fi" → "Finnish"; an unknown code is shown as-is so nothing silently disappears."""
    names = LANGUAGE_NAMES.get(code.lower())
    return names[0].capitalize() if names else code


def _join(items: list[str], last: str = "or") -> str:
    """["a", "b", "c"] → "a, b or c"."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {last} {items[-1]}"


def _languages_rule(lang: LanguagePolicy) -> str:
    ok = [_language_name(c) for c in lang.ok]
    parts = [f"the posting's working language must be {_join(ok) or 'any language the candidate reads'}."]
    if ok:
        parts.append(f"A posting on a board in another language that only needs {ok[0]} is fine.")
    if lang.weak:
        parts.append(f"If {_join([_language_name(c) for c in lang.weak])} or another language is "
                     f'merely "a plus", fine.')
    parts.append("If it is clearly required as the working language, it's out.")
    return " ".join(parts)


def _location_rule(loc: LocationPolicy) -> str:
    tiers = [f"tier 1 = {', '.join(loc.tier1)}" if loc.tier1 else ""]
    if loc.tier2:
        tiers.append(f"tier 2 = {', '.join(loc.tier2)}")
    if loc.tier3:
        tiers.append(f"tier 3 = {', '.join(loc.tier3)} (work rights via employer)")
    text = "preferred " + "; ".join(t for t in tiers if t) if any(tiers) else "no country preference"
    if loc.keep_all_remote:
        return text + ". Remote roles are welcome from anywhere; note the stated region."
    return text + ". Remote roles count only when their stated region overlaps those countries."


def _weak_line(lang: LanguagePolicy) -> str:
    """" Weak: sv (basic, …)" or nothing when the candidate has no weak languages."""
    if not lang.weak:
        return ""
    return f" Weak: {', '.join(lang.weak)} (basic, not usable as the main working language)."


def _policy_block(profile: Profile) -> str:
    sen = profile.seniority
    bullets = [
        "- Seniority: " + profile.prompt.seniority_rule.format(
            max_years_keep=sen.max_years_keep, max_years_review=sen.max_years_review),
        f"- Role: {profile.prompt.role_rule}",
        f"- Languages: {_languages_rule(profile.languages)}",
        f"- Location: {_location_rule(profile.location)}",
        *(f"- {rule.strip()}" for rule in profile.prompt.extra_rules if rule.strip()),
        "- Employment type: anything (full-time, part-time, contract, freelance, internship).",
    ]
    return f"""CANDIDATE
Name: {profile.name}
Summary: {profile.summary.strip()}
Skills: {", ".join(profile.skills)}
Wants to learn / interests: {", ".join(profile.interests)}
Working languages: {", ".join(profile.languages.ok)} (fluent).{_weak_line(profile.languages)}

WHAT COUNTS AS A MATCH
""" + "\n".join(bullets) + "\n"


PREFILTER_SYSTEM = """You are screening job postings for a specific {role_label}.
Be PERMISSIVE: this is a first pass and a stronger reviewer will look at everything you keep.
Reject only when you are confident the posting is not viable (clearly the wrong seniority, clearly
requires a natural language the candidate can't work in, clearly on-site outside the target
regions, or not a software/IT job at all). When uncertain, keep it with a middling score and say
why in concerns. Score 0-100 = probability this is worth the candidate's time to read.

Don't deliberate over rejects: as soon as a posting fails one of those minimum requirements, mark it
relevant=false with score 0 immediately and move on — nobody reads the difference between a 15 and a
42. Spend your judgement only on postings that pass the minimum requirements. A programming language
mismatch is never a minimum-requirement failure (see below).

"""

RANK_SYSTEM = """You are a career advisor ranking job postings for a specific {role_label}.
Judge each posting on fit and on realistic chance of getting an interview. Be concrete: name the
skills that match, the gaps, the language/location/seniority risks, and how the candidate should
angle the application (which projects or background to emphasise). Score 0-100 where 80+ means
"apply this week", 50-79 "apply if time allows", below 50 "probably skip".

"""


#: Appended to the ranking system prompt for the refine stage, which sees the whole shortlist at
#: once instead of one posting at a time. The ranker scores a job inside a 15-job chunk, so its
#: score carries chunk noise; this pass removes it for the part of the list that matters.
REFINE_SYSTEM = """
REFINEMENT PASS
All the shortlisted postings follow, each introduced by a line "### job_id: <id>". You have
already judged them one at a time; now judge them against each other and put them in order.
Return every job_id exactly once, with a position (1 = best) and a score meaning "probability in
percent that the candidate should spend an evening applying to this one". Differences under
5 points are ties — use a gap only where you mean it. The summary is one sentence saying why the
posting sits where it does relative to the others, not a description of the job.
"""


#: What each stage is told to do with a posting that hits a deal-breaker. The screen rejects
#: outright (nothing ambiguous is thrown away, it is passed on with the suspicion noted); the
#: ranker still scores a borderline case so a slip-through is visible instead of silently gone.
_DEAL_BREAKER_INSTRUCTIONS = {
    "prefilter": (
        "If the posting clearly matches one of these, set relevant=false and score 0 and name the "
        "deal-breaker in concerns. If it only might match (ambiguous wording), keep it and name "
        "the suspicion in concerns so the ranker checks it."
    ),
    "rank": (
        "Treat a clear match as score 0-10 regardless of other fit; for a borderline match, cap "
        "the score at 40 and explain what would need to be true for it to be acceptable."
    ),
}


def _deal_breakers_block(profile: Profile, stage: str) -> str:
    """The deal-breaker section, or "" when the profile lists none.

    Empty is the default, and it has to stay byte-for-byte identical to the pre-feature prompt:
    any change here invalidates every cached verdict.
    """
    items = [item.strip() for item in profile.deal_breakers if item.strip()]
    if not items:
        return ""
    bullets = "\n".join(f"- {item}" for item in items)
    return f"\nDEAL-BREAKERS (automatic reject)\n{bullets}\n{_DEAL_BREAKER_INSTRUCTIONS[stage]}\n"


def system_prompt(stage: str, profile: Profile) -> str:
    if stage == "refine":
        # The refine pass judges the same jobs by the same rules; only the shape of the answer
        # differs, so it reuses the ranker's prompt verbatim and adds its own instructions.
        return system_prompt("rank", profile) + REFINE_SYSTEM
    stage = "prefilter" if stage == "prefilter" else "rank"
    template = PREFILTER_SYSTEM if stage == "prefilter" else RANK_SYSTEM
    head = template.replace("{role_label}", profile.prompt.role_label.strip())
    body = _policy_block(profile) + _deal_breakers_block(profile, stage)
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


#: Tail of the user prompt when part of the shortlist is already placed. The answer covers the
#: new postings only, but has to position them among the fixed ones, on the fixed ones' scale.
REFINE_ANCHORS_TAIL = """

Return exactly the {n} new {ids} listed above, each once, and nothing else — the placed postings
keep the score and position they already have. `position` is the slot in the combined ordering
with the placed postings included (1 = best of all), so a new posting that beats everything takes
position 1 and the ones it displaces move down. `score` is on the same scale as the placed scores
above: a new posting as good as the one placed at 80 scores about 80.
"""


def _anchor_line(job: Job, verdict: AIVerdict) -> str:
    """One already-placed posting as a single line.

    Enough to judge a new posting against — what the job is, and where the same reader put it —
    without resending the description it was judged on, which is the whole point of anchoring.
    """
    where = job.location_raw or job.country or "unknown"
    position = verdict.position if verdict.position is not None else "?"
    return (f"{job.id} · {job.title} · {job.company or 'unknown'} · {where} · "
            f"refine score {verdict.score} · position {position} · {verdict.summary}")


def refine_user_prompt(jobs: list[Job], filters: dict[str, FilterResult], max_chars: int,
                       anchors: list[tuple[Job, AIVerdict]] | None = None) -> str:
    """The shortlist as one message: every posting's own ``job_prompt``, keyed by job id.

    The id line is what the answer refers back to, so it has to survive verbatim; the rest is
    byte-for-byte what the rank stage sent for that job on its own.

    ``anchors`` are the shortlist members that already carry a refine verdict. Given them, only
    ``jobs`` (the new ones) get a full block, the anchors are listed as one fixed line each, and
    the answer is asked for in the combined ordering — so a second round pays for the new
    postings only. Without anchors the prompt is unchanged, byte for byte.
    """
    blocks = [f"### job_id: {job.id}\n{job_prompt(job, filters.get(job.id), max_chars)}" for job in jobs]
    if not anchors:
        return f"SHORTLIST ({len(jobs)} postings)\n\n" + "\n\n".join(blocks)
    placed = "\n".join(_anchor_line(job, verdict) for job, verdict in anchors)
    plural = "posting" if len(jobs) == 1 else "postings"
    return (f"SHORTLIST ({len(anchors) + len(jobs)} postings: {len(anchors)} already placed, "
            f"{len(jobs)} new)\n\n"
            "ALREADY PLACED (fixed — do not re-score, do not return these)\n"
            f"{placed}\n\n"
            f"NEW — score these relative to the placed ones ({len(jobs)} {plural})\n\n"
            + "\n\n".join(blocks)
            + REFINE_ANCHORS_TAIL.format(n=len(jobs), ids="job_id" if len(jobs) == 1 else "job_ids"))
