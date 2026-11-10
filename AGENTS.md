# jobScraper

Finds junior and intern software jobs across Finland, Estonia, the EU, Norway, Dubai and remote
boards, drops the obvious misses with rules, then has Claude screen and rank the rest. One
candidate per profile; the owner is the main user. `README.md` is the user guide, `docs/PLAN.md`
the design and the running plan.

## Shared rules

This project follows the owner's working agreements: https://github.com/Tonttu84/ai-workflow/blob/main/rules/working-agreements.md
(Claude Code loads them in every session through the user-level `CLAUDE.md`; other tools should read that file.)
This file holds only what is specific to this project and wins where it says so.

## Kind

**Solo.** Commit to `main` and push once the local gate is green; fresh-agent review before
pushing any non-trivial change. This overrides any tooling default that asks for a `claude/...`
working branch: if a session started on one, push the same commits to `main` too, and open no
pull request unless the owner asks. Don't commit half-written files from still-running
subagents; wait for their report, run the gate, then commit.

## Stack

- Python 3.11+, `uv`, Typer CLI (`jobscraper`), SQLite (one database per run under `data/runs/`).
- HTTP: `httpx` behind the polite client `src/jobscraper/http.py`; headless Chromium (Playwright)
  for Cloudflare-protected boards; `ats-scrapers` and `python-jobspy` for company boards and
  LinkedIn/Indeed.
- Models: pydantic (`src/jobscraper/models.py`, `config.py`); AI through the Anthropic SDK
  (`messages.parse`, structured output) or through Claude Code subagents
  (`scripts/ai_batches.py`).
- Web UI: FastAPI + uvicorn, read-only over `data/serve/`.
- Tests: pytest, pytest-xdist, pytest-cov; lint: ruff. No type checker yet.

## Commands

