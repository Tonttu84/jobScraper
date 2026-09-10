"""Claude calls for prefilter and ranking. One job per request, structured JSON output,
system prompt cached across the run, incremental (verdicts are stored per job/model/prompt).

Two ways to spend the same prompts:

``run()``     one live ``messages.parse`` call per job over a small thread pool — answers in
              seconds, list price.
``run_batch`` every pending job as one Message Batch — the same verdicts at half price, but
              asynchronous (usually under an hour, up to 24 h). Submitted batch ids are stored,
              so an interrupted wait is picked up by the next invocation instead of paying twice.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import BaseModel, ValidationError

from jobscraper.ai.prompts import PROMPT_VERSION, job_prompt, refine_user_prompt, system_prompt
from jobscraper.ai.schemas import Ranking, RefinedJob, Refinement, Screening
from jobscraper.config import Profile
from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.store import Store

log = logging.getLogger(__name__)


def output_schema(model_cls: type[BaseModel]) -> dict:
    """The ``output_config["format"]`` block that ``messages.parse()`` builds from a model.

    ``parse()`` is not available inside a Message Batch, so the batch path has to pass the
    structured-output schema itself. This wraps the SDK's own (private) transform so there is
    one place to change if it moves.
    """
    from anthropic.lib._parse._transform import transform_schema

    return {"type": "json_schema", "schema": transform_schema(model_cls)}


def _chunks(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), max(1, size)):
        yield items[start:start + size]


def _client() -> anthropic.Anthropic:
    """The SDK client every stage uses, with a warning when no credentials are in the environment."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.warning("ANTHROPIC_API_KEY not set; relying on `ant auth login` profile if present")
    return anthropic.Anthropic(max_retries=4)


