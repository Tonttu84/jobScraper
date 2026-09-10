# Plan

## Goal
Surface the best intern / junior software jobs for Johannes (Helsinki; EN/FI/DE; ~2 years of
project-based coding via Hive Helsinki) with as little manual scrolling as possible:
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
fixtures. **Every source must be verified with `jobscraper probe` on Johannes's machine** before
the first real run; expect a few to need small parser fixes.

## Sources
| Source | Region | Method | Confidence | Notes |
|---|---|---|---|---|
| duunitori | FI | JSON `/api/v1/jobentries` | medium | Cloudflare seen on robots.txt in Sept 2026; HTML fallback parser included |
| tyomarkkinatori | FI | internal JSON search + detail | medium | detail endpoint 403s after a few hundred calls; capped |
| thehub | Nordics | JSON `/api/v2/jobs` | high | startup jobs, some junior |
| teamtailor | FI/EE/Nordics | `<tenant>.teamtailor.com/jobs.json` / RSS | high | tenant list in sources.yaml; add companies you like |
| ats_boards | any | ats-scrapers (Greenhouse/Lever/Ashby/Workable/…) | high | careers URLs in sources.yaml |
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
| bayt | AE | HTML | low | cookie handshake; may need a headless browser |
| eures | EU | JSON | off | huge; enable with a narrow query if wanted |
| Skipped | | | | jobly.fi (Cloudflare), finn.no (Norwegian, client-rendered), Indeed direct, relocate.me, meetfrank |

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
- Scheduled runs (cron / GitHub Actions on a self-hosted runner) with a diff of new top jobs.
- Message Batches API for the prefilter (50% cheaper, async).
- Headless-browser fallback (Playwright is already a skill of yours) for Cloudflare sites.
- Auth module for the web UI, for when it is shared with other students (today it is
  handle-only, see docs/WEB.md). Not urgent; the spec depends on later decisions (who hosts
  it, one DB per person or shared, whether profiles become per-user).
- ~~Track applications (applied / rejected / interview) in the same DB.~~ Done: `jobscraper serve`
  is a small FastAPI UI over the stored reports with per-user decisions and facet filters
  (languages, stack, "benefits from Full Stack Open") — see docs/WEB.md.
