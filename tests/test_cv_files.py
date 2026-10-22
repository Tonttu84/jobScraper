"""Tailored CV file names (``jobscraper.cv.files``): named by company, owned through index.json,
an earlier copy moved to older/ before it is replaced, and the dated legacy files adopted."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from jobscraper.cv import files
from jobscraper.models import Job
from jobscraper.store import Store


def _job(source_id: str = "1", title: str = "C++ Developer", company: str | None = "Acme") -> Job:
    return Job(source="test", source_id=source_id, url=f"https://example.test/{source_id}",
               title=title, company=company)


def _touch(path: Path, text: str = "x", when: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if when is not None:
        os.utime(path, (when.timestamp(), when.timestamp()))
    return path


# ------------------------------------------------------------------------- names


def test_safe_name_replaces_what_windows_refuses():
    assert files.safe_name('A<b>c:d"e/f\\g|h?i*j') == "A b c d e-f-g-h i j"
    assert files.safe_name("Tab\there\nnewline\x01ctl") == "Tab here newline ctl"
    assert files.safe_name("  lots   of   space  ") == "lots of space"
    assert files.safe_name("Trailing dots... ") == "Trailing dots"


def test_stems_company_first_then_company_and_title():
    job = _job(title="Data/ML Engineer", company="Acme: Labs")
    assert files.candidate_stems(job) == ["CV - Acme Labs", "CV - Acme Labs - Data-ML Engineer",
                                          f"CV - Acme Labs - Data-ML Engineer ({job.id[:8]})"]


def test_stems_without_a_company_use_the_title():
    job = _job(title="Go Developer", company=None)
    assert files.candidate_stems(job)[0] == "CV - Go Developer"
    assert files.candidate_stems(_job(company="  "))[0] == "CV - C++ Developer"


def test_long_stems_cut_the_title_not_the_prefix_or_company():
    job = _job(title="Senior " + "Very Long Title " * 20, company="Acme")
    for stem in files.candidate_stems(job)[:2]:
        assert len(stem) <= files.MAX_STEM
        assert stem.startswith("CV - Acme")
        assert not stem.endswith((" ", ".", "-"))
    no_company = files.candidate_stems(_job(title="Word " * 60, company=None))[0]
    assert no_company.startswith("CV - Word") and len(no_company) <= files.MAX_STEM
    # A company that alone fills the cap is kept whole; the title has no room left.
    huge = "B" * 130
    stems = files.candidate_stems(_job(company=huge))
    assert stems[0] == f"CV - {huge}" and stems[1] == stems[0]


def test_pdf_title_carries_the_job_id():
    job = _job()
    assert files.pdf_title(job) == f"CV - Acme ({job.id})"
    assert files.pdf_title(_job(company=None)).startswith("CV - C++ Developer (")


# ------------------------------------------------------------------------- index


def test_index_round_trip_is_atomic(tmp_path):
    assert files.load_index(tmp_path) == {}
    files.save_index(tmp_path, {"CV - Acme": "abc"})
    assert json.loads((tmp_path / files.INDEX).read_text(encoding="utf-8")) == {"CV - Acme": "abc"}
    assert files.load_index(tmp_path) == {"CV - Acme": "abc"}
    assert [p.name for p in tmp_path.iterdir()] == [files.INDEX]  # no temp file left behind


def test_claim_takes_the_company_name_and_reuses_it(tmp_path):
    job = _job()
    assert files.claim_stem(tmp_path, job) == "CV - Acme"
    assert files.load_index(tmp_path) == {"CV - Acme": job.id}
    assert files.claim_stem(tmp_path, _job(title="Renamed")) == "CV - Acme"  # same id: same name


def test_another_job_of_the_company_gets_the_title(tmp_path):
    first, second = _job(), _job(source_id="2", title="Go Developer")
    files.claim_stem(tmp_path, first)
    assert files.claim_stem(tmp_path, second) == "CV - Acme - Go Developer"
    third = _job(source_id="3", title="Go Developer")
    assert files.claim_stem(tmp_path, third) == f"CV - Acme - Go Developer ({third.id[:8]})"
    # Windows names ignore case: ACME is the same file as Acme.
    fourth = _job(source_id="4", title="Rust Developer", company="ACME")
    assert files.claim_stem(tmp_path, fourth) == "CV - ACME - Rust Developer"


def test_a_file_nobody_owns_is_not_taken(tmp_path):
    _touch(tmp_path / "CV - Acme.pdf")  # made by hand, not in the index
    assert files.claim_stem(tmp_path, _job()) == "CV - Acme - C++ Developer"


def test_owned_pdf_through_the_index_then_the_legacy_glob(tmp_path):
    job = _job()
    assert files.owned_pdf(tmp_path, job.id) is None
    legacy = _touch(tmp_path / f"2026-10-16-{job.id}.pdf")
    assert files.owned_pdf(tmp_path, job.id) == legacy
    stem = files.claim_stem(tmp_path, job)
    assert files.owned_pdf(tmp_path, job.id) == legacy  # claimed, but nothing printed yet
    pdf = _touch(tmp_path / f"{stem}.pdf")
    assert files.owned_pdf(tmp_path, job.id) == pdf


# ----------------------------------------------------------------------- archive


def test_make_room_moves_the_old_pair_with_its_time(tmp_path):
    when = datetime(2026, 10, 16, 9, 5)
    _touch(tmp_path / "CV - Acme.pdf", "%PDF", when)
    _touch(tmp_path / "CV - Acme.md", "old", when)
    moved = files.make_room(tmp_path, "CV - Acme", lambda old: False)
    older = tmp_path / files.OLDER
    assert sorted(p.name for p in moved) == ["CV - Acme (2026-10-16 0905).md",
                                             "CV - Acme (2026-10-16 0905).pdf"]
    assert (older / "CV - Acme (2026-10-16 0905).md").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "CV - Acme.pdf").exists() and not (tmp_path / "CV - Acme.md").exists()


def test_make_room_keeps_an_unchanged_pair(tmp_path):
    _touch(tmp_path / "CV - Acme.pdf", "%PDF")
    _touch(tmp_path / "CV - Acme.md", "same")
    assert files.make_room(tmp_path, "CV - Acme", lambda old: old == "same") == []
    assert (tmp_path / "CV - Acme.pdf").is_file() and not (tmp_path / files.OLDER).exists()


def test_make_room_with_nothing_there_or_only_a_pdf(tmp_path):
    assert files.make_room(tmp_path, "CV - Acme", lambda old: True) == []
    _touch(tmp_path / "CV - Acme.pdf", "%PDF", datetime(2026, 10, 1, 12, 0))
    moved = files.make_room(tmp_path, "CV - Acme", lambda old: True)  # no .md to compare
    assert [p.name for p in moved] == ["CV - Acme (2026-10-01 1200).pdf"]


def test_make_room_never_overwrites_an_older_copy(tmp_path):
    when = datetime(2026, 10, 16, 9, 5)
    for _ in range(2):
        _touch(tmp_path / "CV - Acme.md", "old", when)
        files.make_room(tmp_path, "CV - Acme", lambda old: False)
    assert sorted(p.name for p in (tmp_path / files.OLDER).iterdir()) == [
        "CV - Acme (2026-10-16 0905) 2.md", "CV - Acme (2026-10-16 0905).md"]


def test_same_handoff_ignores_only_the_cached_marker():
    fresh = "# T\n\n- Library v1, opus\n- PDF: x\n"
    cached = "# T\n\n- Library v1, opus (cached)\n- PDF: x\n"
    assert files.same_handoff(fresh, cached) and files.same_handoff(cached, fresh)
    assert not files.same_handoff(fresh, fresh.replace("v1", "v2"))


# ------------------------------------------------------------------------ legacy


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def test_adopt_legacy_renames_the_dated_files(tmp_path, store):
    first, second = _job(), _job(source_id="2", title="Go Developer")
    store.upsert_jobs([first, second])
    out = tmp_path / "tailored"
    for name, when in ((f"2026-10-15-{first.id}", datetime(2026, 10, 15, 8, 30)),
                       (f"2026-10-17-{first.id}", datetime(2026, 10, 17, 10, 0)),
                       (f"2026-10-16-{second.id}", datetime(2026, 10, 16, 14, 45))):
        _touch(out / f"{name}.pdf", "%PDF", when)
        _touch(out / f"{name}.md", name, when)
    unknown = _touch(out / "2026-10-16-0123456789abcdef.pdf")
    other = _touch(out / "notes.md")

    renamed = files.adopt_legacy(out, store)

    assert len(renamed) == 6
    assert (out / "CV - Acme.md").read_text(encoding="utf-8") == f"2026-10-17-{first.id}"
    assert (out / "CV - Acme.pdf").is_file()
    older = out / files.OLDER
    assert (older / "CV - Acme (2026-10-15 0830).md").read_text(encoding="utf-8") == \
        f"2026-10-15-{first.id}"
    assert (older / "CV - Acme (2026-10-15 0830).pdf").is_file()
    # The second Acme job came later (by its first date) and gets its title in the name.
    assert (out / "CV - Acme - Go Developer.md").read_text(encoding="utf-8") == \
        f"2026-10-16-{second.id}"
    assert files.load_index(out) == {"CV - Acme": first.id, "CV - Acme - Go Developer": second.id}
    assert unknown.is_file() and other.is_file()  # not a known job / not a legacy name
    assert files.adopt_legacy(out, store) == []  # idempotent


def test_adopt_legacy_when_the_new_name_already_exists(tmp_path, store):
    job = _job()
    store.upsert_jobs([job])
    out = tmp_path / "tailored"
    stem = files.claim_stem(out, job)
    _touch(out / f"{stem}.md", "new")
    _touch(out / f"2026-10-16-{job.id}.md", "legacy", datetime(2026, 10, 16, 7, 0))
    files.adopt_legacy(out, store)
    assert (out / f"{stem}.md").read_text(encoding="utf-8") == "new"
    assert (out / files.OLDER / "CV - Acme (2026-10-16 0700).md").read_text(encoding="utf-8") == "legacy"


def test_adopt_legacy_without_a_folder(tmp_path, store):
    assert files.adopt_legacy(tmp_path / "missing", store) == []


def test_a_cv_replaced_soon_after_it_was_written_is_a_draft(tmp_path):
    """A subagent re-importing while it fits the page must not file every attempt in older/."""
    written = datetime(2026, 10, 17, 19, 16)
    _touch(tmp_path / "CV - Acme.pdf", "%PDF", written)
    _touch(tmp_path / "CV - Acme.md", "attempt 1", written)
    soon = written.timestamp() + files.DRAFT_SECONDS - 1
    assert files.make_room(tmp_path, "CV - Acme", lambda old: False, now=soon) == []
    assert not (tmp_path / files.OLDER).exists()
    later = written.timestamp() + files.DRAFT_SECONDS + 1
    moved = files.make_room(tmp_path, "CV - Acme", lambda old: False, now=later)
    assert sorted(p.name for p in moved) == ["CV - Acme (2026-10-17 1916).md",
                                             "CV - Acme (2026-10-17 1916).pdf"]


def test_a_lone_pdf_is_a_draft_by_its_own_time(tmp_path):
    written = datetime(2026, 10, 17, 19, 16)
    _touch(tmp_path / "CV - Acme.pdf", "%PDF", written)
    assert files.make_room(tmp_path, "CV - Acme", lambda old: True, now=written.timestamp() + 60) == []


@pytest.mark.parametrize("company, expected", [
    ("CLOUDFIDE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", "CV - Cloudfide"),
    ("Cloudfide Sp. z o.o.", "CV - Cloudfide"),
    ("RUBINLAKE GmbH", "CV - RUBINLAKE"),
    ("Advaun Oy", "CV - Advaun"),
    ("Volvo AB", "CV - Volvo"),
    ("Acme Ltd.", "CV - Acme"),
    ("Acme, Inc.", "CV - Acme"),
    ("Noesis Portugal S.A.", "CV - Noesis Portugal"),
    ("NANCE", "CV - NANCE"),            # a short all-caps brand stays as it is
    ("King", "CV - King"),
    ("GmbH", "CV - GmbH"),              # nothing left after stripping: keep the original
])
def test_company_names_lose_their_legal_form(company, expected):
    assert files.candidate_stems(_job(company=company))[0] == expected
