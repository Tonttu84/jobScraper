"""Evergreen adverts: postings that are advertising rather than a vacancy.

Some boards keep a page up whether or not anyone is hiring behind it. Cisco opens such a posting
with "This posting is to advertise potential job opportunities … this exact role may not be open
today"; Vodafone runs "Register Your Interest" forms; plenty of others say talent pool, talent
community, future openings, or simply invite an unsolicited CV (*Initiativbewerbung*, *avoin
hakemus*, *candidatura espontânea*, *spontanansökan*). Applying to one of these is not applying
for a job, and the AI stages were noticing it inconsistently — the same Cisco graduate advert
scored 78 in the ranking pass and 38 in the refine pass on exactly this objection.

So the phrase is read once, deterministically, and carried as a rule signal that the ranking
prompt, the report and the web UI all say the same thing about. It is never a drop: the wording
also turns up on pages that do list a real opening, and the AI reads a posting better than a
phrase list can. It is a concern, not a verdict.
"""

from __future__ import annotations

import re

#: The phrases that give a pipeline advert away. Whole phrases only — "talent" and
#: "opportunities" on their own are ordinary recruiting words and match nothing here.
CUES: tuple[str, ...] = (
    # The disclaimer an enterprise board opens a pipeline advert with
    "advertise potential job opportunities", "this exact role may not be open",
    "may not be an open position", "not a specific open position",
    # Register-your-interest forms, talent pools and open applications
    "register your interest", "expression of interest", "talent pool", "talent community",
    "future opportunities", "future openings", "always looking for", "keep your CV on file",
    "speculative application",
    # The same invitation in the other languages the European boards post in
    "Initiativbewerbung", "avoin hakemus", "candidatura espontânea", "spontanansökan",
)

#: Normalized form (lower case, single spaces) → the spelling to report back as evidence, so the
#: signal stays a small stable vocabulary however loudly the board wrote it.
_BY_NORM: dict[str, str] = {" ".join(cue.split()).lower(): cue for cue in CUES}

# Real postings run line breaks and double spaces through these phrases, so any run of
# whitespace stands for the single space in the cue.
_CUE_RE = re.compile(
    r"\b(?:" + "|".join(r"\s+".join(re.escape(word) for word in cue.split()) for cue in CUES)
    + r")\b",
    re.I,
)


def _match(text: str | None) -> str | None:
    """The first cue in ``text``, in its canonical spelling, or ``None``."""
    found = _CUE_RE.search(text or "")
    return _BY_NORM[" ".join(found.group(0).split()).lower()] if found else None


def is_evergreen(title: str, text: str) -> str | None:
    """The cue that gives ``title``/``text`` away as an evergreen advert, or ``None``.

    The title is searched first: "Register Your Interest – Graduates" is the whole story, and a
    long description would otherwise decide which cue gets reported as the evidence.
    """
    return _match(title) or _match(text)
