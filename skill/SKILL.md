---
name: babysit-pr
description: >
  Use when asked to watch, monitor, or babysit an open GitHub pull request or GitLab merge request (including
  self-hosted GitLab): poll CI and review gates until ready to merge, fix branch-caused failures, retry flaky runs,
  triage findings, and stop when a human decision is needed.
metadata:
  author: "Sebastiano Poggi"
  version: "2.5.0"
---

# PR babysitter

Watch an open PR until it is merged/closed, ready to merge (CI green, review gates clear, no conflicts), or blocked on
an owner decision.

## Authority and preflight

- `scripts/gh_pr_watch.py` is the **sole authority** on merge readiness (`stop_ready_to_merge`). Use `gh pr checks`,
  `gh pr view`, and `gh run view` only to diagnose states the watcher reported — never to declare a PR green, and never
  replace a failing watcher or invalid `config.json` with ad-hoc polling (fix or report it as a tooling/config error).
  Zero reported checks (`checks.check_count == 0`) is never green (`diagnose_no_checks`).
- Before polling or editing:
  1. Inspect effective settings with `--print-config` (reads `.agents/skills/babysit-pr/config.json`).
  2. Confirm `gh auth status` succeeds and tell the owner which PR (number and URL) you are tracking.
  3. Before touching code, verify `HEAD` is on `pr.head_branch` at `pr.head_sha` with a clean working tree. If on
     another branch, check out the PR branch in an isolated git worktree first; if unrelated uncommitted changes exist,
     stop and ask the owner.

## Watch in a subagent

Run every wait (`--watch`, or one blocking `--once`) in a subagent. Use the lowest model tier the harness offers at
its lowest reasoning effort: the subagent only runs the watcher and has no judgement to add. Pick from the models this
harness exposes. Examples: Claude Code `model: "haiku"`, `effort: "low"`; Antigravity, Hydrogen. It must not edit files,
push, or diagnose. Give it the PR number or URL, not `--pr auto` once the branch may be deleted.

The subagent returns exactly one line: the final action and a one-line summary, with only the identifiers the main
agent needs. Examples: `stop_ready_to_merge`, `stop_non_retryable_failure: <job>`,
`process_review_comment: 2 new, 1 blocking (ids 123, 456)`, or `stop_pr_closed: merged; merge_pipeline: success`.
Never return the full snapshot JSON, comment bodies, or logs. On a merged `stop_pr_closed`, the same subagent runs
`--watch-merge-pipeline` before returning. The main agent acts on the returned line and starts a new subagent for the
next cycle.

If the harness has no subagents, run `--once` in the main agent and keep only the action line, not the full JSON.
Never write your own polling loop in the main agent. If the watcher doesn't cover a state, report it as a tooling gap.

## Watcher CLI and actions

Run from the repository root (`--pr` accepts `auto`, a PR number, or a PR URL; optional flags: `--repo <owner/repo>`,
`--config <path>`, `--state-file <path>`, `--poll-seconds <n>`, `--max-flaky-retries <n>`, `--max-session-minutes <n>`,
`--json`):

```bash
python3 .agents/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --once             # default: poll every 30s until action needed
python3 .agents/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --snapshot         # instant state check, no waiting
python3 .agents/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --watch            # stream JSONL events until terminal action
python3 .agents/skills/babysit-pr/scripts/gh_pr_watch.py --pr <number> --watch-merge-pipeline # after merge: base-branch pipeline, one line
python3 .agents/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --retry-failed-now # rerun failed jobs of retry-eligible workflows
python3 .agents/skills/babysit-pr/scripts/gh_pr_watch.py --print-config               # print effective config JSON
```

Use `--once` whenever the harness returns output only on command exit (act, then run `--once` again); use `--watch` only
when the harness reads live streamed stdout. `--watch-merge-pipeline` is a separate mode, not an action: run it only
after `stop_pr_closed` reports `pr.merged: true`. It prints one bare line: `success`, `failed: <job>`, `canceled`,
`manual: <job>`, `skipped`, `no_pipeline`, or `timeout`. Read `actions` from:

- `--once` and `--snapshot`: top-level `actions` (alongside `pr`, `checks`, `failed_runs`, `codex_gate`, `pr_af_gate`,
  `hung_checks`, `new_review_items`, `blocking_review_items`, `retry_state`).
- `--retry-failed-now`: `snapshot.actions` (top level reports `rerun_attempted`, `rerun_count`, `reason`).
- `--watch`: `payload.snapshot.actions` on `{"event":"snapshot"}` lines; `payload.actions` on `{"event":"stop"}`.

### Action index

