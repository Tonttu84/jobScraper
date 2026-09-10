# config/profiles/

One directory per candidate. Everything in here except this README is gitignored: a profile
holds a real person's CV text and preferences, so it must never be pushed by accident.

```
config/profiles/<name>/profile.yaml    # candidate, filter policies, prompt wording (required)
config/profiles/<name>/sources.yaml    # which boards to scrape and with what options (required)
```

Create one from the defaults and edit it:

```bash
jobscraper profile-init ana        # copies config/profile.yaml + config/sources.yaml here
jobscraper profiles                # list the profiles that exist (* = the active one)
jobscraper --profile ana run       # everything for that candidate
```

With `--profile ana` (or `JOBSCRAPER_PROFILE=ana`) the whole run moves:

| default            | with `--profile ana`        |
| ------------------ | --------------------------- |
| `config/`          | `config/profiles/ana/`      |
| `data/`            | `data/profiles/ana/`        |
| `results/`         | `results/ana/`              |

`data/profiles/ana/` gets its own `runs/`, `serve/`, `exports/` and `cache/`, so two candidates
never share a database, a published copy or a report.

## `web.preset`: who the served page is for

`profile.yaml` decides how `jobscraper serve` presents the page to whoever opens it. The default
`web.preset: student` is the page shared with fellow students: it offers every language seen in
the postings plus the ones the profile speaks, ticks only English (each viewer picks their own
and the choice is remembered in their browser), and shows the "I have done Full Stack Open" box
so a viewer who has not done it can drop the postings that expect web work. `web.preset:
tailored` is a one-person search: the only language boxes are the ones this profile speaks
(`languages.ok`), they all start ticked, and the Full Stack Open box is gone — it is a fact
about one candidate, not a question worth asking them. Everything else — the stack chips,
sections, countries, scores, decisions — is the same on both.

```yaml
web:
  preset: tailored     # or "student" (the default)
```
