# Plan

## Goal
Surface the best intern / junior software jobs for the owner (Helsinki; EN/FI/DE; ~2 years of
project-based coding; the profile itself is private, see "Private data" below) with as little
manual scrolling as possible:
1. Pull postings from many boards into one normalized store.
2. Drop the obvious misses with deterministic rules.
3. Have a cheap model screen the rest permissively, then a strong model rank the survivors and
   explain fit, risks, and how to angle each application.

## Decisions (2026-09-07)
| Topic | Decision |
|---|---|
| Stack | Python 3.11, `uv`, SQLite, `httpx`, `typer`. Reuse `ats-scrapers` (company career boards) and `python-jobspy` (LinkedIn/Indeed). Everything else written here. |
| Seniority | intern, trainee, junior, graduate, and unlabeled roles asking ≤2 years. 3-4 years → review. 5+ or senior/lead titles → drop. Degree requirements are not a blocker. |
| Languages | Working language must be EN/FI/DE. Posting on a Polish/German/French board that needs only English is fine. Posting *written* in another language → drop. Other language *required* → drop; Swedish required → review. "X is a plus" never drops. |
| Location | Tier 1 FI/EE, tier 2 EU/EEA+NO+CH, tier 3 Dubai + ex-Eastern-bloc (GE, AZ, AM, BA, RS, ME, MK, AL, MD, XK, UA, KZ, UZ, KG, TR) with work-rights notes. On-site elsewhere → drop. |
| Remote | Never auto-dropped. Region text classified (europe / worldwide / us_only / unknown); US-only is flagged "check eligibility", not rejected, because US pay is worth the effort. |
| Employment type | Everything, including part-time, freelance, contract. |
| AI | Sonnet 5 permissive prefilter (effort low) → Opus 5 ranks the top 60 (effort high). Structured JSON via `messages.parse`, system prompt cached, verdicts stored per job/model/prompt version. Upgrade models later if the results look weak. |
| Delegation | Simple coding tasks go to Opus subagents; Fable does architecture and review (see CLAUDE.md). |

## Environment constraint
The Claude Code cloud sandbox cannot reach any job site (only GitHub/PyPI/npm). All adapters were
written from documented payload shapes and other open-source scrapers, and tested against
fixtures. **Every source must be verified with `jobscraper probe` on the owner's machine** before
the first real run; expect a few to need small parser fixes.

## Sources
| Source | Region | Method | Confidence | Notes |
|---|---|---|---|---|
| duunitori | FI | JSON `/api/v1/jobentries` | medium | Cloudflare seen on robots.txt in Sept 2026; HTML fallback parser included |
| tyomarkkinatori | FI | internal JSON search + detail | medium | detail endpoint 403s after a few hundred calls; capped |
| thehub | Nordics | JSON `/api/v2/jobs` | high | startup jobs, some junior |
| teamtailor | FI/EE/Nordics | `<tenant>.teamtailor.com/jobs.json` / RSS | high | tenant list in sources.yaml; add companies you like |
| ats_boards | any | ats-scrapers (Greenhouse/Lever/Ashby/Workable/…) | high | careers URLs in sources.yaml; custom-domain enterprise sites (Eightfold/Oracle/SuccessFactors) as explicit `ats:` + `slug:` entries; per-board `include`/`exclude` title regexes (source-level `default_include`/`default_exclude`) filter before conversion, and a filtered board with per-posting detail support fetches descriptions lazily for the survivors only. Verified live 2026-09-10: Nokia, SAP, Wärtsilä, Volvo, Bosch, Ubisoft, Spotify, Adyen, N26, Celonis, Ericsson, Microsoft (counts in `sources.yaml`); Siemens (Avature, 6/page) and Delivery Hero (Attrax) not scrapeable with this library |
| cvee | EE | JSON search API | medium | postings mostly EE/EN; language filter handles it |
| cvkeskus | EE | HTML + JSON-LD | medium | |
| linkedin | any | python-jobspy guest endpoint | medium | rate-limited; polite delays; no descriptions by default |
| indeed | FI/EE/SE/NO/DE/NL/AE | python-jobspy | medium | Indeed country list lacks Estonia's own site; jobspy maps it |
| nofluffjobs | PL/CEE/NL | JSON search + detail | high | junior/trainee filter; English-only roles common |
| justjoin | PL | JSON cursor API | high | |
| arbeitnow | DE/EU | JSON | high | reference adapter |
| devitjobs | DE/CH/NL/UK | JSON `/api/jobsLight` | high | has a posting-language flag |
| wttj | EU | Algolia | medium-high | public keys rotate; fetched from `/api/env` |
| landingjobs | PT/EU | JSON | medium | company derived from URL |
| remotive, jobicy, himalayas, remoteok, weworkremotely | remote | JSON/RSS | high | region text kept for the filter |
| jobly | FI | HTML via headless browser, JSON-LD detail | medium | Cloudflare; verified live 2026-09-10 through the headless browser (~10 s for 5 postings) |
| bayt | AE | HTML | low | cookie handshake; may need a headless browser |
| eures | EU | JSON | off | huge; enable with a narrow query if wanted |
| ~~microsoft~~ | | | | removed 2026-09-10: careers moved to Eightfold (`apply.careers.microsoft.com`); now an `ats_boards` entry |
| Skipped | | | | finn.no (Norwegian, client-rendered), Indeed direct, relocate.me, meetfrank |

