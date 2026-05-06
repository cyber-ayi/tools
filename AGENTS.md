# AGENTS.md

Conventions for AI coding agents (Claude Code, Cursor, Copilot Agent,
Gemini CLI, etc.) working in this repository. Following the
[agents.md](https://agents.md) convention.

## Project overview

Two independent Python sub-projects under one repo:

- `backup-verification/` — SD-card ↔ NAS backup verifier
  (Poetry build backend; `pyproject.toml` uses legacy `[tool.poetry]`).
- `rclone-migrate/` — `rclone move` alternative split into
  `copy → check → delete` (PEP 621 `[project]` + setuptools).

Each sub-project has its own README, tests, and entry points.
Cross-project changes are rare and should be in separate PRs.

## Repository layout

```
backup-verification/
  pyproject.toml            # Poetry; do not migrate to PEP 621 ad-hoc — see issue #8
  verify.py, verify_backup.py
  test_verify_backup.py     # tests at top level (not in tests/)
rclone-migrate/
  pyproject.toml            # PEP 621
  rclone_migrate/           # source package
    profiles/*.toml         # bundled hash profiles
  tests/                    # pytest tests
.github/                    # CI; see "Files agents must not modify silently"
ruff.toml                   # repo-wide lint config
LICENSE                     # Apache 2.0
README.md, AGENTS.md, SECURITY.md
```

## Setup commands

### backup-verification

```bash
cd backup-verification
poetry install                      # creates .venv with pytest + pytest-cov
poetry run pytest -v                # run tests
```

If Poetry isn't available locally, `pip install pytest pytest-cov`
is sufficient — there are no runtime deps.

### rclone-migrate

```bash
cd rclone-migrate
pip install -e '.[dev]'             # editable install with dev deps
pytest -v                           # run tests
```

The end-to-end test suite (`tests/test_e2e.py`) auto-skips if the
`rclone` binary is not on `PATH`. CI installs rclone so all tests run
there.

## Running the full quality bar locally

Match what CI enforces, in this order:

```bash
# 1. Lint (from repo root)
pip install ruff==0.6.9
ruff check .

# 2. Tests + coverage (per sub-project)
cd backup-verification && pytest --cov=verify_backup --cov-branch && cd -
cd rclone-migrate     && pytest --cov=rclone_migrate --cov-branch && cd -

# 3. Workflow audit (only if you touched .github/)
pip install zizmor
zizmor --config .github/zizmor.yml --format plain .github/workflows
```

Coverage thresholds (enforced in CI via `[tool.coverage.report]
fail_under`):

| Sub-project | `fail_under` | Direction |
|---|---|---|
| `backup-verification` | 30 | ratchet up as test surface grows |
| `rclone-migrate` | 68 | ratchet up; current ~70 |

Do not lower a threshold to make a PR pass — fix the missing test
coverage instead.

## Code style

`ruff.toml` enables a deliberately narrow rule set (`E9`, `F63`, `F7`,
`F82`) targeting bugs commonly produced by AI agents:

- `F82` — undefined names / undefined exports (hallucinated imports)
- `F63` — invalid comparisons (`is` with literal, etc.)
- `F7` — assert on a non-empty tuple, etc.
- `E9` — actual syntax errors

Style rules (line length, import ordering, unused imports) are
**intentionally not enforced**. Don't add them mid-stream — that's a
separate, scoped initiative.

If you find your change is blocked by ruff, the rule almost certainly
caught a real bug. Fix it.

## CI gates that already exist

You don't need to design these — they're already wired up:

- `ci.yml` — lint + matrix tests (Py 3.9–3.13) per sub-project + a
  `CI pass` aggregator job
- `codeql.yml` — CodeQL for `python` and `actions`
- `security.yml` — `pip-audit`, `bandit`, `gitleaks`, `trivy fs`
- `ai-guard.yml` — `actionlint`, `zizmor`, a static check that
  forbids `pull_request_target`, and an AI-attribution summary

A red CI is a real signal. Do not paper over failures (e.g. lowering
thresholds, adding `# noqa`, or marking tests `xfail`) without an
explicit reason in the PR description.

## Files agents must not modify silently

Changes to these files require an explicit, justified note in the PR
description. They control how CI behaves and are easy to weaken
unintentionally.

- `.github/workflows/*.yml` — CI definitions
- `.github/zizmor.yml` — workflow audit policy (SHA-pin enforcement)
- `.github/CODEOWNERS` — review routing
- `.github/dependabot.yml` — automated update config
- `ruff.toml` — lint rule set
- `[tool.coverage]` blocks in either `pyproject.toml`
- `LICENSE`, `SECURITY.md`

## Commit conventions

- One logical change per commit. PRs may bundle multiple commits.
- Subject line ≤ 72 chars; imperative mood ("add X", not "added X").
- If an AI agent assisted, include a `Co-Authored-By:` trailer with
  the tool name. The `ai-guard / AI attribution` workflow surfaces
  these as an informational summary on PRs — it does not block.

Example:

```
ci: drop --disable-pip from pip-audit invocation

Co-Authored-By: Claude <noreply@anthropic.com>
```

## What good agent behavior looks like in this repo

- **Read both READMEs and this file first** before editing anything
  cross-cutting.
- **Run the relevant subset of CI locally** before opening a PR. The
  `ruff check .` step in particular catches the `F82` undefined-name
  class of AI hallucination.
- **Don't introduce a new third-party dependency** without flagging
  it in the PR description and explaining why a stdlib path won't
  work. Both sub-projects deliberately keep their runtime
  dependencies near zero.
- **If a CI run fails, read the failing step before trying to "fix"
  the workflow itself.** Most failures are real bugs, not CI
  misconfiguration.
- **Pin third-party GitHub Actions to commit SHAs** in any new
  workflow. `zizmor` enforces this; the policy is in
  `.github/zizmor.yml`.
- **Do not add `pull_request_target` triggers.** A static check in
  `ai-guard.yml` will fail. If you genuinely need it, raise it for
  human review with the security context first.

## Out-of-scope automation

Don't introduce any of the following without a tracking issue and an
explicit ask:

- LLM-driven code-review actions running on PR events with secrets
  (the prompt-injection class this repo is hardened against)
- Auto-merge bots
- Force-push or rebase automation
- Workflows that delete branches on merge for branches the user
  hasn't opted into

## Asking questions

If a task is ambiguous and the context isn't clear from the existing
code or these conventions, **ask for clarification** rather than
guessing. The cost of a small back-and-forth is much lower than the
cost of an opinionated change that has to be reverted.
