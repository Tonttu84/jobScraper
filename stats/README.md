# jobScraper run statistics

Counts only. One row per `jobscraper report`, per candidate profile: how many postings came in,
how many the rule filter kept, prefiltered and ranked, and what the AI stages cost. No job title,
company, URL or any other text from a posting is recorded here — this file is public, while the
reports it counts stay private.

Columns: **jobs** in the report's window, **new** since the previous report, **keep/review/drop**
from the rule filter, **prefiltered/passed** from the Sonnet prefilter stage, **ranked** by Opus, **cost**
in USD (0 when the AI stages ran through subagents), and the rule and prompt versions behind them.

Written by `jobscraper report`; regenerate from `stats/runs.jsonl` with `jobscraper stats --public`.

## default

| date | report | jobs | new | keep | review | drop | prefiltered | passed | ranked | cost | rules | prompt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2026-09-10 | #5 | 7,881 | 0 | 1,087 | 200 | 6,594 | 2,298 | 1,194 | 80 | $0.00 | 2026-09-10b | 2026-09-09.1 |
| 2026-09-10 | #4 | 7,881 | 0 | 1,087 | 200 | 6,594 | 2,298 | 1,194 | 80 | $0.00 | 2026-09-10b | 2026-09-09.1 |

## felipe

| date | report | jobs | new | keep | review | drop | prefiltered | passed | ranked | cost | rules | prompt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2026-09-10 | #9 | 16,788 | 0 | 4,221 | 1,502 | 11,065 | 5,901 | 1,644 | 97 | $0.00 | 2026-09-10b | 2026-09-09.1 |

## Rule drops by category

Primary drop reason of each profile's newest run.

| profile | category | drops |
|---|---|---:|
| default | level_excluded | 1,848 |
| default | not_software_title | 1,578 |
| default | seniority_label | 1,331 |
| default | duplicate | 444 |
| default | non_software_title_hint | 418 |
| default | posting_language | 283 |
| default | too_old | 241 |
| default | years_required | 222 |
| default | language_required | 138 |
| default | location | 91 |
| felipe | duplicate | 2,690 |
| felipe | too_old | 1,946 |
| felipe | posting_language | 1,840 |
| felipe | not_software_title | 1,754 |
| felipe | location | 1,346 |
| felipe | language_required | 821 |
| felipe | non_software_title_hint | 369 |
| felipe | level_excluded | 221 |
| felipe | seniority_label | 78 |
