"""The FastAPI application: a JSON API over one report plus the single-page UI.

FastAPI runs sync endpoints in a threadpool, so every request gets its own
:class:`~jobscraper.store.Store` through the ``get_store`` dependency instead of sharing one
connection. The dependency is entered in one worker and the endpoint may run in another, so the
Store opens its connection with ``check_same_thread=False``; each one still serves a single
request at a time.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, get_args

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from jobscraper.ai.prompts import PROMPT_VERSION
from jobscraper.config import WebPreset
from jobscraper.facets import FACETS_VERSION, compute_facets
from jobscraper.models import (
    AIVerdict,
    Decision,
    DecisionStatus,
    FilterResult,
    Job,
    JobFacets,
    RemoteKind,
    ReportSection,
    ReportSnapshot,
)
from jobscraper.report import build_snapshot
from jobscraper.store import Store

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

#: How far back the on-the-fly view looks when the DB has no report row yet.
DEFAULT_DAYS = 30
#: Tie-break order of the report sections, mirroring the markdown report.
SECTION_ORDER: dict[str, int] = {"ranked": 0, "prefilter": 1, "review": 2}
DECISION_STATUSES: list[str] = list(get_args(DecisionStatus))

Handle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


#: Reserved file name for the writable decisions sidecar inside a serve directory.
DECISIONS_DB = "decisions.db"


def current_db(serve_dir: Path) -> Path | None:
    """The database to serve: the most recently modified ``*.db`` copy in the directory."""
    candidates = [p for p in Path(serve_dir).glob("*.db") if p.name != DECISIONS_DB and p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.stat().st_mtime, p.name))


def get_store(request: Request) -> Iterator[Store]:
    """One SQLite connection per request — they are bound to the thread that opened them.

    In serve-directory mode the job database is opened read-only (it is a copy the owner
    dropped in) and decisions go to the ``decisions.db`` sidecar next to it, so they survive
    replacing the copy. In single-database mode everything lives in that one file.
    """
    serve_dir: Path | None = request.app.state.serve_dir
    if serve_dir is not None:
        db = current_db(serve_dir)
        if db is None:
            raise HTTPException(status_code=503, detail=f"no database in {serve_dir}; run `jobscraper publish`")
        store = Store(db, readonly=True, decisions_path=serve_dir / DECISIONS_DB)
    else:
        store = Store(request.app.state.db_path)
    try:
        yield store
    finally:
        store.close()


StoreDep = Annotated[Store, Depends(get_store)]


# --------------------------------------------------------------------- response models


class FilterView(BaseModel):
    """The rule filter's outcome as the UI needs it."""

    status: str
    location_tier: int | None = None
    reasons: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)


class VerdictView(BaseModel):
    """One AI stage's verdict; ``why_apply`` is only filled by the rank stage."""

    score: int
    relevant: bool
    summary: str
    concerns: list[str] = Field(default_factory=list)
    why_apply: list[str] = Field(default_factory=list)


class FacetsView(BaseModel):
    posting_language: str | None = None
    languages_required: list[str] = Field(default_factory=list)
    languages_optional: list[str] = Field(default_factory=list)
    stacks: list[str] = Field(default_factory=list)
    web_dev: bool = False


class DecisionView(BaseModel):
    status: DecisionStatus
    note: str | None = None
    updated_at: datetime


class JobView(BaseModel):
    """One posting as the list shows it (no description — see :class:`JobDetail`)."""

    id: str
    title: str
    company: str | None = None
    url: str
    source: str
    country: str | None = None
    city: str | None = None
    location_raw: str | None = None
    remote: RemoteKind = "unknown"
    remote_region: str | None = None
    posted_at: datetime | None = None
    employment_type: str | None = None
    salary_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    score: int | None = None
    section: ReportSection | None = None
    position: int | None = None
    filter: FilterView | None = None
    prefilter: VerdictView | None = None
    rank: VerdictView | None = None
    facets: FacetsView
    decision: DecisionView | None = None


class JobDetail(JobView):
    description: str | None = None


class JobList(BaseModel):
    total: int
    items: list[JobView]


class ReportMeta(BaseModel):
    id: int | None = None
    created_at: datetime
    days: int
    prompt_version: str
    counts: dict[str, int] = Field(default_factory=dict)
    cost: dict[str, float] = Field(default_factory=dict)
    path: str | None = None


