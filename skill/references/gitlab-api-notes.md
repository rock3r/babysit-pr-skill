# GitLab CLI and API notes for `babysit-pr`

`scripts/glab_mr_watch.py` watches a GitLab merge request (gitlab.com or self-hosted, GitLab 15.6 or newer). It takes
the same flags as `gh_pr_watch.py` and returns the same JSON and the same action names. This page lists the `glab`
calls it makes and how it maps GitLab state onto the GitHub vocabulary that the actions use.

Every API call passes `--hostname <host>` explicitly, so nothing depends on the current directory once the MR is
known. Every call has a 60-second timeout. Lists are read 100 items per page until a short page.

## Preflight

```bash
glab auth status --hostname <host>
```

`glab` must be logged in to the MR's host (`glab auth login --hostname <host>`). Self-signed certificates and tokens
are `glab`'s configuration, not the watcher's.

## Commands

### Finding the MR

`--pr` takes an MR URL (`https://<host>/<group>/<project>/-/merge_requests/<iid>`, nested groups allowed), an IID, or
`auto`. For an IID or `auto` the watcher runs `glab mr view [iid] [-R <repo>] -F json` and reads `web_url`. Then:

```bash
glab api --hostname <host> projects/<url-encoded path>/merge_requests/<iid>
```

The `pr` object in the snapshot adds `host`, `project_id`, `pipeline_project_id`, and `detailed_merge_status` (GitLab's
raw verdict) to the usual keys. `number` is the IID and `repo` is the project path.

| `detailed_merge_status` | `mergeable` / `merge_state_status` / `review_decision` | Action through the core rules |
|---|---|---|
| `mergeable` | `MERGEABLE` / `CLEAN` | ready once the other gates pass |
| `conflict` | `CONFLICTING` / `DIRTY` | `diagnose_merge_conflict` |
| `need_rebase` | `MERGEABLE` / `BEHIND` (`up_to_date_required` unless `require_up_to_date` is `false`) | `diagnose_branch_behind` |
| `draft_status` (`draft` before GitLab 16) | `MERGEABLE` / `DRAFT` | `stop_draft_pr` once checks are green |
| `not_approved` / `requested_changes` | `MERGEABLE` / `BLOCKED` / `REVIEW_REQUIRED` or `CHANGES_REQUESTED` | `idle` |
| `checking`, `unchecked`, `preparing`, `approvals_syncing` | `UNKNOWN` / `UNKNOWN` | `idle` |
| anything else | `MERGEABLE` / `BLOCKED` | the failing pipeline or open thread acts first, else `diagnose_merge_blocked` |

The last row fails closed: an unknown value never reads as ready. A missing `detailed_merge_status` stops the watcher
with an error naming GitLab 15.6.

### Pipeline jobs

The watcher reads `head_pipeline` from the MR. When it is missing, or its `sha` differs from the MR `sha`, the check
list is empty (the pipeline for the new push has not appeared yet). The exception is a merged-results or merge-train
pipeline (ref `refs/merge-requests/<iid>/merge` or `/train`), which tests a merge commit: it counts only when that
commit has the MR head as a parent, read with
`glab api --hostname <host> projects/<pipeline project id>/repository/commits/<sha>`. Otherwise:

```bash
glab api --hostname <host> 'projects/<pipeline project id>/pipelines/<id>/jobs?per_page=100&page=<n>'
glab api --hostname <host> 'projects/<pipeline project id>/pipelines/<id>/bridges?per_page=100&page=<n>'
```

Each job becomes a check (`name`, `state` = job status, `workflow` = stage, `link` = `web_url`):

| Job status | Bucket |
|---|---|
| `success` | `pass` |
| `failed`, `allow_failure: false` / `true` | `fail` / ignored (GitLab shows a warning) |
| `canceled` | `cancel` (counts as a failure) |
| `skipped` | ignored |
| `manual`, `allow_failure: true` / `false` | ignored / `skipping` (the pipeline waits for someone to click Run) |
| `created` while the pipeline is `manual` or finished | ignored (waits behind a manual gate) |
| any other status | `pending` |

`required_checks`, `expected_skipped_checks`, and `hung_check_minutes` use the GitLab **job name**. Jobs in
`created` (waiting for earlier stages) or `scheduled` (a delay timer) count as pending but never as hung. A blocking manual
job gives `diagnose_skipping_checks`; when it is expected on every MR, add its name to `expected_skipped_checks`.

### Failed jobs and retries

`failed_runs` has one entry per failed or canceled job that counts: `run_id` is the job ID, `workflow_name` the job
name, plus `stage`. A job is retry-eligible when a `retry_eligible_workflow_keywords` entry occurs in its name or
stage. Bridge (trigger) jobs never are: inspect the downstream pipeline instead. `--retry-failed-now` runs:

```bash
glab api --hostname <host> -X POST projects/<pipeline project id>/jobs/<job id>/retry
```

Diagnose a failure with `glab ci view` or `glab ci trace <job id> -R <group>/<project>`.

### Discussions

```bash
glab api --hostname <host> 'projects/<project id>/merge_requests/<iid>/discussions?per_page=100&page=<n>'
glab api --hostname <host> user
glab api --hostname <host> projects/<project id>/members/all/<user id>
```

System notes are skipped. A note counts when its author is a **trusted member** (Developer access or higher on the
project, the match for GitHub's `OWNER`/`MEMBER`/`COLLABORATOR`; `trusted_author_associations` does not apply), a
review bot, or the authenticated user. Notes from anyone else are ignored, and so are their threads. A failed member
or `user` lookup (other than a 404) fails the snapshot, and the loops retry.

A counted note is a **new item** when someone other than the authenticated user wrote it and its ID or body is new
(the body is compared, not `updated_at`, because resolving a thread changes `updated_at`). An unresolved resolvable
discussion is a **blocking item** (its first counted note) once a counted note is in it. Review items carry
`discussion_id`; `kind` is `review_comment` for diff notes and `issue_comment` otherwise. A plain comment (not started
as a thread) is not resolvable and never blocks.

Bot users are usernames matching `project_<n>_bot…`, `group_<n>_bot…`, or `service_account_…`. They count as review
bots only when the username contains a `review_bot_login_keywords` entry.
The Codex and PR-AF gates are GitHub-only: the watcher forces `codex.enabled`, `codex.required`, and `pr_af.enabled`
off, and `codex_gate` and `pr_af_gate` are always `null`.

## Replying to and resolving a thread

```bash
glab api --hostname <host> -X POST \
  projects/<url-encoded path>/merge_requests/<iid>/discussions/<discussion_id>/notes -f body='Fixed in <sha>.'
glab api --hostname <host> -X PUT \
  projects/<url-encoded path>/merge_requests/<iid>/discussions/<discussion_id> -F resolved=true
```

## Updating a branch that needs a rebase

`need_rebase` follows the same rule as GitHub's `BEHIND`: merge the target branch into the source branch and push once.
Never use GitLab's Rebase button or the `/rebase` quick action on a shared branch, because both force-push.
