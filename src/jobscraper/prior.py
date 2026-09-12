"""A free lexical prior for the order the rank stage reads its queue in.

The rank stage can afford some 90 Opus calls per round against roughly 1 600 screen survivors,
so the *order* of the queue decides what the owner ever sees. Until now that order was the
Sonnet screen score, and the screen score does not sort: measured over the stored rank verdicts
on 2026-09-11 its Spearman against the Opus score is -0.06 on the default profile and +0.23 on
the second one. Reading the queue top-down was therefore close to reading it at random.

The same measurement showed that a signal costing nothing — how many of the profile's own skill
terms the posting actually uses — correlates better than the paid one (+0.19 / +0.29). So the
screen keeps its job as a *gate* (relevant, score >= ``ai.prefilter_min_score``) and this module
supplies the *ordering*. A free prior is tried before anything that spends tokens: a comparative
pre-pass over 1 600 postings would cost more than the ranking it is meant to prioritise, and it
could not be re-run for nothing every time the profile changes.

Everything here is deterministic and dependency-free (stdlib ``math`` only), so the same
database and profile always produce the same queue, and ``jobscraper audit-prior`` can replay
the comparison against the stored rank scores at any time. Whether this ordering is actually
better is a measured question, not a settled one, and ``ai.rank_order = "screen"`` puts the old
ordering back on every path at once.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping

from jobscraper.config import Profile
from jobscraper.models import Job

#: Tech names that a naive split would destroy, rewritten *before* splitting. "C++" would become
#: a bare "c" (dropped as one character) and ".NET" a bare "net"; both then match nothing a
#: profile can ask for. Applied in this order, on the lower-cased text, to query and documents
#: alike — the two only ever agree because they go through the same function.
SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("c++", "cplusplus"),
    ("c#", "csharp"),
    ("f#", "fsharp"),
    ("node.js", "nodejs"),
    ("ci/cd", "cicd"),
    (".net", "dotnet"),
)

#: Runs of non-alphanumerics separate tokens. Unicode-aware, so "Käyttöliittymä" stays one word.
_SPLIT = re.compile(r"[\W_]+", re.UNICODE)

#: One character is never a useful term ("a", "3", the "c" left over from a missed substitution).
MIN_TOKEN_LEN = 2

#: Where a query term can come from and what it is worth. A curated skill is a deliberate claim
#: about the candidate; a word in the CV prose is a word in some sentence. Weighting them the
#: same would let the prose, which is far longer, decide the ordering on its own.
SOURCE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("skills", 3.0),
    ("interests", 2.0),
    ("summary", 1.0),
    ("cv_text", 1.0),
)


def tokens(text: str) -> list[str]:
    """Lower-case ``text``, protect the tech names in :data:`SUBSTITUTIONS`, split, drop singles."""
    if not text:
        return []
    lowered = text.lower()
    for source, target in SUBSTITUTIONS:
        lowered = lowered.replace(source, target)
    return [t for t in _SPLIT.split(lowered) if len(t) >= MIN_TOKEN_LEN]


def query_terms(profile: Profile) -> dict[str, float]:
    """The weighted bag of words the profile is asking for.

    A term's weight is the **maximum** over the sources it appears in — never a sum, and never
    multiplied by how often it occurs. A word repeated through a CV would otherwise outweigh
    every curated skill by sheer volume, which is the opposite of what the profile means.
    A multi-word entry ("full stack open") contributes each of its tokens separately.

    No stopword list: the IDF in :func:`bm25` already flattens anything most postings say.
    """
    terms: dict[str, float] = {}
    for field, weight in SOURCE_WEIGHTS:
        value = getattr(profile, field, None) or ""
        text = " ".join(value) if isinstance(value, list) else str(value)
        for term in tokens(text):
            if weight > terms.get(term, 0.0):
                terms[term] = weight
    return terms


def bm25(docs: Mapping[str, str], query: Mapping[str, float], *,
         k1: float = 1.2, b: float = 0.75) -> dict[str, float]:
    """Okapi BM25 of every document in ``docs`` against the weighted ``query``.

    The IDF corpus is exactly ``docs``: rarity is measured inside the pool being ordered, which
    is the pool the caller is about to sort. ``log(1 + (N - df + 0.5) / (df + 0.5))`` is the
    always-positive form, so a term shared by every posting is worth little but can never push a
    document's score *down*. ``k1`` saturates repetition (the tenth "python" adds almost
    nothing) and ``b`` normalizes against the mean length of the same set, so a long posting
    does not win by being long.

    Summation runs over the query terms in sorted order, so the floats are reproducible.
    """
    if not docs:
        return {}
    tokenized = {doc_id: tokens(text) for doc_id, text in docs.items()}
    if not query:
        return dict.fromkeys(tokenized, 0.0)
    lengths = {doc_id: len(tk) for doc_id, tk in tokenized.items()}
    total = sum(lengths.values())
    if not total:  # every document is empty: nothing to measure against
        return dict.fromkeys(tokenized, 0.0)
    avgdl = total / len(tokenized)
    counts = {doc_id: Counter(tk) for doc_id, tk in tokenized.items()}

    n = len(tokenized)
    idf: dict[str, float] = {}
    for term in query:
        df = sum(1 for c in counts.values() if term in c)
        if df:
            idf[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))

    scores: dict[str, float] = {}
    for doc_id, count in counts.items():
        norm = k1 * (1 - b + b * lengths[doc_id] / avgdl)
        score = 0.0
        for term in sorted(idf):
            freq = count.get(term, 0)
            if freq:
                score += query[term] * idf[term] * freq * (k1 + 1) / (freq + norm)
        scores[doc_id] = score
    return scores


def rank_prior(jobs: list[Job], profile: Profile) -> dict[str, float]:
    """``{job_id: score}`` for every job in ``jobs``, scored against ``profile``.

    The document is the title twice plus the description: the title is a few words long and
    carries most of what the posting is, so repeating it is the conventional cheap boost. The
    caller passes the same list it passes to :func:`jobscraper.report.rank_queue`, so the
    document frequencies come from the pool actually being ordered.
    """
    docs = {j.id: "\n".join([j.title, j.title, j.description or ""]) for j in jobs}
    return bm25(docs, query_terms(profile))


def queue_prior(jobs: list[Job], profile: Profile) -> dict[str, float] | None:
    """The prior the rank queue should be ordered by, or ``None`` for the old screen order.

    The single call site for the CLI and the subagent scripts, so ``ai.rank_order`` flips every
    path at once.
    """
    if profile.ai.rank_order == "screen":
        return None
    return rank_prior(jobs, profile)