class AIStage:
    def __init__(self, stage: str, profile: Profile, store: Store, model: str | None = None) -> None:
        assert stage in ("prefilter", "rank")
        self.stage = stage
        self.profile = profile
        self.store = store
        ai = profile.ai
        self.model = model or (ai.prefilter_model if stage == "prefilter" else ai.rank_model)
        self.effort = ai.prefilter_effort if stage == "prefilter" else ai.rank_effort
        self.schema = Screening if stage == "prefilter" else Ranking
        self.max_chars = ai.max_description_chars
        self.concurrency = ai.concurrency
        self.batch_chunk = ai.batch_chunk
        self.system = system_prompt(stage, profile)
        self.client = _client()

    # ------------------------------------------------------------- verdicts
    @staticmethod
    def _usage(usage) -> dict[str, int | bool]:
        return {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }

    def _no_verdict(self, job_id: str, reason: str, usage: dict[str, int | bool]) -> AIVerdict:
        """What both paths store when the model gave no usable structured answer: keep the job,
        score it in the middle, and say why, so a human still sees it in the report."""
        log.warning("%s: no structured verdict for %s (%s)", self.model, job_id, reason)
        return AIVerdict(job_id=job_id, stage=self.stage, model=self.model, prompt_version=PROMPT_VERSION,
                         relevant=True, score=50, language_ok=True, seniority_ok=True, location_ok=True,
                         summary=f"No verdict ({reason}); kept for manual review.",
                         concerns=["model gave no structured answer"], usage=usage)

    def _verdict(self, job_id: str, parsed, usage: dict[str, int | bool]) -> AIVerdict:
        return AIVerdict(job_id=job_id, stage=self.stage, model=self.model, prompt_version=PROMPT_VERSION,
                         relevant=parsed.relevant, score=parsed.score, language_ok=parsed.language_ok,
                         seniority_ok=parsed.seniority_ok, location_ok=parsed.location_ok,
                         summary=parsed.summary, concerns=parsed.concerns,
                         why_apply=getattr(parsed, "why_apply", []), usage=usage)

    # ------------------------------------------------------------------ one job
    def judge(self, job: Job, fr: FilterResult | None) -> AIVerdict:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": job_prompt(job, fr, self.max_chars)}],
            output_format=self.schema,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        )
        usage = self._usage(response.usage)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return self._no_verdict(job.id, f"stop_reason={response.stop_reason}", usage)
        return self._verdict(job.id, response.parsed_output, usage)

    # ------------------------------------------------------------------ many
    def run(self, jobs: Iterable[Job], filters: dict[str, FilterResult], *, force: bool = False,
            progress: Callable[[AIVerdict], None] | None = None) -> dict[str, AIVerdict]:
        existing = self._existing(force)
        todo = [j for j in jobs if j.id not in existing]
        results = dict(existing)
        if not todo:
            return results
        log.info("%s: scoring %d jobs with %s (%d cached)", self.stage, len(todo), self.model, len(existing))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self.judge, j, filters.get(j.id)): j for j in todo}
            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    verdict = fut.result()
                except anthropic.APIError as exc:
                    log.error("%s: API error for %s: %s", self.stage, job.id, exc)
                    continue
                self.store.save_verdict(verdict)
                results[job.id] = verdict
                if progress:
                    progress(verdict)
        return results

    def _existing(self, force: bool) -> dict[str, AIVerdict]:
        if force:
            return {}
        stored = self.store.verdicts(self.stage, PROMPT_VERSION)
        return {k: v for k, v in stored.items() if v.model == self.model}

    # ---------------------------------------------------------------- batches
    def _params(self, job: Job, fr: FilterResult | None) -> MessageCreateParamsNonStreaming:
        """The same request ``judge()`` makes, spelled out as params a batch entry can carry."""
        return MessageCreateParamsNonStreaming(
            model=self.model,
            max_tokens=4000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": job_prompt(job, fr, self.max_chars)}],
            thinking={"type": "adaptive"},
            output_config={"format": output_schema(self.schema), "effort": self.effort},
        )

    def _from_message(self, job_id: str, message) -> AIVerdict:
        """A batch result's ``Message`` → the verdict, marked as batch-priced."""
        usage = self._usage(message.usage)
        usage["batch"] = True
        text = next((b.text for b in (message.content or []) if getattr(b, "type", None) == "text"), None)
        if message.stop_reason == "refusal" or not text:
            return self._no_verdict(job_id, f"stop_reason={message.stop_reason}", usage)
        try:
            parsed = self.schema.model_validate_json(text)
        except ValidationError as exc:
            log.debug("%s: unparseable answer for %s: %s", self.stage, job_id, exc)
            return self._no_verdict(job_id, "answer did not match the schema", usage)
        return self._verdict(job_id, parsed, usage)

    def _ingest(self, batch_id: str, results: dict[str, AIVerdict],
                progress: Callable[[AIVerdict], None] | None) -> int:
        """Store every succeeded result of an ended batch; log the rest so they are retried."""
        stored = 0
        for r in self.client.messages.batches.results(batch_id):
            if r.result.type != "succeeded":
                error = getattr(r.result, "error", None)
                log.warning("%s: batch %s request %s %s%s", self.stage, batch_id, r.custom_id,
                            r.result.type, f": {error}" if error is not None else "")
                continue
            verdict = self._from_message(r.custom_id, r.result.message)
            self.store.save_verdict(verdict)
            results[r.custom_id] = verdict
            stored += 1
            if progress:
                progress(verdict)
        log.info("%s: batch %s stored %d verdicts", self.stage, batch_id, stored)
        return stored

    def _await(self, batch_id: str, poll_seconds: int) -> None:
        while True:
            batch = self.client.messages.batches.retrieve(batch_id)
            c = batch.request_counts
            log.info("%s: batch %s %s (processing=%d succeeded=%d errored=%d canceled=%d expired=%d)",
                     self.stage, batch_id, batch.processing_status, c.processing, c.succeeded,
                     c.errored, c.canceled, c.expired)
            if batch.processing_status == "ended":
                return
            time.sleep(poll_seconds)

    def run_batch(self, jobs: Iterable[Job], filters: dict[str, FilterResult], *, force: bool = False,
                  progress: Callable[[AIVerdict], None] | None = None, wait: bool = True,
                  poll_seconds: int = 30) -> dict[str, AIVerdict]:
        """Score the pending jobs through the Message Batches API (50% of list price).

        Same incremental rule as :meth:`run`: a job with a stored verdict for this model and
        prompt version is skipped unless ``force``. Batches submitted by an earlier invocation
        are picked up first, and jobs still covered by one are not resubmitted. With
        ``wait=False`` the batches are only submitted — call again later to collect them.
        """
        jobs = list(jobs)
        results = dict(self._existing(force))

        in_flight = self._resume(results, progress)
        covered = {job_id for rec in in_flight for job_id in rec["job_ids"]}
        todo = [j for j in jobs if j.id not in results and j.id not in covered]

        waiting = [rec["id"] for rec in in_flight]
        for chunk in _chunks(todo, self.batch_chunk):
            requests = [Request(custom_id=j.id, params=self._params(j, filters.get(j.id))) for j in chunk]
            batch = self.client.messages.batches.create(requests=requests)
            self.store.save_batch(batch.id, self.stage, self.model, PROMPT_VERSION, [j.id for j in chunk])
            waiting.append(batch.id)
            log.info("%s: submitted batch %s with %d requests", self.stage, batch.id, len(chunk))

        if not waiting:
            return results
        if not wait:
            log.info("%s: %d batch(es) in flight, not waiting: %s", self.stage, len(waiting), ", ".join(waiting))
            return results
        for batch_id in waiting:
            self._await(batch_id, poll_seconds)
            self._ingest(batch_id, results, progress)
            self.store.finish_batch(batch_id, "done")
        return results

    def _resume(self, results: dict[str, AIVerdict],
                progress: Callable[[AIVerdict], None] | None) -> list[dict]:
        """Collect batches an earlier invocation submitted; return the ones still running."""
        running: list[dict] = []
        for rec in self.store.pending_batches(self.stage):
            if rec["model"] != self.model or rec["prompt_version"] != PROMPT_VERSION:
                continue  # a batch for another model or an older prompt; leave it alone
            batch = self.client.messages.batches.retrieve(rec["id"])
            if batch.processing_status == "ended":
                log.info("%s: picking up batch %s from an earlier run", self.stage, rec["id"])
                self._ingest(rec["id"], results, progress)
                self.store.finish_batch(rec["id"], "done")
            else:
                log.info("%s: batch %s from an earlier run is still %s", self.stage, rec["id"],
                         batch.processing_status)
                running.append(rec)
        return running


