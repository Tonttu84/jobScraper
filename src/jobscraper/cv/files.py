"""Where a tailored CV is saved: ``data/cv/tailored/CV - <Company>.pdf`` and its ``.md`` hand-off.

The owner attaches the PDF to an application and, before an interview, looks up which projects
went out, so the files are named by company. A second job of a company that already owns
``CV - <Company>`` gets ``CV - <Company> - <Title>``; a job without a company uses its title.
``index.json`` next to the files maps each stem to its job id: it decides those collisions and
lets a re-tailoring of the same job reuse its name.

A file that is replaced moves to ``older/CV - <...> (<YYYY-MM-DD HHMM>)`` first (its mtime), so a
CV that was already sent is never overwritten — unless the new hand-off says the same thing
(``make_room``'s ``same``), so a cached re-run does not pile up copies, or the old files are a
draft: written less than :data:`DRAFT_SECONDS` ago, as when a subagent re-imports while it fits
the page. Nobody sends a CV within minutes of its first version and then re-tailors it.

``adopt_legacy`` renames the earlier ``<date>-<job id>.pdf/.md`` files into this scheme once.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jobscraper.models import Job
from jobscraper.store import Store

INDEX = "index.json"
OLDER = "older"
PREFIX = "CV - "
#: The longest stem; a long title is cut to fit, never the prefix or the company.
MAX_STEM = 120
#: Files younger than this are a draft and are overwritten instead of moved to older/.
DRAFT_SECONDS = 30 * 60
#: The files of one CV that move and get renamed together (a PNG comes from ``cv-render --png``).
SUFFIXES = (".pdf", ".md", ".png")

_DASHED = re.compile(r"[/\\|]")
_SPACED = re.compile(r'[<>:"?*\x00-\x1f\x7f]')
_LEGACY = re.compile(r"^(\d{4}-\d{2}-\d{2})-([0-9a-f]{16})(\.pdf|\.md|\.png)$")


def safe_name(text: str) -> str:
    """``text`` as part of a Windows file name: path separators become dashes, other refused
    characters spaces; whitespace collapsed, no trailing dots or spaces."""
    text = _SPACED.sub(" ", _DASHED.sub("-", text))
    return " ".join(text.split()).rstrip(". ")


def _cut(text: str, room: int) -> str:
    return text[:max(room, 0)].rstrip(" .-")


def candidate_stems(job: Job) -> list[str]:
    """The names ``job``'s CV may take, in order of preference: the company, then the company
    and the title, then that with the short job id (which no other job can own)."""
    company, title = safe_name(job.company or ""), safe_name(job.title) or job.id
    if not company:
        first = PREFIX + _cut(title, MAX_STEM - len(PREFIX))
        return [first, f"{first} ({job.id[:8]})"]
    first = PREFIX + company
    rest = _cut(title, MAX_STEM - len(first) - 3)
    second = f"{first} - {rest}" if rest else first
    return [first, second, f"{second} ({job.id[:8]})"]


def pdf_title(job: Job) -> str:
    """The printed PDF's title: says which job the file belongs to, whatever it is renamed to."""
    name = " ".join((job.company or "").split()) or " ".join(job.title.split())
    return f"{PREFIX}{name} ({job.id})"


# ------------------------------------------------------------------------- index


def load_index(directory: Path) -> dict[str, str]:
    """Stem → job id; empty when there is no index yet."""
    path = Path(directory) / INDEX
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_index(directory: Path, index: dict[str, str]) -> None:
    """Written to a temporary file and moved over the old one, so it is never half-written."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{INDEX}.tmp"
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, directory / INDEX)


def _stem_of(index: dict[str, str], job_id: str) -> str | None:
    return next((stem for stem, owner in index.items() if owner == job_id), None)


def claim_stem(directory: Path, job: Job) -> str:
    """The stem ``job``'s files go under: the one it already owns, else the first free
    candidate (not in the index, ignoring case as Windows does, and no such file on disk)."""
    directory = Path(directory)
    index = load_index(directory)
    owned = _stem_of(index, job.id)
    if owned is not None:
        return owned
    taken = {stem.casefold() for stem in index}
    candidates = candidate_stems(job)
    stem = next((c for c in candidates if c.casefold() not in taken
                 and not any((directory / f"{c}{s}").exists() for s in SUFFIXES)), candidates[-1])
    index[stem] = job.id
    save_index(directory, index)
    return stem


def owned_pdf(directory: Path, job_id: str) -> Path | None:
    """The PDF of ``job_id``'s CV: through the index, else a legacy ``<date>-<id>.pdf``."""
    directory = Path(directory)
    stem = _stem_of(load_index(directory), job_id)
    if stem is not None and (directory / f"{stem}.pdf").is_file():
        return directory / f"{stem}.pdf"
    return max(directory.glob(f"*-{job_id}.pdf"), default=None)


