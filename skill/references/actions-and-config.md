# Config and action playbooks for `babysit-pr`

## Project config (`config.json`)

`.agents/skills/babysit-pr/config.json` configures project-specific behaviour. `sync.py` creates it from
`config.example.json` on first install and never overwrites it. Missing keys use defaults; unknown keys emit a stderr
warning; wrong types abort with an error naming the key. Run `gh_pr_watch.py --print-config` to inspect effective
values.

| Key | Default | Meaning |
|---|---|---|
| `version` | `1` | Config schema version. A higher version than the watcher supports aborts with an error. |
| `local_gate` | `null` | Command to run before every push (e.g. `./gradlew check`). When `null`, use the repository's documented check command. |
| `expected_skipped_checks` | `[]` | Check names skipped on PRs by design. `skipping` is ignored; `neutral` still surfaces `diagnose_skipping_checks`. |
| `required_checks` | `[]` | Case-insensitive check names that must pass before `stop_ready_to_merge`. |
| `retry_eligible_workflow_keywords` | `["e2e"]` | Case-insensitive workflow name substrings eligible for `--retry-failed-now`. |
| `hung_check_minutes` | `30` | Pending duration before a non-advisory check triggers `diagnose_hung_check`. |
| `trusted_author_associations` | `["OWNER", "MEMBER", "COLLABORATOR"]` | Author associations treated as actionable human reviewers. |
| `review_bot_login_keywords` | `["codex"]` | Substrings matching `[bot]` logins treated as review bots. Never add `github-actions` here. |
| `max_session_minutes` | `90` | Default session timeout when `--max-session-minutes` is omitted. |
| `require_up_to_date` | `"auto"` | Whether `BEHIND` blocks merge. `"auto"` checks branch protection and rulesets once per run: only definitive non-strict answers (`strict: false` or 404 "Branch not protected" / "Required status checks not enabled" with no strict ruleset) clear the requirement; 403 and other errors fail closed as required. Set `false` when the base has no strict rule. |
| `codex.enabled` | `true` | Poll Codex reactions and review summary comments. |
| `codex.required` | `false` | Require a Completed Codex review of `head_sha` even when Codex has never posted on the PR. |
| `codex.idle_wait_minutes` | `10` | Without `codex.required`, minutes after checks finish to wait for Codex to start reviewing `head_sha` or edit a stuck `Running` summary with no 👀 reaction before setting `idle_wait_expired`. |
| `pr_af.enabled` | `false` | Enable label-triggered PR-AF gate tracking. |
| `pr_af.label` | `"pr-af"` | PR label that triggers PR-AF on pushes. |
| `pr_af.workflow_names`, `pr_af.check_names` | `[]` | Case- and whitespace-insensitive names identifying PR-AF runs/checks. |
| `pr_af.review_body_markers` | `[]` | Case-insensitive body substrings identifying PR-AF findings from `pr_af.review_author_login`. |
| `pr_af.review_author_login` | `"github-actions[bot]"` | Bot login used by PR-AF comments/reviews. |
| `pr_af.missing_check_grace_minutes` | `5` | Minutes a labelled head waits for a PR-AF check to appear before stopping the wait. |
| `cleanup.branch_delete_requires_approval` | `false` | Require explicit owner approval before deleting a merged local branch. |
| `sync.keep` | `[]` | Consumer-owned relative paths/globs inside `.agents/skills/babysit-pr/` (`*` also matches `/`) preserved across `sync.py` upgrades (`skill-source.json`, `config.json`, and `VERSION` are always reserved). |

## Diagnostic playbooks for uncommon actions

- **`diagnose_merge_blocked`**: All reported checks passed and no open review thread or conflict explains GitHub's
  `BLOCKED` state. Common causes: an unreported required status check, a required code-owner approval, required commit
  signatures, or a branch ruleset. Inspect `gh api "repos/{owner}/{repo}/rules/branches/<base>?per_page=100"` and
  `gh api repos/{owner}/{repo}/branches/<base>/protection`, report the exact rule to the owner, and never bypass it.
- **`diagnose_no_checks`**: `checks.check_count` remained `0` past the 60-second grace period. Inspect workflow
  `on.pull_request` path/branch filters, disabled workflows, or fork PR workflow approval gates. Report to the owner;
  never treat zero checks as green.
- **`diagnose_missing_required_checks`**: Checks finished, but one or more entries in `checks.required_missing` never
  passed. Check path filters, conditional `if:` expressions, disabled workflows, or mismatched check names in
  `config.json`.
- **`diagnose_codex_review`**: Codex's summary reports a failed or unknown review for `head_sha`, or `codex.required` is
  `true` and `Running` stayed stuck with no 👀 reaction past `codex.idle_wait_minutes` (`codex_gate.stale_running`). Read
  the Codex summary comment on the PR; if the review failed or stalled transiently, post `@codex review` once. If it
  fails a second time, stop and report to the owner.
- **`diagnose_hung_check` / `diagnose_skipping_checks`**: Inspect the workflow run and job steps with
  `gh run view <run-id> --json jobs,name,status,conclusion,url` and report why the job stalled or resolved as
  unexpected `skipping`/`neutral`.
- **`stop_exhausted_retries`**: `--retry-failed-now` reached `--max-flaky-retries` (default 3) on the current
  `head_sha`. Stop rerunning, inspect `gh run view <run-id> --log-failed`, and escalate to the owner with the failing
  run URLs.
