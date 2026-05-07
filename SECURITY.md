# Security policy

## Reporting a vulnerability

Use **GitHub's [private vulnerability reporting][gh-pvr]** — open a
security advisory at
https://github.com/Jarvie8176/tools/security/advisories/new

[gh-pvr]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability

Do **not** file a public GitHub issue, post in Discussions, or
include a proof-of-concept in a pull request before the report is
acknowledged. Public disclosure before a fix is available puts every
user of the tool at risk.

When in doubt about whether something qualifies, report it — it's
easier to triage than to recover from a missed report.

## What to include

- A description of the issue and the impact you can demonstrate.
- The affected file(s) / sub-project(s) (`backup-verification` or
  `rclone-migrate`) and approximate location in the source.
- Steps to reproduce, or a minimal proof-of-concept. Synthetic test
  data is preferred over anything from a real backup.
- Your environment (OS, Python version, `rclone` version where
  relevant).

## Response expectations

- **Acknowledgement**: within 7 days of the report.
- **Triage and severity assessment**: within 14 days.
- **Fix or mitigation**: timeline communicated case-by-case based on
  severity and complexity. Critical issues (remote code execution,
  silent data loss, hash-collision-class) take priority.

This is a personal-asset OSS project maintained on a best-effort
basis. SLAs above are targets, not guarantees.

## Scope

In scope:

- Code in `backup-verification/` and `rclone-migrate/` (Python source,
  bundled hash profiles, CLI behaviour)
- CI / build configuration in `.github/`
- The `pyproject.toml` files and `ruff.toml` if they affect build or
  test integrity

Out of scope:

- Vulnerabilities in upstream dependencies (`rclone`, Python
  standard library, `pytest`, etc.). Report those to their maintainers
  directly. If a known upstream CVE meaningfully affects this project
  in a non-obvious way, reporting that connection is in scope.
- Issues that require an attacker to already control the host running
  the tool (the threat model assumes the local machine is trusted).
- Findings that depend on running with a tampered `pyproject.toml` or
  modified source tree.
- Denial-of-service via maliciously large inputs that would also
  overwhelm the underlying filesystem / `rclone` invocation.

## Coordinated disclosure

The maintainer credits reporters in the GitHub Security Advisory and
the release notes (where releases exist) unless the reporter prefers
to remain anonymous. State your preference in the report.

## What this project does for you

- **Pinned third-party Actions**: every external Action in
  `.github/workflows/` is pinned to a commit SHA, enforced by
  `zizmor` in CI. (Mitigation against tag-force-push supply-chain
  attacks like the 2026 trivy-action incident.)
- **Static AI-injection guards**: a CI step refuses
  `pull_request_target` triggers, the root cause of the 2026 "Comment
  and Control" prompt-injection class. No LLM agents are run with
  secrets in CI.
- **Multi-layered scanning**: CodeQL, `bandit`, `pip-audit`,
  `gitleaks`, and `trivy fs` run on every PR and on a weekly
  schedule.
- **Secret scanning + push protection**: enabled at the repo level so
  accidentally-committed credentials are blocked at push time, not
  detected after the fact.

## Security-relevant configuration

If you operate this toolchain in a sensitive environment:

- Run `rmig-delete` only after a successful `rmig-check` whose
  signature still matches — the gate is intentional, don't bypass it
  with `--force` patches.
- Review `state_dir` permissions; SQLite state files contain hashes
  and absolute file paths.
- Treat the `.rmig-cache.db` and `state.db` as integrity-bearing
  data: backing them up is reasonable; restoring them after they've
  been tampered with is not.
