# jobScraper — working agreements

## Delegation rule (from the owner)
- Simple, well-specified coding tasks (a new source adapter, a fixture + test, a small refactor,
  a bug with a known cause) go to an **Opus** subagent (`model: "opus"`), one task per agent, with
  the adapter contract below pasted into the prompt.
- The main (Fable) session does architecture, tricky debugging, prompt design, and review of
  subagent output. Don't spend Fable on legwork.

## Environment facts
- The Claude Code cloud sandbox cannot reach any job site (egress allowlist: GitHub, PyPI, npm only).
  Adapters are written against documented/observed payload shapes and tested with fixtures in
  `tests/fixtures/`. Live verification: `jobscraper probe` on the owner's machine.
- Python 3.11+, `uv`. Run tests with `uv run pytest -q`. Lint with `uv run ruff check src tests`.

## Adapter contract (src/jobscraper/sources/)
- One module per source; module-level `register(Source())` at the bottom.
- Class with `name`, `description`, `fetch(ctx: SourceContext) -> Iterable[Job]`.
- Use `ctx.http` (polite client) — never `requests`/raw `httpx`. Use `ctx.opt("key", default)` for
  options from `config/sources.yaml`. Respect `ctx.limit` (stop early when set).
- Normalize with `Job(...)` from `jobscraper.models`; helpers in `sources/_common.py`
  (`guess_country`, `guess_remote`, `parse_date`) and `http.strip_html`.
- Per-record failures: skip and log (`safe_records`). Endpoint failures: raise.
- Every adapter has a fixture (`tests/fixtures/<name>.json|xml|html`) and a test that feeds it
  through the parser and asserts title/company/url/country/remote/posted_at on at least one record.

## Pipeline
scrape (sources → SQLite) → filter (rules, permissive) → prefilter (Sonnet 5, permissive) →
rank (Opus 5, top N) → report (markdown + JSONL).
