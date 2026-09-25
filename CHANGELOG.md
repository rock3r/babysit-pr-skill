# Changelog

All notable changes to this skill are listed here. Versions follow semantic versioning.

## 1.0.0

The first release of the central skill. It merges the copies that several repositories carried into one version,
and moves every project-specific value into `config.json`.

### Config

- The watcher reads `config.json` from the skill folder, or the file given with `--config`. Missing keys use the
  defaults, unknown keys only warn, and a value of the wrong type stops the watcher.
- `--print-config` prints the settings that the watcher uses.
- New keys: `local_gate`, `expected_skipped_checks`, `required_checks`, `retry_eligible_workflow_keywords`,
  `hung_check_minutes`, `trusted_author_associations`, `review_bot_login_keywords`, `max_session_minutes`,
  `codex`, `coderabbit`, `pr_af`, and `cleanup`.

### New actions

- `diagnose_merge_blocked`: every check is green, but GitHub still says `BLOCKED` and nothing else explains it. Before,
  the watcher idled until the session timeout.
- `diagnose_branch_behind`: the branch is behind its base and branch protection wants it updated.
- `diagnose_missing_required_checks`: a check from `required_checks` never passed.
- `diagnose_codex_review`: the Codex summary reports a failed or unknown status for the head.
- `request_codex_review`: `codex.required` is on and Codex has not reviewed the PR.
- `wait_coderabbit` and `wait_pr_af`: waits for the optional CodeRabbit and PR-AF gates.

### Changed behaviour

- An empty check set is never green. It usually means GitHub has not registered the checks for a new push yet.
- A merge conflict or an out-of-date branch waits while a review bot is still running.
- A session timeout keeps the actions of the last snapshot, and `--watch` includes that snapshot in its stop event.
- When the reactions cannot be read, the Codex gate stays closed and the watcher emits `wait_codex`.
- The Codex gate matches the bot login exactly.
- An expected skipped check is ignored only when its result is `skipping`. A `neutral` result still needs a look.

### Hardening

- `gh` output is decoded as UTF-8, and every `gh` call stops after 60 seconds.
- A `gh` call without any output fails with a clear message.
- A GraphQL error in the review-thread lookup is a failed lookup, not "no open threads".
- When the REST review list fails, the watcher lists reviews through GraphQL.

### Distribution

- `sync.py` vendors a pinned version into `.agents/skills/babysit-pr/`, keeps `config.json`, and writes `VERSION`.
- CI runs the watcher and sync tests on Python 3.9 and 3.12.