#: Models whose thinking is not ours to configure: it is always on for Fable (an explicit
#: ``thinking`` block is rejected outright) and adaptive by default on Opus 5.
_THINKING_IS_IMPLICIT = ("fable", "opus")


def refine_verdicts(items: Iterable[RefinedJob], job_ids: Iterable[str], model: str,
                    usage: dict[str, int | bool] | None = None) -> list[AIVerdict]:
    """Map one :class:`Refinement` answer onto verdicts, in the order the model gave them.

    Ids that were never sent, and repeats of an id already answered, are dropped with a warning;
    ids the model forgot are logged (the report falls back to their rank score). ``usage`` rides
    on the first verdict only — the whole shortlist cost one request, not one per job.
    """
    known, seen = list(job_ids), set()
    verdicts: list[AIVerdict] = []
    for item in items:
        if item.job_id not in known:
            log.warning("refine: unknown job_id %s in the answer, dropped", item.job_id)
            continue
        if item.job_id in seen:
            log.warning("refine: duplicate job_id %s in the answer, dropped", item.job_id)
            continue
        seen.add(item.job_id)
        verdicts.append(AIVerdict(
            job_id=item.job_id, stage="refine", model=model, prompt_version=PROMPT_VERSION,
            relevant=True, score=item.score, position=item.position, summary=item.summary,
            language_ok=True, seniority_ok=True, location_ok=True, concerns=[], why_apply=[],
            usage=dict(usage or {}) if not verdicts else {}))
    missing = [job_id for job_id in known if job_id not in seen]
    if missing:
        log.warning("refine: %d job(s) missing from the answer: %s", len(missing), ", ".join(missing))
    return verdicts


