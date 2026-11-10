# Decisions

One row per decision. Never rewrite an old row: a reversal is a new row that names the one it replaces.
Decisions from before 2026-11-10 are in `docs/PLAN.md` ("Decisions (2026-09-07)" and the dated sections after it).

| ID | Decision | Reason | Chosen against |
| --- | --- | --- | --- |
| D1 | The project follows the owner's shared working agreements (Tonttu84/ai-workflow) as a **solo** project: `AGENTS.md` is the rulebook, `CLAUDE.md` is `@AGENTS.md`, commits go to `main` and are pushed once `scripts/check.sh` is green. The old `CLAUDE.md` rules that are still true moved into `AGENTS.md`; the delegation rule (`model: "opus"` on every subagent) is dropped, since the shared rules already default subagents to Opus. Owner's choice on 2026-11-10 (migration of all active projects). | One way of working in every project. | Keeping a project-only rulebook. |
| D2 | `./scripts/setup.sh` sets up a fresh clone: `git config core.hooksPath .githooks`, then `uv sync --extra dev`. `.gitattributes` keeps `*.sh` and `.githooks/*` on LF. Agent's choice during the migration. | The project had no setup command besides the README's `uv sync`, and a clone does not copy `core.hooksPath`. Git for Windows' `core.autocrlf=true` would check the bash hooks out with CRLF, which breaks them. | A Makefile (not otherwise used here); only a README line. |
| D3 | The local gate before every push is `./scripts/check.sh` (ruff, the full pytest run with the browser suites, coverage at least 99 %), with the `pre-push` hook checking the outgoing commit messages. The owner's 2026-09-11 rule that data, stats and docs commits need no check is replaced by the shared rule (the gate before every push) until the owner says otherwise. Agent's reading of the shared rules during the migration. | The shared rules run the full gate before every push and the old project rule did not say it overrides them. | Keeping the exemption for data, stats and docs commits. |