class UIConfig(BaseModel):
    """How the page presents its profile-fact controls, from the profile's ``web.preset``."""

    preset: WebPreset = "student"
    default_languages: list[str] = Field(
        default_factory=lambda: ["en"],
        description="Codes ticked in 'languages you speak' for a viewer with no stored choice",
    )
    web_dev_toggle: bool = Field(
        True, description="Whether to show the 'I have done Full Stack Open' box at all"
    )


class Meta(BaseModel):
    """Everything the filter panel needs in one request."""

    report: ReportMeta | None = None
    sections: dict[str, int] = Field(default_factory=dict)
    languages: dict[str, int] = Field(default_factory=dict)
    languages_required: dict[str, int] = Field(default_factory=dict)
    languages_offered: list[str] = Field(
        default_factory=list,
        description="Codes to offer as 'languages you speak': observed in the data, plus English, "
                    "plus whatever the served profile speaks",
    )
    stacks: dict[str, int] = Field(default_factory=dict)
    countries: dict[str, int] = Field(default_factory=dict)
    remote: dict[str, int] = Field(default_factory=dict)
    sources: dict[str, int] = Field(default_factory=dict)
    users: list[str] = Field(default_factory=list)
    decision_statuses: list[str] = Field(default_factory=list)
    ui: UIConfig = Field(default_factory=UIConfig)
    facets_version: str = FACETS_VERSION
    database: str | None = Field(None, description="File name of the database being served")


class DecisionIn(BaseModel):
    user: Handle
    status: DecisionStatus
    note: str | None = None


# ------------------------------------------------------------------------------ view


@dataclass
class View:
    """One report resolved into everything the endpoints read, loaded once per request."""

    snapshot: ReportSnapshot
    stored: bool
    jobs: dict[str, Job]
    filters: dict[str, FilterResult]
    prefilter: dict[str, AIVerdict]
    rank: dict[str, AIVerdict]
    facets: dict[str, JobFacets]
    decisions: dict[str, Decision]


def _stage_verdicts(store: Store, stage: str, ids: list[str] | None = None) -> dict[str, AIVerdict]:
    """Verdicts for ``stage`` under the current prompt version, falling back to older ones.

    A report can reference jobs that were scored before the prompt text changed; those would
    otherwise show up without any AI commentary at all.
    """
    current = store.verdicts(stage, PROMPT_VERSION)
    if ids is not None and all(job_id in current for job_id in ids):
        return current
    merged = store.verdicts(stage)
    merged.update(current)
    return merged


def load_view(store: Store, report_id: int | None = None, user: str | None = None) -> View:
    """Resolve one report (the latest by default) into jobs, filters, verdicts and facets.

    When the DB holds no report at all, a snapshot is built on the fly from the current state
    over the last :data:`DEFAULT_DAYS` days; that snapshot has ``id=None`` and is not stored.
    """
    snapshot = store.report(report_id)
    stored = snapshot is not None
    if snapshot is None:
        if report_id is not None:
            raise HTTPException(status_code=404, detail=f"no report with id {report_id}")
        jobs_list = store.jobs(seen_within_days=DEFAULT_DAYS)
        filters = store.filter_results()
        snapshot = build_snapshot(
            jobs_list,
            filters,
            _stage_verdicts(store, "prefilter"),
            _stage_verdicts(store, "rank"),
            days=DEFAULT_DAYS,
            prompt_version=PROMPT_VERSION,
        )
    else:
        filters = store.filter_results()

    ids = [item.job_id for item in snapshot.items]
    jobs = {job.id: job for job in store.jobs(ids=ids)}
    facets = store.facets(ids)
    for job_id, job in jobs.items():
        if job_id not in facets:  # never persisted from the web app
            facets[job_id] = compute_facets(job, filters.get(job_id))
    decisions = {d.job_id: d for d in store.decisions(user)} if user else {}
    return View(
        snapshot=snapshot,
        stored=stored,
        jobs=jobs,
        filters=filters,
        prefilter=_stage_verdicts(store, "prefilter", ids),
        rank=_stage_verdicts(store, "rank", ids),
        facets=facets,
        decisions=decisions,
    )


def _verdict_view(v: AIVerdict | None) -> VerdictView | None:
    if v is None:
        return None
    return VerdictView(score=v.score, relevant=v.relevant, summary=v.summary,
                       concerns=list(v.concerns), why_apply=list(v.why_apply))


def _facets_view(view: View, job: Job) -> FacetsView:
    f = view.facets.get(job.id) or compute_facets(job, view.filters.get(job.id))
    return FacetsView(posting_language=f.posting_language,
                      languages_required=list(f.languages_required),
                      languages_optional=list(f.languages_optional),
                      stacks=list(f.stacks), web_dev=f.web_dev)