- **Passive waits (`--once` keeps polling; `--watch` continues):** `idle` (CI pending), `wait_codex` (Codex reviewing
  or head unreviewed — do not push or merge), `wait_pr_af` (PR-AF check pending or within grace window).
- **Active work (`--once` returns; `--watch` continues):** `process_review_comment` (triage `new_review_items` and
  `blocking_review_items`), `diagnose_ci_failure` (classify failed checks before acting), `retry_failed_checks` (all
  failed runs match `retry_eligible_workflow_keywords` and checks are terminal — run `--retry-failed-now`),
  `diagnose_codex_review` (Codex summary failed/unknown for head, or `codex.required` is true and `Running` stayed stuck
  without 👀 past `codex.idle_wait_minutes` — inspect summary, comment `@codex review` once, else escalate),
  `request_codex_review` (`codex.required` is true and head is unreviewed — comment `@codex review`).
- **Diagnose and stop (`--once` returns; `--watch` stops):** `diagnose_merge_conflict` (`CONFLICTING`/`DIRTY`, waits
  while bots run), `diagnose_branch_behind` (`BEHIND` when `pr.up_to_date_required` is true, waits while bots run),
  `diagnose_merge_blocked` (checks green yet `BLOCKED` — inspect `repos/{owner}/{repo}/rules/branches/<base>`, report
  blocker, never bypass), `diagnose_no_checks` (`check_count == 0` past grace — check triggers/fork approval, report),
  `diagnose_missing_required_checks` (`checks.required_missing` non-empty — check path filters/triggers, report),
  `diagnose_hung_check` (pending past `hung_check_minutes`), `diagnose_skipping_checks` (unexpected `skipping` or
  `neutral` check — inspect with `gh run view`).
