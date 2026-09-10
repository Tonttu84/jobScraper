# jobScraper

Finds junior / intern software jobs across Finland, Estonia, the EU + Norway, Dubai, and remote
boards; auto-filters them with cheap rules; then lets Claude screen and rank what is left.

```
scrape → filter (rules) → prefilter (Sonnet 5, permissive) → rank (Opus 5, top N) → report (markdown + DB) → publish → serve (web UI)
 21 sources   SQLite         structured JSON verdicts          top N explained        results/ + runs/*.db   copy to data/serve/  FastAPI, read-only
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

`ats_boards` is 80-odd company career boards behind one source, so `probe ats_boards` stops at
the first five jobs and tells you nothing about the rest. Probe them one at a time instead:

```bash
uv run python scripts/probe_boards.py                    # every board: listed / kept / seconds / a sample
uv run python scripts/probe_boards.py greenhouse workday # only boards whose label matches
```

Which companies are worth adding, and how that list was built, is in
[`docs/boards-ranked-2026-09-10.md`](docs/boards-ranked-2026-09-10.md); the ranking script is
`scripts/rank_boards_from_jobhive.py`.

### Cloudflare sites (headless browser)

Some boards (jobly.fi, sometimes duunitori.fi) answer a plain HTTP client with Cloudflare's
"Just a moment…" page. Those pages are fetched with a headless Chromium instead:

```bash
uv sync --extra dev                 # installs Playwright
uv run playwright install chromium  # once per machine, downloads the browser (~150 MB)
uv run jobscraper probe jobly       # a source line ends with "(browser)" when Chromium was used
```

`http.browser` in `config/sources.yaml` is the switch: `auto` (default) lets a source start the
browser when it is turned away — nothing is launched until one actually needs it — and `never`
turns it off, so such a source simply fails with "needs a headless browser". Per source, the
`mode` option decides how its pages are fetched: `browser` (straight to Chromium, for a site
known to be protected), `auto` (plain HTTP first, browser as fallback) or `http` (never).

The browser profile lives in `data/browser/`, so the Cloudflare clearance cookie survives
between runs and the challenge is normally solved once. Delete that directory to start clean.

## Daily use

```bash
uv run jobscraper run                   # new data/runs/<timestamp>.db, then scrape → filter → prefilter → rank → report
uv run jobscraper run --skip-ai         # no API calls: scrape + rules + report only
uv run jobscraper run --fresh           # start the run from an empty database instead of copying the last one forward
uv run jobscraper run --batch           # same pipeline with the prefilter sent through the Message Batches API
uv run jobscraper runs                  # list per-run databases (* = the one commands use by default)
uv run jobscraper --db data/runs/20260909-101500.db report   # any command against a specific database
uv run jobscraper scrape linkedin indeed
uv run jobscraper filter --days 14
uv run jobscraper prefilter --max-jobs 50     # try the Sonnet pass on a sample first
uv run jobscraper prefilter --batch           # same verdicts at half price via the Message Batches API (async; --no-wait submits and exits, re-run to collect)
uv run jobscraper rank --top 30
uv run jobscraper report
uv run jobscraper diff                  # what changed since the previous report — see docs/SCHEDULING.md
uv run jobscraper stats
uv run jobscraper facets                # backfill the filter facets of an existing DB, no AI re-runs
uv run jobscraper publish --name week37 # copy the current DB to data/serve/week37.db
uv run jobscraper serve                 # web UI over data/serve/ → http://127.0.0.1:8000
```

### Profiles (more than one candidate)

Everything candidate-specific lives in `config/profile.yaml`: the CV, the language / seniority /
location policies, the role terms, and the wording of the AI prompts (`prompt.role_label`,
`prompt.seniority_rule`, `prompt.role_rule`, `prompt.extra_rules`). Nothing about the owner is
hard-coded, so a second person only needs their own copy of that file.

```bash
uv run jobscraper profile-init ana      # copies config/profile.yaml + sources.yaml to config/profiles/ana/
$EDITOR config/profiles/ana/profile.yaml
uv run jobscraper profiles              # which profiles exist (* = active)
uv run jobscraper --profile ana run     # scrape → … → report, all under that profile
uv run jobscraper --profile ana publish --name week37
uv run jobscraper --profile ana serve   # serves data/profiles/ana/serve/
```

`--profile ana` (or `JOBSCRAPER_PROFILE=ana`) moves the whole run: config from
`config/profiles/ana/`, databases, `runs/`, `serve/`, `exports/` and `cache/` under
`data/profiles/ana/`, reports into `results/ana/`. Without it everything behaves exactly as
before. `config/profiles/` is gitignored — other people's CV text must not be pushed.

### Databases

Every `run` creates its own SQLite file, `data/runs/<timestamp>.db`, by snapshotting the
previous run's database (so first-seen dates, cached AI verdicts and decisions carry over) and
then working only on the new file; the earlier files are never touched again. `--fresh` starts
from an empty database. Commands without `--db` use the newest run database (or the legacy
`data/jobs.db` if there are no runs yet); `$JOBSCRAPER_DB` overrides that too.

The web UI never reads the run databases directly. `jobscraper publish` copies whichever
database you choose into `data/serve/` (any name you like), and `jobscraper serve` shows the
most recently modified `*.db` copy in that directory, opened read-only. Decisions made in the
UI go to `data/serve/decisions.db`, a sidecar keyed by job id, so you can replace the served
copy with a newer one and keep everyone's marks. To expose a different snapshot, publish (or
copy) another file there; to roll back, delete the newer copy. `serve --dir <path>` serves
another directory, `serve --db-file <db>` serves one writable database the old way.

Other outputs: `results/report-YYYY-MM-DD.md` (ranked list, with a short "new since report #N"
section on top), `results/diff-YYYY-MM-DD.md` (the whole diff against the previous report),
`data/exports/filtered.jsonl`
(rule survivors with verdicts, for review in Claude Code or a spreadsheet). Each `report` run is
also stored *in* the database — the run and its counts in `reports`, its ordered sections in
`report_items` — which is what `serve` reads. AI verdicts are cached per job, model and prompt
version, so re-runs only pay for new jobs. Change `PROMPT_VERSION` in
`src/jobscraper/ai/prompts.py` when you edit the prompts.

### Sharing with other students

`jobscraper serve` is meant to be usable by more than one person: everyone types a handle into
the page, and decisions (applied / skipped / interested / interview / rejected / offer) are
stored per handle, so you each keep your own list over the same jobs. The filters are built for
that too — tick the languages you actually speak (a posting drops out when it is written in, or
requires, a language you did not tick), and untick "I have done Full Stack Open" if you have
not, which drops the React/Node/TypeScript-ish roles that expect web development. There is **no authentication**: keep it on localhost, or
put it behind a tunnel (`cloudflared`, `tailscale funnel`) or a reverse proxy with basic auth
before sharing the URL. See [docs/WEB.md](docs/WEB.md) for the API and the data model.

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

Every action takes `--profile NAME` (or `$JOBSCRAPER_PROFILE`), like the CLI: config, database and
exports all move under that candidate's directories. `export` also takes `--sample N`, which keeps
every N-th candidate (positions 0, N, 2N, … after `--top`), so `export prefilter --sample 20` is a
"1 in 20" sanity run through the whole loop before committing a few hundred postings to it.

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
