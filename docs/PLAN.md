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
| valtiolle | FI | HTML `<job-card>` listing + posting page | high | Finnish state employers; Grade Solutions platform, parser shared with kuntarekry (`sources/_grade.py`); overlaps tyomarkkinatori but serves the whole advert with no rate limit, so dedupe prefers it. The JSON-LD `description` is only the lead plus the first section, so the body is assembled from the rendered `<ip-details>` blocks (contact details deliberately dropped); the structured "Etätyö" field decides `remote`. Verified live 2026-09-13 (`?desc=ohjelmisto` 15 hits, `?desc=data` 41 over two pages; 5/5 probed with full descriptions) |
| kuntarekry | FI | HTML `<job-card>` listing + posting page | high | Finnish municipalities, wellbeing counties and their inhouse IT companies; same platform and parser as valtiolle, same tyomarkkinatori overlap and same dedupe preference. Paid promotions below the pagination (`is-promoted`) are skipped. Verified live 2026-09-13 (`?desc=ohjelmisto` 27 hits over two pages, `?desc=data` 187 over eight; 5/5 probed with full descriptions) |
| thehub | Nordics | JSON `/api/v2/jobs` | high | startup jobs, some junior |
| teamtailor | FI/EE/Nordics | `<tenant>.teamtailor.com/jobs.json` / RSS | high | tenant list in sources.yaml; add companies you like. **2026-09-13**: +8 tenants (EE: comodule, milrem, fractory; FI: iqm, qutwo, liikennevirtaoy/Virta; Baltics: keskosenukaidigital, luminorbank), 22 in all |
| ats_boards | any | ats-scrapers (Greenhouse/Lever/Ashby/Workable/…) | high | careers URLs in sources.yaml; custom-domain enterprise sites (Eightfold/Oracle/SuccessFactors) as explicit `ats:` + `slug:` entries; per-board `include`/`exclude` title regexes (source-level `default_include`/`default_exclude`) filter before conversion, and a filtered board with per-posting detail support fetches descriptions lazily for the survivors only. Verified live 2026-09-10: Nokia, SAP, Wärtsilä, Volvo, Bosch, Ubisoft, Spotify, Adyen, N26, Celonis, Ericsson, Microsoft (counts in `sources.yaml`); Siemens (Avature, 6/page) and Delivery Hero (Attrax) not scrapeable with this library. **2026-09-13**: +40 Finnish/Estonian company career sites verified live (30 FI, 10 EE), taking the board list to 121; Bolt, Danske Bank, SEB, Gofore, Futurice, Pactum and Cleveron could not be resolved — see the comment block in `config/sources.yaml` |
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
- **Store** (`store.py`): `jobs` (first/last seen, and the run that last saw them —
  see "Postings a board no longer lists"), `filter_results`, `ai_verdicts`, `runs`.
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
- [ ] **Schedule it** — parked 2026-10-10 (owner): runs happen on request in a Claude session
  (the AI stages go through subagents anyway; no API key on this machine), so no Task Scheduler
  entry for now. `scripts/scheduled_run.sh` falls back to `python -m uv` / the venv's
  `jobscraper` when `uv` is not on PATH. Original note: `scripts/scheduled_run.sh` is a cron/bash script; on this Windows machine
  it needs Task Scheduler running it through Git Bash, or the self-hosted-runner route with
  the repository variable `SCHEDULED_RUNS=true` for `.github/workflows/scheduled-run.yml`.
  See docs/SCHEDULING.md. Also decide which profiles the schedule covers (`--profile felipe`
  runs are separate).
- [x] **Browser smoke tests** — see the first item.
- [x] **Location-aware board filtering** — done 2026-09-10: a board may declare
  `countries: [..]` (ISO-2) in `config/sources.yaml`, and `ats_boards` skips a posting whose
  country is readable and outside the list *before* fetching its description (unknown country
  and remote postings are kept for the rule filter). 47 worldwide boards share the
  `&target_countries` anchor = the profile's tier1+tier2+tier3; keep it in sync with
  `config/profile.yaml`. Workday's "N Locations" rollups now resolve their country from the
  `externalPath` slug (`/job/Shanghai-Shanghai-China/…`). Source-level `default_countries:`
  exists but is unset on purpose. Felipe's `config/profiles/felipe/sources.yaml` has not
  been given a list yet (his tiers differ).
- [x] **Time one full `ats_boards` run** — measured 2026-09-10 on the owner's machine with the
  country filter on: **1 627 s (27 min)** for all 81 boards, 4 012 jobs, 3 997 new to the
  default database. It fits a daily schedule but is the slowest source by far; if it grows,
  split it into `ats_boards` + `ats_boards_enterprise`. The country filter's saving, counted on
  the 29 boards whose log lines survived (the run was logged with `-v` through a PowerShell
  redirect, which wrote UTF-16 and mangled the rest): 663 of 2 578 description fetches skipped
  (26%), 421 of them on JPMorgan alone. Log scrapes with `--log-file` or a plain `>` from bash,
  not `*>` from PowerShell.
  - [x] **Re-time with parallel boards** — done 2026-09-13, see the next item (524 s for 121 boards). Original note: `ats_boards` now fetches
    boards through a thread pool (`workers: 4` in `config/sources.yaml`, `1` = the old
    sequential path) and the polite client throttles per host under a per-host lock, so the
    wall time should approach the slowest board (JPMorgan / Hitachi). Time the next full run
    and record it here; also watch `probe --limit`, which may pause briefly at exit while
    in-flight boards finish.