def _job_view(view: View, job: Job, section: str | None, position: int | None,
              *, detail: bool = False) -> JobView:
    fr = view.filters.get(job.id)
    pre, rank = view.prefilter.get(job.id), view.rank.get(job.id)
    decision = view.decisions.get(job.id)
    fields: dict[str, Any] = {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "url": job.url,
        "source": job.source,
        "country": job.country,
        "city": job.city,
        "location_raw": job.location_raw,
        "remote": job.remote,
        "remote_region": job.remote_region,
        "posted_at": job.posted_at,
        "employment_type": job.employment_type,
        "salary_text": job.salary_text,
        "tags": list(job.tags),
        "score": rank.score if rank else (pre.score if pre else None),
        "section": section,
        "position": position,
        "filter": FilterView(status=fr.status, location_tier=fr.location_tier,
                             reasons=list(fr.reasons), signals=dict(fr.signals)) if fr else None,
        "prefilter": _verdict_view(pre),
        "rank": _verdict_view(rank),
        "facets": _facets_view(view, job),
        "decision": DecisionView(status=decision.status, note=decision.note,
                                 updated_at=decision.updated_at) if decision else None,
    }
    if detail:
        return JobDetail(description=job.description, **fields)
    return JobView(**fields)


def _sorted_views(view: View) -> list[JobView]:
    """Every job of the report, best score first, ``None`` scores last."""
    out = [
        _job_view(view, view.jobs[item.job_id], item.section, item.position)
        for item in view.snapshot.items
        if item.job_id in view.jobs
    ]
    out.sort(key=lambda j: (j.score is None, -(j.score or 0),
                            SECTION_ORDER.get(j.section or "", 9), j.position or 0))
    return out


# ----------------------------------------------------------------------- query params


def _codes(values: list[str] | None, *, upper: bool = False) -> list[str]:
    """Flatten repeated and comma-separated query values into normalized codes."""
    out: list[str] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                out.append(part.upper() if upper else part.lower())
    return out


def _matches(view: View, job: JobView, *, langs: list[str], stack: list[str], section: list[str],
             country: list[str], remote: list[str], source: list[str], web_dev: bool | None,
             min_score: int | None, q: str | None, decision: list[str]) -> bool:
    facets = job.facets
    if langs and not view.facets[job.id].language_ok(langs):
        return False
    if stack and not set(stack) & set(facets.stacks):
        return False
    if web_dev is not None and facets.web_dev is not web_dev:
        return False
    if section and (job.section or "") not in section:
        return False
    if country and (job.country or "").upper() not in country:
        return False
    if remote and job.remote not in remote:
        return False
    if source and job.source.lower() not in source:
        return False
    if min_score is not None and (job.score is None or job.score < min_score):
        return False
    if q:
        needle = q.lower()
        if needle not in job.title.lower() and needle not in (job.company or "").lower():
            return False
    # "none" stands for "no decision by this user", so every job has exactly one value here.
    return not decision or (job.decision.status if job.decision else "none") in decision


def _counter(values) -> dict[str, int]:
    return dict(Counter(v for v in values if v))


# ------------------------------------------------------------------------------- app


