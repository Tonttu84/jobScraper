# Web UI and structured results

`jobscraper serve` starts a small FastAPI app over `data/jobs.db`. It shows the latest
report (ranked → prefilter survivors → rule-review) and lets each user mark jobs as
**applied / skipped / interested / interview / rejected / offer**. Several students can share one
instance: each picks a handle in the UI and decisions are stored per (job, user).

There is no authentication. Keep it on localhost or behind a reverse proxy / tunnel with
basic auth when sharing it.

## Data model additions

| table | purpose |
|---|---|
| `job_facets` | deterministic filter attributes per job: posting language, required/optional languages, stack tags, `web_dev` (the posting expects web development, i.e. what Full Stack Open prepares you for). Computed by `filter`/`report`, no AI. |
| `reports` / `report_items` | one row per `jobscraper report` run: counts, cost, prompt version, and the ordered items of each section (`ranked`, `prefilter`, `review`). |
| `decisions` | `(job_id, user) → status, note, updated_at`. |

Stack tags (`facets.STACKS`): `web c_cpp python go rust java dotnet mobile embedded devops data game qa php ruby`.
`web_dev` is true when `web` is among the tags.

## API

All list endpoints return JSON. Query parameters that take lists accept comma-separated values
or repeated parameters.

| method + path | purpose |
|---|---|
| `GET /api/meta` | latest report meta, facet values with counts (posting languages, required languages, stacks, countries, remote kinds, sources, sections), known users, decision statuses |
| `GET /api/jobs` | list `JobView`s (see below) from a report, filtered and sorted by score desc. Params: `report_id` (default latest; if the DB has no report yet the view is built on the fly from the current state), `section`, `langs` (languages the viewer speaks; a job passes when its posting language is unknown or spoken, and every required language is spoken), `stack` (any-of), `web_dev` (`true`/`false`; the UI sends `false` only, when the viewer says they have not done Full Stack Open, and omits it otherwise), `country`, `remote`, `source`, `min_score`, `q` (title/company substring), `user` (whose decisions to attach), `decision` (`none` or a status; requires `user`), `limit` (default 200), `offset`. Response `{ "total": n, "items": [...] }` |
| `GET /api/jobs/{id}` | one `JobView` including `description`; `?user=` attaches that user's decision |
| `PUT /api/jobs/{id}/decision` | body `{ "user": "...", "status": "applied", "note": "..." }` → stored `Decision` |
| `DELETE /api/jobs/{id}/decision?user=...` | clear a decision → 204 |
| `GET /api/decisions?user=...` | all decisions (one user's when given) |
| `GET /api/reports` | report history (no items) |
| `GET /` | the single-page UI |

`JobView` fields: `id title company url source country city location_raw remote remote_region
posted_at employment_type salary_text tags score section position`, plus nested `filter`
(`status location_tier reasons signals`), `prefilter` (`score relevant summary concerns`),
`rank` (`score summary concerns why_apply`), `facets` (`posting_language languages_required
languages_optional stacks web_dev`), `decision` (`status note updated_at` or `null`).
`score` is the rank score when ranked, otherwise the prefilter score, otherwise `null`.