- [x] **First scrape with the 2026-09-13 sources** — done 2026-09-13 15:13–16:18 UTC. Whole `scrape`:
  **3 897 s (65 min)** for 26 sources run one after another; `ats_boards` alone **524 s (8.7 min)**
  for all 121 boards with `workers: 4` (was 1 627 s for 81 boards sequential), 4 188 jobs, 364 new.
  Every board answered, none errored. The slow ones are now `linkedin` 1 059 s, `thehub` 343 s,
  `sapoemprego` 243 s, `justjoin` 205 s — the sources themselves run sequentially, so the next
  timing win is a pool across sources, not inside one. Totals: 16 044 jobs in the DB, 4 166 new;
  `filter` → keep 2 451 / review 610 / drop 11 234; 1 033 survivors unscreened → Sonnet. Log:
  `data/scrape-2026-09-13.log` (15 MB with `-v`; the DEBUG httpcore lines are 78 000 of them).
  Round finished the same evening: 11 + 1 Sonnet chunks (hydration cleared 64 verdicts), 3 rank
  windows to the 90 budget (stop: patience), refine 10 new vs 12 anchored, report #10 (216
  ranked, 90 new), published `full-2026-09-13.db`. `audit-prior` after it (236 rank verdicts):
  prior ordering puts 10/21 of the effective top in its first 60 vs 6/21 for the screen
  ordering, median position 62 vs 117, Spearman +0.12 vs +0.07 — the prior is the better queue,
  with the selection-bias caveat the command prints. Two things the rankers noticed (both fixed
  2026-10-10, see "Built 2026-10-10"): the years-of-experience extractor read 1 from a "minimum 6 years"
  posting, and cross-source duplicates with different company strings (VOIS/Vodafone,
  Academic Work/Academic Work Finland Oy) each cost two Opus verdicts.
  Original note: `valtiolle`, `kuntarekry`,
  40 more `ats_boards` (121 in all) and 8 more Teamtailor tenants (22). Time the run — the last
  full `ats_boards` pass was 27 min for 81 boards, sequential; with `workers: 4` and 121 boards
  the wall time is the open question. Watch `-v` for boards that error: a board that 500s marks
  its postings as missed under `complete_listing`, so a flaky new entry is the first thing to
  disable. Then `filter`, and `jobscraper audit-prior` once the next rank round adds verdicts.
- [ ] **Decide on the big-tech boards** (`ats: google|amazon|tesla|tiktok|apple`). The dataset
  ranks Google at 204 tech postings in our countries, TikTok 110, Amazon 64, Tesla 53 — worth
  having, but they list 10 000+ jobs each and mostly cannot fetch a description per posting, so
  enable one at a time and watch the first run. They are commented out in `config/sources.yaml`.
  **Probed 2026-10-10** (`scripts/probe_boards.py` on a temporary config, title filter only, the
  country skip not applied):

  | board | listed | title survivors | time | what the adapter reads |
  |---|---|---|---|---|
  | google | 3 224 | 1 438 | 84 s | no location at all (country/city/location_raw empty) — the country skip cannot trim it, so all 1 438 would reach the rule filter as "unknown country" and most of them Sonnet |
  | amazon | 34 576 | 10 714 | 36 s | `location_raw` is an ISO-3 code ("SGP"); `guess_country` reads ISO-2 only, so the skip is blind until ISO-3 is mapped — one small change, then it should work |
  | apple | 6 102 | 4 181 | **1 676 s (28 min)** | country parsed ("China"), but ats-scrapers drives it through a third-party stealth Chromium (CloakBrowser, downloaded on first use) — too slow and too much machinery |
  | tesla | 8 365 | 2 885 | 89 s | country and city parsed ("Palo Alto, California" → US), descriptions in the listing — the country skip would work as is |

  Reading: `tesla` can be enabled as it stands; `amazon` after an ISO-3 → ISO-2 mapping in
  `guess_country`; `google` needs a location source first (none in the listing payload); `apple`
  is not worth it. All four carry the description in the listing, so none pays a request per
  posting.
  **Decided 2026-10-10 (owner):** `tesla` and `amazon` enabled the same day; google and apple
  stay out. `guess_country` now reads ISO 3166-1 alpha-3 codes (`sources/_iso3.py`, 249 entries)
  and, in a string of three or more parts, lets a capitalised trailing country code beat the
  name rule — "Atlanta, Georgia, USA" had resolved to GE (the country), which is in the target
  list, 189 Amazon rows' worth. Measured on the live Amazon listing: 10 714 software titles →
  **1 017 in region** after the skip (0 unknown-country rows; 144 IE, 9 remote US). **Watch the
  first run:** Tesla's 2 885 software titles minus the country skip is the open number.

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
   - [ ] **Label the first sample** (owner; skipped for now, 2026-10-10): `data/labels/drop-audit-2026-09-10.jsonl` holds
     150 rows, 15 per drop category out of 6 590 drops (seed 1). Set `label` to `good` /
     `bad` / `unsure` per row (a `note` is optional), then
     `jobscraper audit-drops --score drop-audit-2026-09-10.jsonl` prints the per-category
     false-negative rate and the estimated number of good jobs lost per run. Paste the
     one-line summary into the commit that records it. Note: `location` is the primary reason
     for only 91 drops because title reasons come first; the location rule itself fires far
     more often as a secondary reason.