class RefineStage:
    """One request that ranks the whole shortlist against itself.

    The rank stage scores each posting alone inside a chunk, so its score carries chunk noise
    (measured 2026-09-10: median drift 3 points, p90 8, top-10 overlap 0.54 between two runs).
    Seeing every shortlisted posting in one call removes that noise where it matters. A refusal
    or an unparseable answer stores nothing, and the report falls back to the rank scores.
    """

    def __init__(self, profile: Profile, store: Store, model: str | None = None) -> None:
        ai = profile.ai
        self.profile = profile
        self.store = store
        self.model = model or ai.refine_model
        self.effort = ai.refine_effort
        self.max_chars = ai.max_description_chars
        self.system = system_prompt("refine", profile)
        self.client = _client()

    def _thinking(self) -> dict[str, dict]:
        """``thinking`` as request kwargs — empty for the models that decide it themselves."""
        if any(family in self.model.lower() for family in _THINKING_IS_IMPLICIT):
            return {}
        return {"thinking": {"type": "adaptive"}}

    def _split(self, jobs: list[Job], force: bool) -> tuple[list[Job], list[tuple[Job, AIVerdict]]]:
        """(new jobs, anchors) — the shortlist members without and with a refine verdict.

        Anchors are ordered the way the previous pass placed them, so the prompt reads as a list.
        """
        if force:
            return list(jobs), []
        placed = self.store.verdicts("refine", PROMPT_VERSION)
        todo = [j for j in jobs if j.id not in placed]
        anchors = [(j, placed[j.id]) for j in jobs if j.id in placed]
        anchors.sort(key=lambda pair: (pair[1].position is None, pair[1].position or 0, -pair[1].score))
        return todo, anchors

    def run(self, jobs: list[Job], filters: dict[str, FilterResult], *,
            force: bool = False) -> list[AIVerdict]:
        """Score the shortlist members that are new, and return their verdicts in answer order.

        Incremental by default: a job that already carries a refine verdict under this prompt
        version is not re-scored, it is sent as a fixed anchor so the new ones land in the same
        ordering. ``force=True`` re-scores the whole shortlist against itself, as the first pass
        over a shortlist does anyway.
        """
        if not jobs:
            return []
        todo, anchors = self._split(jobs, force)
        if not todo:
            log.info("refine: all %d shortlisted jobs are already placed; nothing new to compare",
                     len(jobs))
            return []
        log.info("refine: judging %d new job(s) against %d already placed with %s",
                 len(todo), len(anchors), self.model)
        prompt = refine_user_prompt(todo, filters, self.max_chars, anchors=anchors or None)
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_format=Refinement,
            output_config={"effort": self.effort},
            **self._thinking(),
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            log.warning("refine: %s gave no structured answer (stop_reason=%s); keeping the rank "
                        "scores", self.model, response.stop_reason)
            return []
        verdicts = refine_verdicts(response.parsed_output.items, [j.id for j in todo], self.model,
                                   AIStage._usage(response.usage))
        for v in verdicts:
            self.store.save_verdict(v)
        return verdicts


def estimate_cost(verdicts: Iterable[AIVerdict]) -> dict[str, float]:
    """Rough USD from usage counters, using list prices (per 1M tokens).

    Verdicts that came from a Message Batch carry ``usage["batch"]`` and are billed at half.
    """
    prices = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
              "claude-haiku-4-5": (1.0, 5.0), "claude-fable-5-1": (10.0, 50.0)}
    total = 0.0
    by_model: dict[str, float] = {}
    for v in verdicts:
        inp, out = prices.get(v.model, (5.0, 25.0))
        u = v.usage or {}
        cost = (u.get("input", 0) * inp + u.get("cache_write", 0) * inp * 1.25 + u.get("cache_read", 0) * inp * 0.1
                + u.get("output", 0) * out) / 1e6
        if u.get("batch"):
            cost *= 0.5
        by_model[v.model] = by_model.get(v.model, 0.0) + cost
        total += cost
    by_model["total"] = total
    return by_model
