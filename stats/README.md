# jobScraper run statistics

Counts only. One row per `jobscraper report`, per candidate profile: how many postings came in,
how many the rule filter kept, screened and ranked, and what the AI stages cost. No job title,
company, URL or any other text from a posting is recorded here — this file is public, while the
reports it counts stay private.

Columns: **jobs** in the report's window, **new** since the previous report, **keep/review/drop**
from the rule filter, **screened/passed** from the Sonnet prefilter, **ranked** by Opus, **cost**
in USD (0 when the AI stages ran through subagents), and the rule and prompt versions behind them.

Written by `jobscraper report`; regenerate from `stats/runs.jsonl` with `jobscraper stats --public`.

No runs recorded yet.