2. **Ranking stability** — `scripts/ai_batches.py stability export|compare` scores the same top-N
   jobs twice in different chunkings and reports score drift, rank correlation and top-10 overlap.
   Decides whether the explicit scoring-weights lever is worth building.
   **Measured 2026-09-10** (top 30 of report #… on the owner's DB, Opus subagents, two runs
   with different order and chunking; full table in the private repo under
   `data/exports/ai/stability/summary.md`):

   | metric | value |
   |---|---|
   | median \|A−B\| | 3 points |
   | 90th percentile \|A−B\| | 8.3 points |
   | Spearman A vs B | 0.74 |
   | top-10 overlap (Jaccard) | 0.54 |
   | relevant flips | 0 of 30 |
   | re-score vs stored score, mean signed | −5.3 (A), −6.0 (B) |
   | re-score vs stored score, Spearman | 0.45 (A), 0.62 (B) |

   Reading: two fresh scorings of the same job agree to within a few points and never
   disagree on relevance, but the *order* inside the top 30 is noisy (about half the top 10 is
   stable). The stored scores sit 5–6 points above both re-scorings: the ranked list is the top
   30 *selected* out of ~80 by a noisy score, so the winners carry selection bias
   (regression to the mean on re-scoring). Consequences: (a) treat differences under ~5 points
   as ties in the UI/report; (b) a cheap improvement is to re-score the top N once more and
   average, which removes most of the selection bias for the price of N extra calls; (c) the
   scoring-weights lever is worth building, since the residual noise is in how each call
   balances the criteria, not in the hard gates.
   - [ ] **Refine stage** (agreed 2026-09-10, next coding task): a second pass over the top N
     (20–40) in ONE call — all shortlisted postings plus the profile in a single request,
     asked for an ordering and a score per job — so the shortlist is ranked against itself
     instead of in 15-job chunks. Model configurable (`ai.refine_model`); **default
     `claude-fable-5-1`** (owner's call 2026-09-10: start with the better model, optimize cost
     later). Through a subagent (`model: "fable"`) it is free on the subscription; via the API a
     few dollars per run. API notes for Fable: thinking is always on (omit the `thinking`
     parameter, steer with `output_config.effort`), no assistant prefill, structured output
     via `messages.parse` works as for Opus, and a `refusal` stop reason must be handled
     (keep the job with score 50 for manual review, as the other stages do).
     Stored as its own stage (`refine`) and combined with the rank score by averaging;
     override only once the owner's decisions show the refine model is measurably better.
     Extend `stability compare` so the refine pass is measured the same way.
     **Built and measured 2026-09-10** (Fable, top 20, one call, report #5):

     | metric (same 20 jobs) | value |
     |---|---|
     | Spearman refine vs Opus re-run A / B | 0.74 / 0.74 |
     | Spearman refine vs mean(A, B) | 0.78 |
     | Spearman refine vs stored rank score | 0.62 |
     | Spearman Opus A vs B (reference: Opus vs itself) | 0.84 |
     | top-10 overlap refine vs A / B / stored | 0.54 each |
     | mean score: refine / A / B / stored | 59.5 / 74.1 / 74.3 / 80.6 |

     Reading: Fable's shortlist order agrees with an Opus re-run almost as well as two Opus
     re-runs agree with each other, so it is a second opinion of similar reliability, not a
     copy — where it differs it is opinionated (Pathos Dubai 57 vs Opus ~78, Junior C++
     Engineer 38 vs ~67), and on a much lower scale (mean 60 vs 74). Averaging the two therefore
     lowers the effective numbers by ~10 points but keeps both orderings' information; whether
     Fable's opinion is *better* can only be decided against the owner's applied/skipped
     decisions (item 4).
     **Calibration (2026-09-11).** The level difference is a rater-scale effect, not information:
     over the jobs with both verdicts the rank mean is 80.6 vs refine 58.6 (offset −22.0, sd
     10.1) for the owner and 86.1 vs 68.1 (−18.0) for Felipe, while the Opus-vs-Opus re-score
     control dropped only 5–6 points. Averaging raw scores dragged a strong shortlist into the
     60s (2 of 24 refined jobs kept 80+ against 17 by rank). Decision: shift the refine scores by
     the shortlist's mean offset (jobs with both verdicts, none applied under 3) before averaging,
     clamp to 0–100, keep the raw score, and report the offset per run (report snapshot, stats
     column "refine Δ"). Fable's ordering and spread survive; its level does not. The prompt is
     deliberately unchanged so old and new refine verdicts stay on one raw scale (anchors mix
     them); a "same scale as the first pass" prompt line can come later with a version bump.
     - [ ] **Iterate to a fully refined top N** (owner, 2026-09-11): once refined jobs are
       calibrated they sit lower than rank-only neighbours, so an unrefined job at rank position
       21–30 can enter the effective top 20 on one lucky sample. Select the refine shortlist by
       *effective* score (rank alone for unrefined jobs), refine the unrefined members against
       the refined anchors, recompute, repeat until the effective top N has no unrefined member
       (cap 5 passes). API path: `jobscraper refine` loops itself; subagent path: `export refine`
       until it reports nothing new — each pass is one small Fable call.
       Width (owner, 2026-09-11): the invariant is a fully refined **top 20** (`ai.refine_top_n`);
       only the *first* pass, when nothing is refined yet, takes **25** (`ai.refine_first_pass`),
       because jobs 21–25 are likely to move up once the refined ones regress. Later passes
       refine just the unrefined members of the effective top 20.
     - [x] Built 2026-09-11 (`refine_shortlist`, `ai.refine_first_pass`, `ai.refine_max_passes`,
       tie rule: the cut extends over every job tied with its last member; `reports.refine_offset`).
   - [ ] **Rank until the top stops moving** (owner, 2026-09-11 — "assume it can happen"): the
     rank cut is "top 90 survivors by Sonnet score", but measured over the ranked jobs the Sonnet
     score has **no relation to the Opus score** (Spearman −0.06 owner / 0.15 Felipe, n=152/101),
     and two thirds of the Opus-80+ jobs sat within 5 Sonnet points of the cut. Sonnet works as a
     gate, not a sorter, so the ~1 500 never-ranked survivors probably hide strong jobs at a
     similar rate, and refining can never surface them. In a small pool (Finland only) the miss
     would be visible. Design: rank survivors in chunks of 15 (Sonnet order for want of better)
     and stop when `ai.rank_patience` (30) consecutive newly ranked jobs produced no entrant
     into the effective top 20, or when `ai.rank_budget` (90) calls are spent this round; then the
     refine loop; if refining lowered the 20th score, one more rank window. Small pools rank
     everything, huge pools are bounded. Subagent path: `export rank` becomes one window per
     export and reports whether the stop rule is met. Loop shape (owner):
     `while (top20 not refined) { while (top20 not ranked) [rank]; [refine] }`.
     - [x] Built 2026-09-11: `ai.rank_window` 15, `ai.rank_patience` 30, `ai.rank_budget` 90,
       `ai.rank_top_n` re-documented as the first window's depth; `jobscraper rank` loops windows,
       `run` re-ranks when the refine loop lowered the top-20 boundary; subagent path keeps the
       round's memory in `data/exports/ai/rank/state.json` (`export rank --reset-round` starts a
       new round). First real round (Felipe, 2026-09-11): every window placed at least one
       entrant into the top 20, so the patience rule never fired and the budget (90) stopped it —
       the Sonnet-ordered queue is far from exhausted. Fixed the same day: `export rank` now
       caps the window to the remaining budget (it had exported 30 with 15 left; the budget was
       only enforced at import).
     - [x] A queue order worth reading, 2026-09-12: with ~1 600 survivors and 90 ranked per
       round the *order* is most of the product, and the screen score does not provide one
       (Spearman −0.06 / +0.23 against the Opus score). The queue is now ordered by a free
       lexical prior — BM25 over the posting against the profile's own skills, interests,
       summary and CV, `src/jobscraper/prior.py`, no new dependencies — which scored +0.19 /
       +0.29 on the same stored verdicts. Sonnet is a **gate** only (relevant, ≥
       `ai.prefilter_min_score`); it no longer decides who is read first. `ai.rank_order`
       (`prior` | `screen`) switches every path at once, and `jobscraper audit-prior` compares
       the two orderings on the current database: Spearman against the stored rank score plus
       how deep each one buries the effective top 20. The comparison understates the prior,
       since the only measurable jobs are the ones the screen ordering itself selected.
     - [x] Absolute scale, 2026-09-11: batch-relative scoring corrupts the stop rule — a weak
       window inflates a false entrant into the top 20, a strong one hides real ones — so the rank
       prompt now carries a paragraph forbidding it and every batch is prefixed with three fixed
       reference scores (`report.rank_anchors`, targets 85/65/40, written to `anchors.txt` on the
       subagent path). Verdicts made under `2026-09-09.1` stay current through
       `COMPATIBLE_PROMPT_VERSIONS` by the owner's decision: no re-ranking, the old verdicts age
       out with their jobs.
