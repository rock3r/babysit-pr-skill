# Changelog

All notable changes to this skill are listed here. Versions follow semantic versioning.

## 2.2.0

This is a minor release: it adds the `codex.idle_wait_minutes` config key and fixes behaviour.

### Fixed

- With `require_up_to_date: "auto"`, a 403 from the branch-protection lookup counted as "not required". That endpoint
  needs repository administration access, so a collaborator token gets 403 even when strict up-to-date checks are
  required, and free private repositories answer 403 as well. A BEHIND PR could then get `stop_ready_to_merge`. Now
  only definitive answers mean "not required": a 404 that says the branch has no protection or no required checks,
  or `strict: false`, and in both cases no strict ruleset. A 403, any other 404, and every other failure count as
  required, and the watcher does not cache them. A repository that knows it has no strict rule can set
  `require_up_to_date: false`.
- The watcher waited until the session timeout on a PR where Codex was active but never reviewed the new head. Once
  the checks are done and the grace period has passed, with no 👀 reaction and no running or completed review of the
  head:
  - with `codex.required`, the watcher emits `request_codex_review`, and `--once` returns for it;
  - without it, the watcher waits `codex.idle_wait_minutes` (new, default 10). After that the missing review no longer
    blocks readiness, and `codex_gate.idle_wait_expired` and `codex_gate.note` say that Codex did not review the head.

  A running Codex review still blocks as before.

## 2.1.0

This is a minor release: it adds two config keys and fixes behaviour, and it removes no action or key.

### Added

- `sync.py` keeps files that belong to the repository. It never deletes or overwrites `config.json`, `VERSION` (which
  it still rewrites), `skill-source.json`, or any path that matches a glob in the new `sync.keep` setting. It lists
  kept files in its report. An unreadable `config.json` or a bad `sync.keep` stops it before it changes anything.
  `skill-source.json` is a reserved name, so it is kept without any `sync.keep` entry.
- `require_up_to_date` (`"auto"`, `true`, or `false`, default `"auto"`) says whether a PR that is behind its base
  must be updated before merge.

### Fixed

- `BEHIND` blocked every PR that was behind its base, and gave `diagnose_branch_behind`, even when branch protection
  allows merging an out-of-date branch. With `"auto"`, the watcher now reads the base branch's required status checks
  and rulesets. Only a strict (up-to-date) requirement makes `BEHIND` block. A 403 or 404 answer means not required,
  and any other failed lookup counts as required. The snapshot shows the result as `pr.up_to_date_required`.
- `check_count` counted advisory PR-AF checks and expected skips, which the pass, pending, and fail totals leave out.
  A PR with only such checks could never be ready, and `diagnose_no_checks` never fired. `check_count` now counts the
  same checks as the totals.
- With `codex.required`, a head that Codex has not reviewed while its latest review is of an older commit only got
  `wait_codex`, so the watch ran into the session timeout on repositories where Codex does not review every push by
  itself. Once the checks are done and the grace period has passed, the watcher now emits `request_codex_review`.
  Without `codex.required`, it keeps waiting.

## 2.0.0

This is a major release because it removes an action and a config key. The README's versioning table counts both
as major changes.

### Removed

- The optional CodeRabbit gate, which no repository used. The watcher no longer emits `wait_coderabbit`, and the
  snapshot no longer has a `coderabbit_gate` field.
- The `coderabbit` config section. A config file that still has it keeps working: the watcher prints the usual
  unknown-key warning and ignores it. Delete the section to silence the warning. The config format stays at
  version 1.
- A CodeRabbit check is now an ordinary check. While it is pending, the watcher waits for it like for any other
  check. Comments from `coderabbitai[bot]` are review items only when `review_bot_login_keywords` matches them.

### Fixed

- The tests that run the command line no longer leave their config behind for later tests.

## 1.0.1

### Fixed

- A PR without any check made `gh pr checks` exit with "no checks reported", and the watcher treated that as a
  failed command. `--snapshot` stopped with an error, and `--once` retried until the session timeout. The watcher
  now reads it as an empty check list. Other failures of `gh pr checks` still stop the poll.
- An empty check set is still never ready. After the grace period, the watcher now ends the wait with the new
  `diagnose_no_checks` action instead of idling until the session timeout.
- The `checks` summary now has `check_count`, the number of checks that GitHub reported.

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