| Task | Command |
| --- | --- |
| Set up a fresh clone (installs the commit hooks) | `./scripts/setup.sh` |
| Run the whole pipeline | `uv run jobscraper run` (`--skip-ai` for no API calls) |
| Live-check the sources (owner's machine only) | `uv run jobscraper probe [source] [-v]` |
| Unit tests (no gate) | `uv run pytest -q` |
| Lint | `uv run ruff check src tests` |
| Function-size report (no gate) | `uv run ruff check src --select C901,PLR0912,PLR0913,PLR0915 --exit-zero` |
| Quick iteration, no browser suites, coverage not gated | `./scripts/check.sh --fast` |
| The local gate before every push (ruff, full pytest, coverage 99 %) | `./scripts/check.sh` |

Never run two checks (or a check and the browser suites) at the same time: the headless-browser
tests fail intermittently under contention. `scripts/check.sh` already runs the browser suites
on one worker after the parallel part.

## Testing

- Test first: for a new adapter, the fixture and parser test before the adapter body; for a rule
  or prompt change, a case in `tests/test_rules.py` / `tests/test_ai_stage.py` first.
- Coverage gate: the threshold in `scripts/check.sh` (99). Raise it as coverage grows, never
  lower it. Subset runs print coverage but do not gate.
- No network in tests: sources go through `FakeHttp` (`tests/conftest.py`), the Claude SDK
  through a monkeypatched `messages.parse`, third-party scrapers through monkeypatch.
- Briefs for coding subagents include this section and the adapter contract below.

## Adapter contract (`src/jobscraper/sources/`)

- One module per source; module-level `register(Source())` at the bottom.
- A class with `name`, `description`, `fetch(ctx: SourceContext) -> Iterable[Job]`.
- Use `ctx.http` (the polite client), never `requests` or raw `httpx`; `ctx.page(...)` for HTML
  that may need the headless browser. `ctx.opt("key", default)` reads options from
  `config/sources.yaml`. Respect `ctx.limit` (stop early when set).
- Normalize with `Job(...)` from `jobscraper.models`; helpers in `sources/_common.py`
  (`guess_country`, `guess_remote`, `parse_date`) and `http.strip_html`.
- Per-record failures: skip and log (`safe_records`). Endpoint failures: raise.
- Every adapter has a fixture (`tests/fixtures/<name>.json|xml|html`) and a test that feeds it
  through the parser and asserts title, company, url, country, remote and posted_at on at least
  one record.
- `complete_listing = True` only for an adapter that provably walks the whole listing (it lets
  the gone-posting counter act); `uses_browser = True` for one that calls `ctx.page`.

## Domain rules

- Pipeline: scrape (sources to SQLite) → filter (rules, permissive) → prefilter (Sonnet,
  permissive) → rank (Opus, top N) → check-live (top 40 pages; closed or gone postings drop) →
  report (markdown + DB) → publish → serve.
- The rule filter drops only on strong signals (senior title, 5+ years, a posting written in or
  requiring a language the profile does not work in, on-site outside the target countries,
  non-software title); everything softer is `review` and goes to the AI stage.
- AI verdicts are cached per job, model and prompt version: bump `PROMPT_VERSION` in
  `src/jobscraper/ai/prompts.py` whenever a prompt changes.
- The dossier and scoring weights go only to the Opus ranking prompt; the Sonnet screening prompt
  stays short and cheap. Exception: `deal_breakers` go to both prompts (the screen rejects clear
  matches; the ranker keeps them for borderline cases).
- Nothing about the candidate is hard-coded: everything candidate-specific lives in the profile.

## Private data

The repository is public. The owner's profile (`config/profile.yaml`, the CV), dossier answers
and notes, the CV parts library, other candidates' profiles, `results/`, run databases and
decisions are gitignored and mirrored into the private repo `Tonttu84/jobScraper-private` by
`scripts/sync_private.sh` (see `docs/PLAN.md` → "Private data"). The Anthropic API key lives in
`.env` (gitignored; `.env.example` holds the variable name). Never commit any of these.

## Owner workflows

### "Tailor my CV for this job: jobscraper tailor <id>" (or a batch)

That line comes from the web UI's "Tailor CV" button. There is no API key on the owner's machine,
so:

1. `PYTHONIOENCODING=utf-8 ./.venv/Scripts/jobscraper.exe tailor <id> --export`. For a batch use
   `--interested` (jobs marked interested in the web UI) or `--top N` instead of ids; cached jobs
   are skipped and `data/cv/tailored/prompts/batch.txt` lists `<id>	<prompt>	<answer>` per job.
2. One subagent per job, in parallel: read the prompt file, follow its SYSTEM/USER sections,
   write the JSON to the answer path, run `... tailor <id> --import` until it validates. Import
   trims an overflowing page by the answer's `drop_order` (the prompt shows mm).
3. Run the same `tailor ... --import` once more for the batch summary table, then show the owner
   each hand-off (`data/cv/tailored/CV - <Company>.md`: focus, rationale, gaps, changes from the
   standard CV) and its PDF next to it. The model only picks ids from `config/cv-parts.yaml`;
   never write CV text yourself: new text goes into the library, by the owner, first.

Naming (`jobscraper.cv.files`): `CV - <Company>.pdf/.md`, `CV - <Company> - <Title>` when another
job owns the company name (`tailored/index.json`: stem → job id); a changed re-tailoring moves the
old pair to `tailored/older/CV - X (<mtime>)`; `prompts/` and `answers/` stay keyed by job id.

### A new CV from the owner (or "improve matching")

Before touching `config/profile.yaml`, ask one question with these options and wait:

1. **Just use the CV**: update `summary`, `cv_text`, `skills`, `interests`; nothing else.
2. **Go through the matching levers** (`docs/PLAN.md` → "Improving match quality"): an AI-only
   dossier built from the questionnaire (`docs/DOSSIER-QUESTIONNAIRE.md`), the owner's own
   applied/skipped verdicts as prompt anchors, and explicit scoring weights. Offer them one at a
   time; each is optional.

### AI stages without API credits

`scripts/ai_batches.py export|import prefilter|rank` runs the same prompts through Claude Code
subagents and stores the verdicts as an API run would (README → "AI stages without API credits").

## Layout

- `src/jobscraper/cli.py`: every command. `sources/`: one adapter per board, shared helpers in
  `_common.py`, `_grade.py`, `_cornerstone.py`. `filters/`: rule filter, language, deadline,
  evergreen. `ai/`: prompts, schemas, the SDK client. `cv/`: tailored CV (parts, fit, render).
  `web/`: the FastAPI app and its static page. `store.py`: SQLite. `report.py`, `compare.py`:
  reports and diffs. `health.py`, `sentinel.py`, `audit.py`, `runstats.py`: pipeline
  self-observation. `liveness.py`: closed-posting checks.
- `config/`: `sources.yaml` (tracked), `profile.example.yaml` and `cv-parts.example.yaml`
  (templates for the gitignored real files), `profiles/` (other candidates, gitignored).
- `tests/`: one test module per area; payload fixtures in `tests/fixtures/`.
- `scripts/`: `check.sh` (the gate), `setup.sh`, `ai_batches.py`, `probe_boards.py`,
  `scheduled_run.sh`, `sync_private.sh`, `rank_boards_from_jobhive.py`.
- `stats/`: tracked per-run statistics. `docs/`: plan, scheduling, web API, board ranking.

## Quirks

- The Claude Code cloud sandbox cannot reach any job site (egress: GitHub, PyPI, npm only).
  Adapters are written against documented or observed payloads and tested with fixtures; live
  verification is `jobscraper probe` on the owner's machine.
- Things only the owner can run (probes, a first batch run, scheduling) go into `docs/PLAN.md` →
  "TODO on the owner's machine". Add to that list; don't repeat them in chat.
- Several boards block cloud IP ranges, so `.github/workflows/scheduled-run.yml` is for a
  self-hosted runner only and stays off until the repository variable `SCHEDULED_RUNS` is `true`.
- Headless Chromium needs `uv run playwright install chromium` once per machine; its profile in
  `data/browser/` keeps the Cloudflare clearance cookie between runs.
- `JOBSCRAPER_HTTP_CACHE=1` caches responses in `data/cache/` (handy for new fixtures);
  `JOBSCRAPER_PROFILE`, `JOBSCRAPER_DB` and the `JOBSCRAPER_*_DIR` variables move a run.
- On Windows, set `PYTHONIOENCODING=utf-8` when calling `jobscraper.exe` directly.

## Decisions

`docs/decisions.md` (D1 onward); the decisions from before the migration are in `docs/PLAN.md`
→ "Decisions (2026-09-07)" and the dated sections after it.