## Pipeline details
- **Normalized `Job`** (`models.py`): source, source_id, url, title, company, description (plain
  text), location_raw, country (ISO2), city, remote (remote/hybrid/onsite/unknown), remote_region
  (free text), seniority_raw, employment_type, salary_text, tags, posted_at, raw payload.
- **Store** (`store.py`): `jobs` (first/last seen), `filter_results`, `ai_verdicts`, `runs`.
- **Rules** (`filters/rules.py`): role gate on title (broad regex list, description gets one chance);
  seniority by title terms + "N years" extraction near experience words; posting language via
  lingua; language requirements via sentence classification (required vs plus); location tiers;
  remote-region classification; cross-source dedupe by (company, title) preferring direct boards.
- **AI** (`ai/`): one job per request, `messages.parse` with a Pydantic schema, adaptive
  thinking, effort low/high, cached system prompt with the profile (+ CV for ranking).
  Refusals or parse failures keep the job with score 50 for manual review.
- **Report** (`report.py`): ranked section (score, why apply, concerns, work-rights note),
  prefilter table, rule-review table; JSONL export.

## Verification checklist (on the owner's machine)
1. `uv sync --extra dev && uv run pytest -q`
2. `uv run jobscraper probe` → note ✗ sources; fix or disable.
3. `uv run jobscraper scrape` (first full pull; LinkedIn/Indeed are slow).
4. `uv run jobscraper filter` → skim the drop-reason table; loosen rules that drop good jobs.
5. `uv run jobscraper prefilter --max-jobs 30` → check Sonnet's calls against your own judgement,
   then run it fully. `uv run jobscraper rank --top 20` → same for Opus.
6. `uv run jobscraper report` → read `data/reports/…md`. Adjust prompts, bump `PROMPT_VERSION`.

## TODO on the owner's machine (added 2026-09-10, nothing here can run in the sandbox)
Launch from the desktop; tick items off here (or delete the section) as they are done.

- [x] **Install the browser once** — done 2026-09-09; `tests/test_browser.py` + `tests/test_web_browser.py`
  run for real (47 passed 2026-09-10).
- [x] **Probe the new sources** — done 2026-09-10. `ats_boards`: 10 of the 13 enterprise boards
  answer (counts in `config/sources.yaml`); Siemens (Avature, 6 per page) and Delivery Hero
  (Attrax) dropped, Ericsson fixed to its Eightfold site. `microsoft`: the old endpoint is gone
  (careers moved to Eightfold), adapter removed, replaced by an `ats_boards` entry. `jobly`:
  works as written, enabled.
- [ ] **One real batch run**: `uv run jobscraper prefilter --batch` on a small DB (or
  `--max-jobs 20`) to confirm the Message Batches wire format is accepted; then decide whether
  to set `ai.prefilter_batch: true` in `config/profile.yaml`. Needs `ANTHROPIC_API_KEY` in the
  environment or `.env` — not set on this machine as of 2026-09-10 (the AI stages have run
  through subagents via `scripts/ai_batches.py` so far).
- [x] **Check the diff output** — verified 2026-09-10: `jobscraper diff` between reports #1 and
  #2 listed 4 new / 4 dropped ranked jobs, and `jobscraper report` (#3) wrote
  `results/diff-2026-09-10.md` plus the "New since report #2" section.
- [ ] **Schedule it**: `scripts/scheduled_run.sh` is a cron/bash script; on this Windows machine
  it needs Task Scheduler running it through Git Bash, or the self-hosted-runner route with
  the repository variable `SCHEDULED_RUNS=true` for `.github/workflows/scheduled-run.yml`.
  See docs/SCHEDULING.md. Also decide which profiles the schedule covers (`--profile felipe`
  runs are separate).
- [x] **Browser smoke tests** — see the first item.
- [ ] **Location-aware board filtering**: Microsoft/Ericsson list every job worldwide and the
  title filter still leaves ~350/~200 postings whose descriptions are fetched lazily even
  though most are in the US/India and the rule filter drops them afterwards. A per-board
  `countries:` filter applied before the detail fetch would cut that. This got a lot more
  valuable on 2026-09-10: `ats_boards` now has 81 boards, and the probe samples of the big
  ones came back from Kuala Lumpur, Bengaluru, Portage MI and Petaling Jaya.