3. **Coverage gate to 97%** (adapter error branches).
4. - [x] **Labelled evaluation set** — built 2026-10-10 as `jobscraper eval` (see "Built 2026-10-10"
   below); waits for the owner's first decisions. Original note: the owner records applied / skipped decisions in the web UI
   while applying (the `decisions` table). Once there are a few dozen, add `jobscraper eval`:
   Sonnet screen precision/recall and Opus precision@10/20 against those decisions, and reuse
   the best cases as prompt anchors (lever 2 under "Improving match quality"). Every rule or
   prompt change then carries a before/after number in its commit message. The `closed` status
   (the vacancy stopped accepting applications) is a fact about the posting, not a verdict on
   the fit: exclude it from both the positive and the negative side of the eval and from the
   anchors — a strong match that was merely filled must never count as a rejection.
5. - [ ] **Public run statistics** — a committed `stats/runs.jsonl` (one row per report: jobs
   in the DB, new since last run, per-source counts, rule keep/review/drop, drops per reason
   category, screened/passed, ranked, models + prompt versions, tokens and API-list-price cost
   per stage, seconds per stage) written by `jobscraper report`, plus a rendered `stats/README.md`.
   Counts only, so it is safe in the public repo; the profile name may appear (owner's call,
   2026-09-10). Build after the drop-audit command lands (same CLI file).
6. - [ ] **Haiku vs Sonnet for the screen** — shelved (2026-09-10). Sonnet 5 stays; when the
   product is otherwise finished, run one full screen both ways over the same batch and compare
   recall on the labelled set and cost. Haiku 4.5 is half the price, not a tenth, and needs its
   own thinking parameters in `ai/client.py` (no `effort`). Data for the README, not scope.