- **Terminal stops (`--once` returns; `--watch` stops):** `stop_non_retryable_failure` (fix branch-caused failure
  locally; do not rerun blindly), `stop_exhausted_retries` (per-SHA retry budget used, default 3 — escalate),
  `stop_ready_to_merge` (sole green signal; when `codex_gate.idle_wait_expired` is `true`, report `codex_gate.note` to
  the owner), `stop_draft_pr` (green draft — ask owner to mark ready), `stop_pr_closed` (merged or closed),
  `stop_session_timeout` (session limit reached; carries last snapshot's `actions`).

## GitLab merge requests

- Use `scripts/glab_mr_watch.py` instead of `gh_pr_watch.py` for GitLab: when the URL contains `/-/merge_requests/`, or
  for `--pr auto` / IID when the remote is a GitLab host (such as `gitlab.com` or a host authenticated in `glab` —
  never GitHub Enterprise `/pull/<n>` remotes). Same flags, JSON, and actions; `--pr` takes an MR URL, IID, or
  `auto`. Preflight with `glab auth status --hostname <host>` instead of `gh auth status`.
- It is the sole authority on readiness there too. Diagnose with `glab mr view`, `glab ci view`, and
  `glab ci trace <job-id>`, never declare an MR green from them. `pr.detailed_merge_status` holds GitLab's raw verdict.
- Check names are **job names**. A blocking manual job (`allow_failure: false`) gives `diagnose_skipping_checks`: tell
  the owner it needs someone to click Run, and never play manual jobs (they often deploy) without the owner's go-ahead.
- Codex and PR-AF gates are off. Only notes from members with Developer access or higher, review bots, and you count.
  Unresolved threads block as on GitHub; reply and resolve through the discussions API, and update a `need_rebase`
  branch by merging the target into it (never GitLab's Rebase button or `/rebase`).

## Review gates and threads

- **Codex (`codex_gate`):** `chatgpt-codex-connector[bot]` has no CI check; it adds 👀 while reviewing
  (`codex_gate.reviewing`) and maintains a "Codex Review Summary" table (and `codex-review-state:v1` marker). When active
  (or `codex.required` is `true`), readiness requires a **Completed** review of `head_sha` (`codex_gate.head_reviewed`).
  Without `codex.required`, an unstarted review expires after `codex.idle_wait_minutes`, and an unedited `Running`
  summary with no 👀 expires after the 60s grace window when Codex added 👍 after `Running` (or after
  `codex.idle_wait_minutes` without 👍), setting `idle_wait_expired: true` and `codex_gate.note` (with `codex.required`,
  a stuck `Running` summary sets `stale_running: true` and emits `diagnose_codex_review`). Status tables/markers and
  `"Didn't find any major issues"` comments (which prove clean completion when naming `head_sha`) are never review items.
  Unreadable reactions keep the gate closed.
- **PR-AF (`pr_af_gate`):** Enabled via `pr_af.enabled` when `pr_af.label` is on the PR (rerunning on every push while
  labelled). Its check is **advisory** (`wait_pr_af` while pending or within `pr_af.missing_check_grace_minutes`; failed,
  skipped, cancelled, or hung PR-AF checks never count as CI failures), but comments from `pr_af.review_author_login`
  (`github-actions[bot]`) carrying `pr_af.review_body_markers` (or belonging to a review carrying a marker) while a head
  PR-AF check exists **block merge**. Never add `github-actions[bot]` to `review_bot_login_keywords`.
- **Threads (`blocking_review_items`):** Any unresolved inline thread — including open threads started by the
  authenticated account — blocks readiness (and if GraphQL thread lookup fails, every actionable inline comment blocks).
  Reply in-thread via REST `POST repos/{owner}/{repo}/pulls/<n>/comments/<comment-id>/replies` (not `gh pr comment`)
  when no code changed or when linking an issue, then resolve via GraphQL `resolveReviewThread`.

## Fix loop, triage, branch updates, and cleanup

- **Push-once batching:** Every push restarts CI and bot reviews. Start fixing branch-caused failures locally right
  away, fold in bot/human comments that arrive while checks and bots finish, run `local_gate` (or the repo's documented
  check command when `null`) until green, push **once** per cycle, then reply to and resolve bot threads. Follow repo
  rules (`AGENTS.md`) on pushing and merging.
- **2x2 finding triage** (name the box in your reply; a defect that already exists on the base branch without this PR is
  out of scope):

  | | **Blocker** (visible harm, data loss, security hole) | **Improvement** (hardening, stricter validation, "could be wrong if") |
  |---|---|---|
  | **In scope** (this PR's behaviour) | Fix now. | Fix now if trivial; otherwise file an issue. |
  | **Out of scope** (pre-existing on base) | File an issue and link on the PR. | File an issue. |

- **Loop breakers** — stop and ask the owner (with commits, file count, and `git diff --stat` vs base; preserve reverted
  out-of-scope work on `git branch followup/<topic>`) when: a fix would break working behaviour; 3 rounds in a row yield
  no in-scope blocker; 2 rounds in a row target code added only for an earlier finding in this PR; the diff outgrew the
  task; or every push spawns new findings.
- **Updating `BEHIND` or `CONFLICTING` branches:** Wait until no review bot is running -> read the real base with
  `gh pr view <n> --json baseRefName` (never assume `main`) -> fetch from the **target** repository's remote (`origin`
  on a fork is the fork, not upstream) -> **merge** the base into the PR branch (rebase with `--force-with-lease` only
  when the owner asked; never plain `--force`), verify earlier fixes remain, run `local_gate`, and push once.
- **Post-merge pipeline (`stop_pr_closed`):** When `pr.merged` is `true`, the same subagent runs
  `--watch-merge-pipeline` and appends its one-line result to the action line. On `failed: <job>`, `canceled`,
  `manual: <job>`, `skipped`, `no_pipeline`, or `timeout`, report the result to the owner; do not push to the base
  branch or rerun base-branch jobs unless the owner asks. A closed unmerged PR skips this step.
- **Post-merge cleanup (`stop_pr_closed`):** Clean up **only** when `pr.merged` is `true` (`mergedAt` set); if closed
  unmerged, keep everything and tell the owner. Run from the **main checkout** (never inside the PR worktree; local
  state only — never delete remote branches):
  1. Remove any linked worktree first with `git worktree remove <path>` (never `--force`; stop and report if dirty).
  2. If `cleanup.branch_delete_requires_approval` is `true`, ask the owner first. Prove the local tip matches the
     merged `headRefOid` (`test "$(git rev-parse <head_branch>)" = "$merged_head"`) before `git branch -D <head_branch>`
     — if tips differ, keep the branch and tell the owner.
  3. Update the base branch in the main checkout with `git pull --ff-only`.

## References

- `config.json` key-by-key reference and diagnostic playbooks for uncommon actions ->
  [references/actions-and-config.md](references/actions-and-config.md)
- Classifying branch-caused vs flaky CI failures, comment-safety gates, and stop-and-ask triggers ->
  [references/heuristics.md](references/heuristics.md)
- `gh` CLI/REST/GraphQL calls, inline reply + `resolveReviewThread` payloads, and JSON fields read by the watcher ->
  [references/github-api-notes.md](references/github-api-notes.md)
- `glab` calls, GitLab state mapping, and thread reply/resolve payloads ->
  [references/gitlab-api-notes.md](references/gitlab-api-notes.md)
