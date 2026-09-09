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