7. - [ ] **README as the showcase** — deliberately postponed until the project is stable, so the
   README is not rewritten around features that then change. Until then the README stays a
   working manual (setup, commands, what the AI stages need). The showcase version adds: funnel
   table, the eval numbers from item 4, cost per run, an architecture diagram, and a sample
   report generated from an anonymised profile.

## Postings a board no longer lists (2026-09-12)
Nothing in the pipeline knew when a posting had been taken down: jobs were loaded with
`seen_within_days=30`, so a posting stayed a rank candidate and a report entry for 30 days after
its board dropped it. Measured on the owner's default database: `ats_boards` was scraped on 09-07
and again on 09-10, and 427 of its 4 439 postings (10%) did not come back in those three days —
one round of the Opus budget (90 calls) partly spent on jobs nobody can apply to.

The signal was already there, only the link was missing. The `runs` row is now opened *before* the
source is fetched (`store.start_run` / `finish_run`; `log_run` is the two of them together), every
posting the fetch brings back is stamped with that run id (`jobs.last_run_id`), and
`store.mark_missing` counts one miss (`jobs.missed_runs`) against every posting of that source
that kept an older id. Timestamps could not have done this: the run row used to be written *after*
its jobs, so every posting looked older than its own run. `store.gone_ids(n)` reads the counter,
and the jobs are filtered out once, where the pipeline loads them (`_load_state` in `cli.py`, the
report's own load, the export points in `scripts/ai_batches.py`). Nothing is deleted: a gone
posting keeps its row, its verdicts and its history, and the rule filter still sees it — it simply
stops being a candidate. The report says so rather than shrinking quietly ("Left out: N postings no
longer listed on their board", also `counts["gone"]` in the snapshot).

Three guards against calling a posting gone when it is not:
- **A run that failed** finishes with its error and marks nothing.
- **A run that fetched nothing**, or a `--limit`ed one (probe-style caps), marks nothing: a
  partial fetch is not a purge.
- **A source that does not walk its whole listing** is skipped — and that is the default
  (`complete_listing = False`, changed 2026-09-13 the same day it shipped). `linkedin` and `indeed`
  are keyword searches where absence means nothing (525 of 852 LinkedIn rows would have been
  mislabelled), but so, less obviously, is every adapter that runs a fixed query list or stops at
  `max_pages`: a live posting slides past the cap as newer ones arrive. Only `ats_boards`,
  `teamtailor`, `devitjobs`, `remoteok` and `weworkremotely` provably enumerate everything and opt
  in; `tests/test_cli.py` pins that set.

`profile.gone_after_misses` (default 1, 0 turns the mechanism off) is how many consecutive complete
runs of its source must miss a posting before it counts as taken down.

Known limitation: `ats_boards` is one `runs` row covering 81 company boards, so if one board 500s
its postings count as missed although they are not gone. At the default threshold of 1 that means
one flaky board can hide its jobs for a round; they come back the moment the board answers again.
Raise `gone_after_misses` to 2 if that ever bites.

## Rule and prompt candidates raised by the ranking agents (2026-09-10, owner to decide)
- **University-enrolment gate.** German/Romanian "Werkstudent" and mandatory-internship
  postings (Bosch, SAP, Stryker, Deutsche Börse, Hitachi, Allianz, Cisco intern) require
  *current enrolment* at a recognised university with an enrolment certificate. Hive Helsinki
  does not satisfy that. The prompts only say degree requirements are not a blocker, so the
  rankers capped these at 20–48 or rejected them ad hoc; in the 2026-09-10 rank round about a
  third of the 61 new candidates were such postings. **Decided 2026-09-10: keep them in.**
  Finnish employers accept Hive for student positions and Germany/Romania are assumed doable.
  Added as an `extra_rules` line in the profile (both prompts, no prompt-version bump, so
  nothing already scored is invalidated). The ~15 enrolment-gated jobs scored 20–48 on
  2026-09-10 keep those scores until re-scored: delete their rank verdicts and re-export when
  budget allows (one Opus chunk).
- **Evergreen pipeline adverts** — done 2026-09-10 (`filters/evergreen.py`, rules `2026-09-10d`,
  facets `2026-09-10.1`): 17 cue phrases (EN/DE/FI/PT/SV, title first) set `signals["evergreen"]`;
  never a drop. Shown as a concern in the report, a grey tag and a "hide evergreen adverts"
  box in the web UI, passed to the ranking prompt so rank and refine discount it the same way,
  counted in the public stats.
- **Application deadlines in the text** — done 2026-09-10 (`filters/deadline.py`, rules
  `2026-09-10c`): a date within 80 characters of a cue phrase (EN/FI/DE/PT/SV) becomes
  `signals["deadline"]`; a passed deadline is a rule drop (`deadline_passed` in the audit and
  stats), an open one shows as "closes in N days" in the report and the web UI with an amber
  tag under 7 days, and the ranking prompt sees it. Measured read-only before the first
  re-filter: default DB 89 of 11 878 postings carry a deadline (0.7%), 11 already passed, 40
  closing within a week; Felipe's 107 of 16 891 (0.6%), 4 passed, 46 closing within a week.

## Improving match quality (agreed levers; 1 and 3 wired 2026-10-10, content pending)
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

**Status 2026-10-10** (owner: "all improvements before the next run"):
- Lever 1 and 3 are wired: `dossier:` (prose) and `weights:` (criterion → points) in
  `config/profile.yaml`, rendered into the Opus rank prompt after the CV as a DOSSIER block and a
  SCORING RUBRIC block (percent of the sum per criterion); the refine pass inherits them, the Sonnet
  screen never sees them, and an empty field leaves the prompt byte-for-byte as before. No
  `PROMPT_VERSION` bump (same call as for `extra_rules` and `deal_breakers`): old verdicts age out
  with their jobs, new jobs are scored with the dossier.
- The content comes from `docs/DOSSIER-QUESTIONNAIRE.md` (36 questions in 9 groups; section 9 is
  the weights table). Answers go to `config/dossier-answers.md` (gitignored, mirrored by
  `sync_private.sh`) or into the chat, and are condensed into the profile by the main session.
- **2026-10-13: the dossier is in.** 35 of 36 questions answered conversationally (8.2 "what makes
  you skip" is deferred until the owner has skipped a posting in the web UI; one skip so far).
  `dossier:` in `config/profile.yaml` holds ~9.5k characters: timeline, hand-written vs AI-directed
  code with evidence, a SHOWCASE PROJECTS section (the CV carries three lead projects swapped per
  application; the ranker names the three in `why_apply`), level/title stance, location and
  conditions, learning stance (will learn anything; Go and cloud wanted; embedded C near-fit),
  product vs engineering problem, and the gaps to weigh. The draft and its sources stay in
  `config/dossier-notes/` (private). Takes effect at the next rank round, no re-scoring.
- Lever 2 (the owner's verdicts as anchors) needs decisions in the web UI first, so it comes after
  the next run; `jobscraper eval` is the measurement for it.

## Built 2026-10-10, waiting for the next fresh run (owner's call: no re-scoring of old verdicts)
Four changes from the open list, each written by an Opus subagent against tests first; the full
check (ruff + pytest, 99% gate) passed before the commit. Nothing was re-filtered or re-scored on
the stored data — the owner wants one fresh run once the pending features are in.
- **Years-of-experience extractor** (`filters/rules.py`, rules `2026-10-10a`): the first "N years"
  figure next to an experience word wins instead of the smallest. Measured on the stored database:
  206 postings carried `years_required: 1`, 7 of them stating a 3+ year minimum first ("Minimum 5
  years of engineering experience … 1 year of experience with LangSmith"). Known trade-off: a
  per-tool figure written *before* the headline would now win.
- **Dedupe across company spellings** (`dedupe` in `filters/rules.py`): company names are folded to
  ASCII, legal forms (oy, ab, gmbh, oü, a/s, sp. z o.o., …) and a trailing country/region word are
  stripped, `company_aliases:` in `config/sources.yaml` maps one employer's names onto each other
  (shipped with `vois: vodafone`), and under the same title a longer name merges into a two-word-or-
  longer word-prefix of it ("Academic Work Finland Oy" → "Academic Work"; "Polar" / "Polar Squad"
  stay apart). The 2026-09-13 round paid two Opus verdicts each for VOIS/Vodafone and Academic Work.
- **Sources fetched in parallel** (`scrape.workers: 3` in `config/sources.yaml`): `jobscraper
  scrape` submits every source to a thread pool and does all store writes on the main thread as
  fetches finish; a source that may need the headless browser (`uses_browser = True`, today only
  `jobly`) runs afterwards on the main thread, because Playwright objects belong to the thread that
  created them. With `ats_boards` keeping its own `workers: 4` the two multiply (up to 4 + 2 hosts in
  flight; the per-host throttle still holds). Expected effect: the 65-min scrape of 2026-09-13
  should approach its slowest source (`linkedin`, 18 min).
  **Timed 2026-10-12** (first run with the pool, `workers: 3`, plus Tesla and Amazon): whole
  `scrape` **62 min** — no better than before, because `ats_boards` alone took **3 648 s (61
  min)**: Tesla's listing carries no description and ats-scrapers' per-posting fetch launches a
  stealth Chromium (CloakBrowser) for every one of its 384 in-region postings, ~10 s each, the
  last 75 returning empty. Set `lazy_descriptions: false` on Tesla the same day (title + location
  only, like LinkedIn rows). The pool itself worked: the 24 other sources were all done by 28 min
  (`linkedin` 1 486 s, `wttj` 1 705 s counted from submit, so queue wait included), and `jobly`
  (browser) ran after the pool in 80 s. Totals: 24 351 jobs in the DB, 8 307 new (Amazon 682,
  Tesla 384); `filter` over 19 267 jobs seen in 30 days → 1 757 duplicate groups, keep 1 964 /
  review 708 / drop 13 628; 2 068 unscreened survivors → 21 Sonnet chunks; `hydrate linkedin`
  12. Log: `data/scrape-2026-10-12.log` (17 MB with `-v`). Next timing: without Tesla's
  browser, `ats_boards` should be back near its 2026-09-13 figure (524 s) plus Amazon.
  Round finished the same day: 21 Sonnet chunks (2 068 verdicts, 0 bad), 3 rank windows of 30 to
  the 90 budget (stop: budget — every window still placed top-20 entrants, so the 1 148 left in
  the queue are far from exhausted), one Fable refine pass (18 new vs 2 anchored, then "top 20
  fully refined"; refine offset 19.7), **report #11: 124 ranked (90 new), 1 254 prefilter**,
  published `full-2026-10-12.db`, private mirror synced. The top of #11 is RUBINLAKE Frankfurt
  (junior NestJS backend), Property Finder Dubai (graduate trainee), Eaton Vantaa (firmware test
  automation), then four Ericsson entry-level C++/cloud roles. `boost`: 392 LinkedIn rows were
  hydrated across rounds, 71 of them now ranked, the deepest from prefilter position 463.
- **`jobscraper eval`** (`audit.decision_eval`): grades the screen (precision / recall of "relevant
  and score ≥ `ai.prefilter_min_score`") and the ranking (positives in the effective top 10 / 20,
  P@k over the labelled jobs, mean effective score of wanted vs skipped, Spearman) against the web
  UI's decisions. applied / interview / offer / rejected / interested count as wanted, skipped as
  not wanted, `closed` as neither; the latest decision per job wins across users. Reads
  `data/serve/decisions.db` when it exists (`--decisions PATH` otherwise), refuses below
  `--min-decisions` (10), `--json PATH` dumps everything. Prints one summary line for commit
  messages: `eval: 24 decisions — screen P 0.71 / R 0.93, rank P@10 0.60 P@20 0.55`.
- Also: the pipeline tests count from a frozen date (`FIXTURE_TODAY` in `tests/test_cli.py`,
  `rules.today_utc`) — the arbeitnow fixture is from 2026-09-07 and `max_age_days: 20` had started
  dropping it, 15 tests failed on 2026-10-10 for no code reason.

## Pipeline self-observation (agreed 2026-10-13, in progress)
Why: the stages hide their own input problems. Measured on the stored verdicts: 992 of 6,266
screen verdicts judge a title because the description never arrived; rule drops and screen
rejects are never sampled, so `eval` cannot see a false reject (survivorship); the EPAM Junior
C++ posting was re-posted after 35 days and the ranker called it fresh (fixed 2026-10-13: the job
prompt now shows the pipeline's first-seen date). Rank vs refine agree after the 19.5-point
offset (residual sd 9.3; one job over 20), so disagreement is a health line, not a system.

Tasks, one Opus subagent each, sequential (shared files; browser tests fail under contention),
each committed after a green `scripts/check.sh`. **All five landed 2026-10-13** (A a404852,
B 76830af, C 10ddb2e, E 9b27e3b, D below). Follow-up for D: the rule-drop sentinel pool still
includes age/deadline drops, which are right by construction and dilute the estimate; exclude them.
Follow-up found 2026-10-13 while re-scraping: with stdout redirected to a file on Windows,
Python's cp1252 fallback made `console.print("✓ …")` in `_book_fetch` raise UnicodeEncodeError,
which ended the scrape after the first source and discarded the other 24 fetches (their `runs`
rows were marked errored by hand). `scripts/scheduled_run.sh` now exports `PYTHONUTF8=1`; the code
fix (console output must never abort a stage: configure the Console for the stream's encoding or
catch and degrade to ASCII) is a small TDD task still to do.
- **A. Pipeline health**: `run_notes` table + Store API; producers: source count drop vs the
  previous complete run (and the takedown guard: a half-returned listing does not count misses),
  source error, empty-description share per source, borderline language drops, rank/refine
  residual > 20, cross-board duplicates in the ranked section; `## Pipeline health` appendix in
  the report; `diff` names new note kinds.
- **B. AI verdict fields**: `input_notes` on both AI schemas (categories: missing/truncated input,
  a detected signal contradicting the text, an instruction that could not be applied, *profile
  gap* = the verdict hinges on a fact about the candidate the profile does not state; "usually
  empty"); `criteria` sub-scores (one 0-100 per rubric weight) on the rank verdict, stored beside
  the holistic score, which still ranks; consistency note when |score − weighted sum| > 15;
  notes flow into `run_notes`; import validates them on the subagent path.
- **C. Versioning**: `profile_hash` (hash of the rendered system prompt) on every verdict;
  reference anchors restricted to the current hash (first window after a profile change runs
  without anchors); `jobscraper compare-verdicts --old V --new V` (deltas, flips, top-N overlap,
  biggest movers) for comparing prompt versions.
- **E. Skip reasons**: one-tap reasons on the web UI's skip button (wrong stack, level, location,
  consultancy, company, duplicate/applied, posting gone, pay, other+note), stored on the
  decision; `eval` prints their distribution. Answers questionnaire 8.2 continuously and gives
  lever 2 structured anchors.
- **D. Sentinels**: each run, rank ~10 random screen rejects and screen ~10 random rule drops,
  stored under their own stage names so they never enter the shortlist; the report shows the
  false-reject estimate per gate and any sentinel find scoring 70+.
- **Then** (owner 2026-10-13, Claude's call accepted): scrape once more first, so Task A's scrape
  checks meet real boards and descriptions are fresh; then bump `PROMPT_VERSION`, keep
  `COMPATIBLE_PROMPT_VERSIONS` to the new one, and re-run screen → rank → refine through
  subagents. The comparison runs on the intersection of jobs both versions scored. The old
  verdicts stay under the old version; `compare-verdicts` shows what the dossier and the new
  fields changed. Owner 2026-10-13: "don't overwrite the old ones, keep both".

## Run of 2026-10-13: report #12, first run with the dossier and the self-observation stack
Scrape 25 sources, 13,474 fetched, 2,737 new (one aborted attempt first: cp1252 console crash,
see above). Filter: 2,358 keep / 835 review / 17,743 drop. Screen 2,890 (29 Sonnet chunks) +
147 re-screened after LinkedIn hydration; rank 90 in two windows (stop: budget, miss run 0/30 —
the top was still gaining entrants when the budget ran out); refine 28; sentinels 10 + 10.
`compare-verdicts --old 2026-09-11.1 --new 2026-10-13.1`:
- rank, 47 jobs on both sides: mean +10.3 (sd 10.9), Spearman +0.88, top-30 Jaccard 0.82. The
  dossier lifted the scale by about ten points without reordering much; the first window ran
  without anchors, so this is the new scale, not noise around the old one.
- refine, 9 jobs: +3.6, Spearman +0.85. prefilter, 1,844 jobs: −0.35, 43 true→false, 69
  false→true (several French "Stage" internships 0→50; the screen prompt changed only by the
  INPUT NOTES block, so read these as noise plus a few genuine second looks).
- Sentinels: 0/10 rule drops would have passed the screen; 0/10 screen rejects scored 70+.
- Pipeline health: 1,042 missing_input (descriptions truncated or absent: devitjobs 89%, linkedin
  until hydration, justjoin 52%, cvee 50%, tyomarkkinatori 34%), 102 signal_conflict (a cluster:
  postings located in the UK or CH tagged country DE — a source's country default, to fix),
  19 profile_gap (law studies as a legal qualification; whether he is enrolled in a degree
  programme for thesis/internship postings — dossier questions), 16 rubric_mismatch (holistic
  score well under the weighted criteria, mostly where a hard gate like enrolment applies).
Open: one more rank round (`export rank --reset-round`) would rank another 90 of the 1,363-job
queue; the stop rule did not reach patience.

## Tailored CV (started 2026-10-14)
Goal: a CV customised per job, built by *selecting* pre-written parts, never by writing text, so
a hallucinated claim is structurally impossible. The Canva design stays the reference: page 1 is
the CV, page 2 the parts bin the owner drags projects from; `data/cv/` (gitignored) holds the
PDF/PNG exports (`cv-canva-2026-10.*`).
- Decisions (2026-10-14):
  - Where it runs: a separate `tailor` stage after refine, over the effective top 20, cached per
    job and per parts-library fingerprint — not folded into the refine prompt (that would
    invalidate every cached verdict through `PROMPT_VERSION`). The same function behind a button
    in the web UI next to the apply decision and `jobscraper tailor <job_id>`, for rank-only jobs
    the owner applies to anyway. Cost is about one more verdict per job; the unused tailorings for
    skipped jobs are cheap, and the report can show "what the CV would lead with".
  - Parts library `config/cv-parts.yaml` (gitignored, mirrored by `sync_private.sh`; template
    `config/cv-parts.example.yaml`): summaries (one chosen), projects (ordered subset of
    `slots.projects`, each with a bullet subset), work (all shown, bullets selected), fixed header
    and sidebar. `jobscraper.cv.parts` loads, validates, resolves a `Selection` of ids to text and
    renders markdown; `jobscraper cv-parts` prints the default selection (= page 1 today).
  - The model returns ids only; free text (one job-specific sentence, always shown to the owner
    before it lands) is a later, optional step. Rendering to PDF: reproduce the two-column layout
    in HTML/CSS and print with Playwright (already a dependency); Canva's autofill API is
    enterprise-only. Until then the markdown tells the owner what to drag into the Canva slot.
  - Text changes made while building the library (owner to confirm): typos fixed
    (programming, hackathons, competitive, architecture, entrepreneurial); Full Stack Open bullet
    says Zustand instead of Redux and ESLint/Prettier instead of CI/CD (the notes found neither
    Redux nor an own CI pipeline in those repos); Python is still absent from the sidebar
    languages line.
- Canva geometry measured 2026-10-14 (reference for the HTML renderer; both pages follow it):
  project slot = title (Aileron 11.1, X 8.12 cm), lead line (Aileron 11.1, X 8.12 cm, flush with
  the title), bullet box (Aileron 10.1, X 7.93 cm so the bullet marker sits under the title's
  first letter). Section headings Now 14.6; profile paragraph Lato 12; sidebar Lato 10; name
  Now 23; work entries: title bold, lead Aileron 12, dates Aileron 10 right-aligned at ~20.2 cm.
  Page 2 (the parts bin) uses the same X values, so an entry can be dragged into a page-1 slot
  without re-aligning. Lesson from editing Canva through the browser: select a text box, press
  Escape once to leave edit mode, then type into the Position panel's X/Y fields; the panel's
  field positions move when a selection exists, and its values lag one selection behind.
- Done 2026-10-16: `jobscraper tailor <id>` (`jobscraper.cv.tailor`). One request per job; the
  model returns ids plus notes for the owner (`focus`, `rationale`, `gaps` for the cover letter),
  a bad answer is sent back once with its problems, then it is an error. Cached in
  `cv_tailorings` on library fingerprint + `TAILOR_PROMPT_VERSION` + model. The ranker's
  why_apply/concerns ride along in the user prompt. No API key here, so `--export` / `--import`
  is the subagent path (CLAUDE.md, "Tailor my CV for this job"). Web UI: a "Tailor CV" button
  per card (only when the server has a `cv-parts.yaml`) copies the hand-off line for a Claude
  Code chat. Not yet in the pipeline run: tailoring stays on demand until it has proven itself.
- Next: the HTML renderer (two-column layout, Playwright print to PDF), so the hand-off ends in
  a PDF instead of drag instructions for Canva; library review with the owner (libft, so_long,
  the rest of the Hive core curriculum are not parts yet).

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