# ----------------------------------------------------------------------- archive


def _archive(directory: Path, stem: str, paths: list[Path]) -> list[Path]:
    """Move ``paths`` (one CV's files) to ``older/<stem> (<time>)<suffix>``; the time is the
    .md's mtime (else the first file's), shared by the pair. Never overwrites an older copy."""
    if not paths:
        return []
    clock = next((p for p in paths if p.suffix == ".md"), paths[0])
    when = f"{datetime.fromtimestamp(clock.stat().st_mtime):%Y-%m-%d %H%M}"
    older = Path(directory) / OLDER
    older.mkdir(parents=True, exist_ok=True)
    base, n = f"{stem} ({when})", 1
    while any((older / f"{base}{p.suffix}").exists() for p in paths):
        n += 1
        base = f"{stem} ({when}) {n}"
    moved = []
    for path in paths:
        target = older / f"{base}{path.suffix}"
        os.replace(path, target)
        moved.append(target)
    return moved


def make_room(directory: Path, stem: str, same: Callable[[str], bool], *,
              now: float | None = None) -> list[Path]:
    """Before ``<stem>.pdf/.md`` are written: move the existing files to older/, unless the
    existing .md passes ``same`` (the new CV says the same) or the files are a draft (see
    :data:`DRAFT_SECONDS`; ``now`` is for tests). The moved paths."""
    directory = Path(directory)
    md = directory / f"{stem}.md"
    if md.is_file() and same(md.read_text(encoding="utf-8")):
        return []
    paths = [p for s in SUFFIXES if (p := directory / f"{stem}{s}").is_file()]
    if paths:
        clock = md if md.is_file() else paths[0]
        if (time.time() if now is None else now) - clock.stat().st_mtime < DRAFT_SECONDS:
            return []
    return _archive(directory, stem, paths)


def _without_cached(markdown: str) -> str:
    return re.sub(r"^(- Library .*?) \(cached\)$", r"\1", markdown, count=1, flags=re.M)


def same_handoff(old: str, new: str) -> bool:
    """Two hand-offs say the same; only the "(cached)" note of a re-run may differ."""
    return _without_cached(old) == _without_cached(new)


# ------------------------------------------------------------------------ legacy


def adopt_legacy(directory: Path, store: Store) -> list[Path]:
    """Rename the ``<date>-<job id>.pdf/.md`` files of jobs in ``store`` into this scheme: the
    newest date of a job takes its stem (unless a file already has it), the older dates go to
    older/. Files of jobs not in the store are left alone. The new paths."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    for path in sorted(directory.iterdir()):
        match = _LEGACY.match(path.name)
        if match and path.is_file():
            found[match[2]][match[1]].append(path)
    if not found:
        return []
    jobs = {job.id: job for job in store.jobs(ids=list(found))}
    renamed: list[Path] = []
    # The job tailored first keeps the plain company name, as it would have had it then.
    for job_id in sorted((i for i in found if i in jobs), key=lambda i: (min(found[i]), i)):
        stem = claim_stem(directory, jobs[job_id])
        dates = sorted(found[job_id], reverse=True)
        if not any((directory / f"{stem}{s}").exists() for s in SUFFIXES):
            for path in found[job_id][dates.pop(0)]:
                target = directory / f"{stem}{path.suffix}"
                os.replace(path, target)
                renamed.append(target)
        for date in dates:
            renamed += _archive(directory, stem, found[job_id][date])
    return renamed