- [ ] **Time one full `ats_boards` run** and decide whether it still fits the schedule. Probing
  the 63 new boards one at a time took ~4 minutes of listing in total, but JPMorgan alone is
  78 s / 7 379 postings and Hitachi 20 s / 4 246, and the descriptions of the title survivors
  are fetched on top of that in a real run. If it is too slow, split the source in two
  (`ats_boards` + `ats_boards_enterprise`) or add the `countries:` filter above first.
- [ ] **Decide on the big-tech boards** (`ats: google|amazon|tesla|tiktok|apple`). The dataset
  ranks Google at 204 tech postings in our countries, TikTok 110, Amazon 64, Tesla 53 — worth
  having, but they list 10 000+ jobs each and mostly cannot fetch a description per posting, so
  enable one at a time and watch the first run. They are commented out in `config/sources.yaml`.

- [x] **Felipe: incremental round done 2026-09-10** — 7,635 new jobs from 81 boards + jobly,
  1,595 rule survivors screened by Sonnet subagents, 37 newly ranked by Opus, report #8
  (97 ranked) published as `data/profiles/felipe/serve/full-2026-09-10b.db`. The batch API is
  irrelevant while the AI stages run on the subscription through subagents (it is API-key
  billing only). Recipe for the next round: scrape → filter → `ai_batches.py export prefilter`
  (only unscreened jobs) → Sonnet subagents → `import prefilter` → `export rank --top 90` →
  Opus subagents → `import rank` → `report` → `publish`.
- [x] **Rule-filter gap closed 2026-09-10**: `guess_country` now reads the trailing ISO-2 token
  ("Hyderabad, TS, IN" → IN, "Utrecht, NL, 3584 AB" → NL), US state and Canadian province
  codes and spelled-out state names, non-European country names, and ~50 more non-European
  cities; boards that hire in one country only may declare `country: XX` in
  `config/sources.yaml` (12 of them do), which fills in what the location string does not say.
  Measured against the run of 2026-09-09 (16 788 jobs): **3 630 had no country, the new parsing
  resolves 3 320 of them, and 2 279 of those are outside Europe** — postings the rules now drop
  themselves instead of paying Sonnet to reject them. The 310 left are not in the string:
  Workday's "N Locations" rollups (135; those tenants send no locations list in the raw
  payload, only an `externalPath` slug like `/job/Shanghai-Shanghai-China/…`, which is the
  next thing to try), plus "Anywhere" / "Remote" / "EMEA" and empty locations. Rule semantics
  are unchanged for a still-unknown country (still permissive); `RULES_VERSION` is bumped so
  the next `jobscraper filter` re-evaluates.
- [x] **GMV (Cornerstone) descriptions are a page-loader stylesheet**, and imec's are empty
  (done 2026-09-10). What Cornerstone actually serves: the listing endpoint
  (`rec-job-search/external/jobs`) returns `externalDescription` with the HTML tags *already
  stripped*, so GMV — which pastes a whole HTML page into that field — leaks the text of its
  `<style>` blocks as prose (~7 000 chars of the WalkMe stylesheet, the advert at the end).
  Fixed in two parts: `sources/_common.clean_description` stores `None` for a body that is
  mostly CSS rule blocks or has under 40 letters, and `sources/_cornerstone.py` re-reads those
  postings from the career site's own requisition service (career-site page → JWT →
  `GET /services/x/job-requisition/v2/requisitions/<id>`), which still serves the original
  HTML and strips cleanly, English culture first. Live before → after: GMV 15 161 chars of CSS
  → 2 045 chars of English prose, imec 3 → 0 (it publishes `"..."` everywhere, in the listing
  and the requisition service alike, so that board is `lazy_descriptions: false`), OHB 575 and
  Henkel 1 337 unchanged (clean already, so neither pays for a request — Henkel's requisition
  service answers 403 anyway). Cost: one extra request per GMV posting (~130 per run).

## Private data (added 2026-09-10)
The GitHub repository is public, so nothing personal is tracked in it: `config/profile.yaml` (the
CV) is gitignored and `config/profile.example.yaml` is the committed template; `results/`,
`data/labels/`, `data/exports/ai/` and the web UI's `decisions.db` files were never tracked.
All of it is mirrored by `scripts/sync_private.sh` into a clone of the private repository
`Tonttu84/jobScraper-private` (default location `../jobScraper-private`, override with
`JOBSCRAPER_PRIVATE_DIR`), one commit per sync tagged with the public repo's commit hash, so
every run's report and verdicts stay debuggable without being public. `scheduled_run.sh` pushes
at the end of each run; `sync_private.sh restore` copies the config back on a fresh clone (the
self-hosted workflow does this). The public history was rewritten on 2026-09-10 to purge the
profile from every earlier commit.