def create_app(db_path: Path | None = None, serve_dir: Path | None = None,
               languages: list[str] | None = None, preset: WebPreset = "student") -> FastAPI:
    """Build the app over one database (``db_path``) or over a directory of copies.

    With neither argument the app serves ``$JOBSCRAPER_SERVE_DIR`` or ``data/serve/``: whatever
    ``*.db`` copy was put there last, read-only, with decisions in a sidecar.

    ``languages`` are the codes the served candidate speaks (``profile.languages.ok``); they are
    offered as tick boxes even when no posting in the report happens to use them.

    ``preset`` is the profile's ``web.preset``. ``student`` is the page shared with fellow
    students: every observed language is on offer, only English starts ticked, and the "I have
    done Full Stack Open" box is there. ``tailored`` is a one-person search: only the profile's
    languages are offered, all of them ticked, and no Full Stack Open box.
    """
    if db_path is None and serve_dir is None:
        from jobscraper.config import paths

        serve_dir = Path(os.environ.get("JOBSCRAPER_SERVE_DIR") or paths().data / "serve")
    app = FastAPI(title="jobscraper", description="Ranked jobs with per-user decisions.")
    app.state.db_path = db_path
    app.state.serve_dir = Path(serve_dir) if serve_dir is not None else None
    app.state.languages = [code.lower() for code in (languages or [])]
    app.state.preset = preset

    def _ui(jobs: list[JobView]) -> tuple[list[str], UIConfig]:
        """The languages to offer as tick boxes and how the page should present itself."""
        spoken = sorted(set(app.state.languages))
        if app.state.preset == "tailored":
            # A page for one known person: their languages, all ticked, no profile questions.
            return spoken, UIConfig(preset="tailored", default_languages=spoken,
                                    web_dev_toggle=False)
        offered = sorted(
            {j.facets.posting_language for j in jobs if j.facets.posting_language}
            | {c for j in jobs for c in j.facets.languages_required}
            | {"en", *app.state.languages}
        )
        return offered, UIConfig(preset="student", default_languages=["en"], web_dev_toggle=True)

    @app.get("/api/meta", response_model=Meta)
    def meta(store: StoreDep, report_id: int | None = None) -> Meta:
        view = load_view(store, report_id)
        jobs = _sorted_views(view)
        offered, ui = _ui(jobs)
        return Meta(
            report=ReportMeta(**view.snapshot.model_dump(exclude={"items"})) if view.stored else None,
            sections=_counter(j.section for j in jobs),
            languages=_counter(j.facets.posting_language for j in jobs),
            languages_required=_counter(c for j in jobs for c in j.facets.languages_required),
            languages_offered=offered,
            stacks=_counter(s for j in jobs for s in j.facets.stacks),
            countries=_counter(j.country for j in jobs),
            remote=_counter(j.remote for j in jobs),
            sources=_counter(j.source for j in jobs),
            users=store.decision_users(),
            decision_statuses=DECISION_STATUSES,
            ui=ui,
            facets_version=FACETS_VERSION,
            database=store.path.name,
        )

    @app.get("/api/jobs", response_model=JobList)
    def list_jobs(
        store: StoreDep,
        report_id: int | None = None,
        section: list[str] | None = Query(None),
        langs: list[str] | None = Query(None),
        stack: list[str] | None = Query(None),
        country: list[str] | None = Query(None),
        remote: list[str] | None = Query(None),
        source: list[str] | None = Query(None),
        web_dev: bool | None = None,
        min_score: int | None = None,
        q: str | None = None,
        user: str | None = None,
        decision: list[str] | None = Query(None),
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> JobList:
        decisions = _codes(decision)
        if decisions and not user:
            raise HTTPException(status_code=400, detail="decision= requires user=")
        view = load_view(store, report_id, user)
        jobs = [
            j for j in _sorted_views(view)
            if _matches(view, j, langs=_codes(langs), stack=_codes(stack), section=_codes(section),
                        country=_codes(country, upper=True), remote=_codes(remote),
                        source=_codes(source), web_dev=web_dev, min_score=min_score, q=q,
                        decision=decisions)
        ]
        return JobList(total=len(jobs), items=jobs[offset:offset + limit])

    @app.get("/api/jobs/{job_id}", response_model=JobDetail)
    def get_job(store: StoreDep, job_id: str, report_id: int | None = None,
                user: str | None = None) -> JobDetail:
        view = load_view(store, report_id, user)
        job = view.jobs.get(job_id)
        item = next((i for i in view.snapshot.items if i.job_id == job_id), None)
        if job is None:  # in the DB but outside this report
            found = store.jobs(ids=[job_id])
            if not found:
                raise HTTPException(status_code=404, detail=f"no job {job_id}")
            job = found[0]
        section = item.section if item else None
        position = item.position if item else None
        return _job_view(view, job, section, position, detail=True)  # type: ignore[return-value]

    @app.put("/api/jobs/{job_id}/decision", response_model=Decision)
    def put_decision(store: StoreDep, job_id: str, body: DecisionIn) -> Decision:
        if not store.jobs(ids=[job_id]):
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        return store.save_decision(
            Decision(job_id=job_id, user=body.user, status=body.status, note=body.note)
        )

    @app.delete("/api/jobs/{job_id}/decision", status_code=204)
    def clear_decision(store: StoreDep, job_id: str, user: Handle) -> Response:
        if not store.delete_decision(job_id, user):
            raise HTTPException(status_code=404, detail="no decision to clear")
        return Response(status_code=204)

    @app.get("/api/decisions", response_model=list[Decision])
    def list_decisions(store: StoreDep, user: str | None = None) -> list[Decision]:
        return store.decisions(user)

    @app.get("/api/reports", response_model=list[ReportSnapshot])
    def list_reports(store: StoreDep) -> list[ReportSnapshot]:
        return store.reports()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(INDEX_HTML, media_type="text/html")

    return app
