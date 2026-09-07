# jobScraper

Finds junior / intern software jobs across Finland, Estonia, the EU + Norway, Dubai, and remote
boards; auto-filters them with cheap rules; then lets Claude screen and rank what is left.

```
scrape  →  filter (rules)  →  prefilter (Sonnet 5, permissive)  →  rank (Opus 5, top N)  →  report
 21 sources    SQLite            structured JSON verdicts             markdown + JSONL
```

See `docs/PLAN.md` for the design, the source list, and the decisions behind it.

## Setup (your machine)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:tonttu84/jobScraper.git && cd jobScraper
uv sync --extra dev            # creates .venv, installs everything incl. python-jobspy from git
cp .env.example .env           # put your ANTHROPIC_API_KEY in it (only needed for prefilter/rank)
uv run pytest -q               # fixture-based tests, no network
./scripts/check.sh             # lint + tests + coverage gate (definition of done)
```

Load the key with `set -a; source .env; set +a` (or use `ant auth login`, which the SDK picks up).

## First run: verify the sources

The scrapers were written against documented payload shapes without live access, so the first
job is to see which ones work from your network:

```bash
uv run jobscraper probe                 # every enabled source, 5 jobs each, prints ✓/✗ per source
uv run jobscraper probe duunitori -v    # one source, verbose logging
JOBSCRAPER_HTTP_CACHE=1 uv run jobscraper probe nofluffjobs   # cache responses while fixing a parser
```

Disable anything broken in `config/sources.yaml` (`enabled: false`) or fix the adapter in
`src/jobscraper/sources/<name>.py`. Cached responses live in `data/cache/` and are handy as new
test fixtures.

## Daily use

```bash
uv run jobscraper run                   # scrape → filter → prefilter → rank → report
uv run jobscraper run --skip-ai         # no API calls: scrape + rules + report only
uv run jobscraper scrape linkedin indeed
uv run jobscraper filter --days 14
uv run jobscraper prefilter --max-jobs 50     # try the Sonnet pass on a sample first
uv run jobscraper rank --top 30
uv run jobscraper report
uv run jobscraper stats
```

Outputs: `data/jobs.db` (everything), `data/reports/report-YYYY-MM-DD.md` (ranked list),
`data/exports/filtered.jsonl` (rule survivors with verdicts, for review in Claude Code or a
spreadsheet). AI verdicts are cached per job, model and prompt version, so re-runs only pay for
new jobs. Change `PROMPT_VERSION` in `src/jobscraper/ai/prompts.py` when you edit the prompts.

## AI stages without API credits (subagent mode)

The prefilter/rank stages can run through Claude Code subagents instead of the API, on a local
session, with the same prompts and the same stored verdicts:

```bash
uv run python scripts/ai_batches.py export prefilter        # data/exports/ai/prefilter/{system.txt,chunk-NN.json}
# ask one Sonnet subagent per chunk to write data/exports/ai/prefilter/verdicts/chunk-NN.jsonl
uv run python scripts/ai_batches.py import prefilter
uv run python scripts/ai_batches.py export rank --top 60    # top prefilter survivors, full descriptions + CV
# ask one Opus subagent per chunk to write data/exports/ai/rank/verdicts/chunk-NN.jsonl
uv run python scripts/ai_batches.py import rank
uv run jobscraper report
```

Each verdict line is `{"job_id", "relevant", "score", "language_ok", "seniority_ok", "location_ok",
"summary", "concerns"[, "why_apply"]}`. Import validates against the stage schema and stores the rows
under the current `PROMPT_VERSION`, so `report` renders them like an API run. On 2026-09-07 the cheap
pass cost about 1,500 subagent tokens per posting (100 postings ≈ 5-8 minutes per Sonnet agent).

## Tuning

- `config/profile.yaml`: who you are (used in the prompts), language policy, seniority limits,
  target countries by tier, role keyword gates, AI models and thresholds.
- `config/sources.yaml`: queries, page limits, Teamtailor tenants, ATS career URLs, on/off.

The rule filter is deliberately permissive: it only *drops* on strong signals (senior title,
5+ years, posting written in a language you don't work in, an explicit requirement for one,
on-site outside the target countries, non-software title). Everything softer is marked `review`
and goes to the AI stage with the detected signals attached.

## Cost

Per full run on ~300 rule survivors: Sonnet prefilter roughly $1, Opus ranking of the top 60
roughly $1.50, before caching. Incremental runs cost only for new jobs.
