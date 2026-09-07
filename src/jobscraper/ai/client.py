"""Claude calls for prefilter and ranking. One job per request, structured JSON output,
system prompt cached across the run, incremental (verdicts are stored per job/model/prompt)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from jobscraper.ai.prompts import PROMPT_VERSION, job_prompt, system_prompt
from jobscraper.ai.schemas import Ranking, Screening
from jobscraper.config import Profile
from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.store import Store

log = logging.getLogger(__name__)


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
        self.system = system_prompt(stage, profile)
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            log.warning("ANTHROPIC_API_KEY not set; relying on `ant auth login` profile if present")
        self.client = anthropic.Anthropic(max_retries=4)

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
        usage = {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
            "cache_read": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        }
        if response.stop_reason == "refusal" or response.parsed_output is None:
            log.warning("%s: no structured verdict for %s (%s)", self.model, job.id, response.stop_reason)
            return AIVerdict(job_id=job.id, stage=self.stage, model=self.model, prompt_version=PROMPT_VERSION,
                             relevant=True, score=50, language_ok=True, seniority_ok=True, location_ok=True,
                             summary=f"No verdict (stop_reason={response.stop_reason}); kept for manual review.",
                             concerns=["model gave no structured answer"], usage=usage)
        p = response.parsed_output
        return AIVerdict(job_id=job.id, stage=self.stage, model=self.model, prompt_version=PROMPT_VERSION,
                         relevant=p.relevant, score=p.score, language_ok=p.language_ok, seniority_ok=p.seniority_ok,
                         location_ok=p.location_ok, summary=p.summary, concerns=p.concerns,
                         why_apply=getattr(p, "why_apply", []), usage=usage)

    # ------------------------------------------------------------------ many
    def run(self, jobs: Iterable[Job], filters: dict[str, FilterResult], *, force: bool = False,
            progress: Callable[[AIVerdict], None] | None = None) -> dict[str, AIVerdict]:
        existing = {} if force else self.store.verdicts(self.stage, PROMPT_VERSION)
        existing = {k: v for k, v in existing.items() if v.model == self.model}
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


def estimate_cost(verdicts: Iterable[AIVerdict]) -> dict[str, float]:
    """Rough USD from usage counters, using list prices (per 1M tokens)."""
    prices = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}
    total = 0.0
    by_model: dict[str, float] = {}
    for v in verdicts:
        inp, out = prices.get(v.model, (5.0, 25.0))
        u = v.usage or {}
        cost = (u.get("input", 0) * inp + u.get("cache_write", 0) * inp * 1.25 + u.get("cache_read", 0) * inp * 0.1
                + u.get("output", 0) * out) / 1e6
        by_model[v.model] = by_model.get(v.model, 0.0) + cost
        total += cost
    by_model["total"] = total
    return by_model
