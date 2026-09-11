# jobScraper run statistics

Counts only. One row per `jobscraper report`, per candidate profile: how many postings came in,
how many the rule filter kept, prefiltered and ranked, and what the AI stages cost. No job title,
company, URL or any other text from a posting is recorded here — this file is public, while the
reports it counts stay private.

Columns: **jobs** in the report's window, **new** since the previous report, **keep/review/drop**
from the rule filter, **prefiltered/passed** from the Sonnet prefilter stage, **ranked** by Opus,
**refine Δ** the points added to every refine score to read it on the rank stage's scale (blank
when the refine pass did not run), **cost** in USD (0 when the AI stages ran through subagents),
and the rule and prompt versions behind them.

Written by `jobscraper report`; regenerate from `stats/runs.jsonl` with `jobscraper stats --public`.

## default

| date | report | jobs | new | keep | review | drop | prefiltered | passed | ranked | refine Δ | cost | rules | prompt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2026-09-11 | #9 | 11,878 | 0 | 1,691 | 436 | 9,751 | 3,177 | 1,630 | 137 | +22.0 | $0.00 | 2026-09-10d | 2026-09-09.1 |
| 2026-09-10 | #8 | 11,878 | 0 | 1,691 | 436 | 9,751 | 3,177 | 1,630 | 137 |  | $0.00 | 2026-09-10d | 2026-09-09.1 |
| 2026-09-10 | #7 | 11,878 | 0 | 1,699 | 440 | 9,739 | 3,177 | 1,630 | 139 |  | $0.00 | 2026-09-10b | 2026-09-09.1 |
| 2026-09-10 | #6 | 11,878 | 3,997 | 1,699 | 440 | 9,739 | 2,298 | 1,194 | 78 |  | $0.00 | 2026-09-10b | 2026-09-09.1 |
| 2026-09-10 | #5 | 7,881 | 0 | 1,087 | 200 | 6,594 | 2,298 | 1,194 | 80 |  | $0.00 | 2026-09-10b | 2026-09-09.1 |
| 2026-09-10 | #4 | 7,881 | 0 | 1,087 | 200 | 6,594 | 2,298 | 1,194 | 80 |  | $0.00 | 2026-09-10b | 2026-09-09.1 |

## felipe

| date | report | jobs | new | keep | review | drop | prefiltered | passed | ranked | refine Δ | cost | rules | prompt |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2026-09-11 | #13 | 16,891 | 0 | 4,447 | 751 | 11,693 | 5,937 | 1,666 | 189 | +16.4 | $0.00 | 2026-09-10d | 2026-09-09.1 |
| 2026-09-11 | #12 | 16,891 | 0 | 4,447 | 751 | 11,693 | 5,937 | 1,666 | 99 | +18.0 | $0.00 | 2026-09-10d | 2026-09-09.1 |
| 2026-09-10 | #11 | 16,891 | 0 | 4,447 | 751 | 11,693 | 5,937 | 1,666 | 99 |  | $0.00 | 2026-09-10d | 2026-09-09.1 |
| 2026-09-10 | #10 | 16,891 | 103 | 4,448 | 753 | 11,690 | 5,937 | 1,666 | 99 |  | $0.00 | 2026-09-10b | 2026-09-09.1 |
| 2026-09-10 | #9 | 16,788 | 0 | 4,221 | 1,502 | 11,065 | 5,901 | 1,644 | 97 |  | $0.00 | 2026-09-10b | 2026-09-09.1 |

## Rule drops by category

Primary drop reason of each profile's newest run.

| profile | category | drops |
|---|---|---:|
| default | not_software_title | 2,187 |
| default | level_excluded | 1,966 |
| default | seniority_label | 1,326 |
| default | duplicate | 1,037 |
| default | too_old | 994 |
| default | years_required | 648 |
| default | posting_language | 635 |
| default | non_software_title_hint | 560 |
| default | language_required | 248 |
| default | location | 144 |
| default | deadline_passed | 6 |
| felipe | duplicate | 2,732 |
| felipe | location | 2,187 |
| felipe | posting_language | 1,853 |
| felipe | not_software_title | 1,754 |
| felipe | too_old | 1,661 |
| felipe | language_required | 837 |
| felipe | non_software_title_hint | 369 |
| felipe | level_excluded | 221 |
| felipe | seniority_label | 78 |
| felipe | deadline_passed | 1 |