## Measuring the business logic (added 2026-09-10)
The engineering side is quantified (test count, coverage gate, funnel counts per report). The
*correctness* of the rules and the ranking was not, so these are being added, in order:
1. **Drop audit** — `jobscraper audit-drops` samples rule-dropped jobs per drop reason for hand
   labelling and `--score` turns the labels into a per-reason false-negative rate (how many good
   jobs the rules kill). Labels live in `data/labels/` (private repo).
2. **Ranking stability** — `scripts/ai_batches.py stability export|compare` scores the same top-N
   jobs twice in different chunkings and reports score drift, rank correlation and top-10 overlap.
   Decides whether the explicit scoring-weights lever is worth building.
3. **Coverage gate to 97%** (adapter error branches).
4. - [ ] **Labelled evaluation set** — the owner records applied / skipped decisions in the web UI
   while applying (the `decisions` table). Once there are a few dozen, add `jobscraper eval`:
   Sonnet screen precision/recall and Opus precision@10/20 against those decisions, and reuse
   the best cases as prompt anchors (lever 2 under "Improving match quality"). Every rule or
   prompt change then carries a before/after number in its commit message.
5. - [ ] **README as the showcase** — deliberately postponed until the project is stable, so the
   README is not rewritten around features that then change. Until then the README stays a
   working manual (setup, commands, what the AI stages need). The showcase version adds: funnel
   table, the eval numbers from item 4, cost per run, an architecture diagram, and a sample
   report generated from an anonymised profile.

## Improving match quality (agreed levers, not yet built)
The CV is a thin signal. Three additions, in order of expected payoff:

1. **AI-only dossier.** A `dossier` section in `config/profile.yaml`, used only by the Opus
   ranking stage. It holds what a CV never has room for: each project's real size and the
   owner's role in it, tools by actual proficiency, domains liked and disliked, deal-breakers
   (on-call, travel, consultancy placements, B2B contracts), salary floor, preferred company
   size, what "senior" means to him, whether a mid-level title is acceptable for the right
   stack. Produced from a ~30-question questionnaire answered in prose, then condensed.
2. **The owner's own verdicts as anchors.** After 20-30 decisions in the web UI, a handful of
   "applied" and "skipped" cases with one line of reasoning each go into the ranking prompt as
   worked examples. Strongest calibration available; also removes chunk-to-chunk variance,
   since every batch sees the same anchors.
3. **Explicit scoring weights.** A small rubric in the profile (stack fit, domain, level,
   location, pay, company type, each with a weight) so the score is composed the same way in
   every batch instead of each call inventing its own balance.

Deal-breakers are the one part of the dossier that also goes into the Sonnet screen: a clear
match is rejected there (cheap, never reaches ranking), and the ranker still sees the list so a
borderline match that slips through is capped instead of graded as a great fit. Implemented as
`deal_breakers` in `config/profile.yaml`.

Trigger: whenever a new CV arrives, offer these (CLAUDE.md → "When the owner hands over a new CV").

## Later ideas
- ~~Scheduled runs (cron / GitHub Actions on a self-hosted runner) with a diff of new top jobs.~~
  Done: `jobscraper report` diffs itself against the previous snapshot in the same database and
  writes `results/diff-*.md`, `jobscraper diff` shows any two snapshots, and
  `scripts/scheduled_run.sh` + `.github/workflows/scheduled-run.yml` are the two schedules —
  see docs/SCHEDULING.md.
- ~~Message Batches API for the prefilter (50% cheaper, async).~~ Done: `jobscraper prefilter --batch`
  (or `ai.prefilter_batch: true`) submits the pending jobs as one Message Batch and polls until it
  ends; submitted batch ids are stored, so an interrupted wait is resumed instead of paid twice.
- ~~Headless-browser fallback (Playwright is already a skill of yours) for Cloudflare sites.~~
  Done: `browser.py` wraps a persistent headless Chromium (profile in `data/browser/`, so the
  clearance cookie survives), `SourceContext.page(url, mode=…)` falls back to it on 403/503 or
  a challenge page, `http.browser: auto|never` in `config/sources.yaml` is the switch, and
  `jobly` is the first source that uses it.
- Auth module for the web UI, for when it is shared with other students (today it is
  handle-only, see docs/WEB.md). Not urgent; the spec depends on later decisions (who hosts
  it, one DB per person or shared, whether profiles become per-user).
- ~~Track applications (applied / rejected / interview) in the same DB.~~ Done: `jobscraper serve`
  is a small FastAPI UI over the stored reports with per-user decisions and facet filters
  (languages, stack, "benefits from Full Stack Open") — see docs/WEB.md.
