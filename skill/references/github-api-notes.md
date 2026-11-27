# GitHub CLI and API notes for `babysit-pr`

This page lists the `gh` calls that the watcher makes and the fields that it reads. Every call has a 60-second
timeout, and the watcher decodes all output as UTF-8.

## Commands

### PR metadata

```bash
gh pr view --json number,url,state,mergedAt,closedAt,mergeCommit,headRefName,headRefOid,headRepository,headRepositoryOwner,baseRefName,mergeable,mergeStateStatus,reviewDecision,labels
```

The watcher uses this to find the PR number, URL, branch, head SHA, labels, and the closed, merged, and mergeable
state. `mergeCommit.oid` is the commit the base branch was updated to when the PR merged.

### PR checks

```bash
gh pr checks <n> --json name,state,bucket,link,workflow,event,startedAt,completedAt
```

The watcher uses this to count pending, failed, passed, and skipped checks, and to tell whether the current CI round
is done. `bucket` values are `pass`, `fail`, `pending`, `skipping`, and `cancel`. The watcher counts `cancel` as a
failure.

`gh pr checks` exits with code 1 when a check failed and with code 8 while checks are pending. In both cases it
still prints the requested JSON, so the watcher reads that JSON. A PR without any check also makes it exit with code
1, with no JSON and the message "no checks reported". The watcher reads that as an empty check list. Without JSON in
any other case, or with any other non-zero exit code, the call counts as failed.

### Workflow runs for the head SHA

```bash
gh api repos/{owner}/{repo}/actions/runs -X GET -f head_sha=<sha> -f per_page=100 -f event=pull_request
```

The watcher uses this to find failed workflow runs and the run IDs to rerun. It keeps only the latest run of each
workflow.

### Post-merge pipeline

```bash
# All workflow runs for the merge commit (the watcher keeps only base-branch runs from merge events)
gh api repos/{owner}/{repo}/actions/runs -X GET -f head_sha=<merge-sha> -f per_page=100 -f page=<p>

# Jobs of a failed run, to name the failed job
gh api repos/{owner}/{repo}/actions/runs/<run-id>/jobs -X GET -f per_page=100 -f page=<p> -f filter=latest
```

`--watch-merge-pipeline` waits for these runs and prints one line. Only runs on the base branch from events the merge
starts count: `push`, `workflow_run`, `workflow_dispatch`, `dynamic`, `check_suite`, `deployment`, and
`deployment_status`. Runs from `issue_comment`, `issues`, `schedule` and similar events also land on the merge commit
right after a merge, but say nothing about it. A run concluded `action_required` reports `manual: <workflow>`, and so
does a `waiting` run unless every pending deployment has no reviewers and a wait timer that is still running (from
`wait_timer_started_at`). Such a timer-only wait starts on its own and stays pending; required reviewers, a custom
protection rule (no reviewers, no timer), or an elapsed timer need someone:

```bash
# Pending deployments of a waiting run (reviewers vs. a timer-only wait)
gh api repos/{owner}/{repo}/actions/runs/<run-id>/pending_deployments -X GET
```

A failed run reports its first failed job, or the workflow name when the jobs lookup fails. Every result except `failed`
is held for 60 seconds to catch a late `workflow_run`, and the hold
restarts whenever an observed run appears, retries, or changes result; no run within 5 minutes reports `no_pipeline`.
Independent runs of one workflow all count, so a failed or canceled run is not hidden by a newer successful
invocation; a rerun replaces its earlier attempt. A `neutral` conclusion is not a pass: it usually means a check has
something to report (Cursor Bugbot returns it when it has comments), so it prints `failed: <job>`. Any other completed
conclusion outside `success` and `skipped` is not green either. A poll error clears the hold, and a
job name is flattened to one line before printing. An empty `mergeCommit.oid` is re-read on each poll until it appears.

### Failed log inspection

```bash
gh run view <run-id> --json jobs,name,workflowName,conclusion,status,url,headSha
gh run view <run-id> --log-failed
```

Use these to tell a branch-related failure from a flaky or unrelated one.

### Rerun failed jobs only

```bash
gh run rerun <run-id> --failed
```

This reruns only the failed jobs of a run, and the jobs they depend on.

### Up-to-date requirement of the base branch

```bash
gh api repos/{owner}/{repo}/branches/<base>/protection/required_status_checks
gh api "repos/{owner}/{repo}/rules/branches/<base>?per_page=100&page=<p>"
```

The watcher makes these calls only for a PR whose `mergeStateStatus` is `BEHIND`, and only when
`require_up_to_date` is `"auto"`. It remembers the answer for the rest of the run. The base requires up-to-date
branches when `strict` is `true`, or when a `required_status_checks` rule has
`parameters.strict_required_status_checks_policy` set to `true`. Only a definitive answer means no such
requirement: a 404 that says "Branch not protected" or "Required status checks not enabled", or `strict: false`, and
in both cases no strict ruleset. The protection call needs repository administration access, so a 403 proves
nothing. A 403, any other 404, and every other failure count as "required", and the watcher does not remember them.

## Review endpoints

```bash
# Issue comments on the PR, including the Codex review summary and codex-review-state:v1 metadata
gh api "repos/{owner}/{repo}/issues/<n>/comments?per_page=100&page=<p>"

# Inline review comments
gh api "repos/{owner}/{repo}/pulls/<n>/comments?per_page=100&page=<p>"

# Review submissions. When this call fails, the watcher lists reviews through GraphQL instead.
gh api "repos/{owner}/{repo}/pulls/<n>/reviews?per_page=100&page=<p>"

# Reactions on the PR (Codex 👀 in-progress and 👍 clean-completion reactions), all pages
gh api "repos/{owner}/{repo}/issues/<n>/reactions?per_page=100&page=<p>"

# The authenticated login, so the watcher can skip its own comments
gh api user
```

For Codex summary comments, the watcher reads both the Markdown status table and any embedded
`<!-- codex-review-state:v1 {...} -->` JSON block, plus `updated_at` (falling back to `created_at`) and reaction
`created_at` timestamps so an unedited `Running` summary can be distinguished from an older commit's `👍` reaction.

The watcher reads unresolved review threads through GraphQL (`pullRequest.reviewThreads`, 100 threads per page,
100 comments per thread). A GraphQL response with `errors` or without data is a failed lookup, never "no open
threads". When the lookup fails, every actionable inline comment blocks.

With PR-AF enabled, the watcher also reads the last 20 label and unlabel events of a labelled PR through GraphQL
(`pullRequest.timelineItems`). It uses them to notice that the PR-AF label was removed and added again on the same
head.

### Replying in-thread and resolving inline review threads

`gh pr comment` posts a top-level PR issue comment, which does not reply inside an inline review thread. Reply to the
root inline comment's numeric REST `id` (`new_review_items[].id` / `blocking_review_items[].id`), then look up the
thread's GraphQL node ID (`PRRT_...`) and resolve it:

```bash
# Reply inside an inline review thread
gh api repos/{owner}/{repo}/pulls/<n>/comments/<comment-id>/replies --method POST -f body="<triage box + rationale or issue link>"

# Look up review thread node IDs (PRRT_...) and comment databaseIds across all pages
gh api graphql --paginate -F owner="{owner}" -F name="{repo}" -F number=<n> -f query='
  query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
    repository(owner:$owner, name:$name) {
      pullRequest(number:$number) {
        reviewThreads(first:100, after:$endCursor) {
          pageInfo { hasNextPage endCursor }
          nodes { id isResolved comments(first:10) { nodes { databaseId } } }
        }
      }
    }
  }'

# Resolve a review thread by its GraphQL node ID
gh api graphql -F threadId="<PRRT_...>" -f query='
  mutation($threadId:ID!) {
    resolveReviewThread(input:{threadId:$threadId}) {
      thread { id isResolved }
    }
  }'
```

## JSON fields the watcher reads

### `gh pr view`

| Field | Used for |
|---|---|
| `number` | Identifying the PR |
| `url` | Finding `owner/repo` |
| `state` | Detecting closed PRs |
| `mergedAt` / `closedAt` | Detecting the end state |
| `mergeCommit.oid` | The base-branch commit to watch after a merge (merge, squash, or rebase) |
| `headRefName` | Branch name |
| `baseRefName` | Base branch, for the up-to-date requirement and the post-merge pipeline |
| `headRefOid` | Head SHA for the workflow run lookup |
| `mergeable` | Detecting conflicts (`CONFLICTING`) |
| `mergeStateStatus` | Detecting `BEHIND`, `BLOCKED`, `DIRTY`, `DRAFT`, and `UNKNOWN` |
| `reviewDecision` | Approval gating (`REVIEW_REQUIRED`, `CHANGES_REQUESTED`) |
| `labels` | The PR-AF label |

### `gh pr checks`

| Field | Used for |
|---|---|
| `bucket` | Pass, fail, pending, and skip classification |
| `state` | Extra pending detection |
| `name` / `workflow` | Reports, expected skips, required checks, and the PR-AF checks |
| `link` | Links to failed runs |
| `startedAt` / `completedAt` | Hung-check detection and the latest PR-AF run. GitHub reports `0001-01-01T00:00:00Z` for a check that has not started. The watcher ignores that value. |

### Actions runs API (`workflow_runs[]`)

| Field | Used for |
|---|---|
| `id` | Run ID for rerun commands |
| `workflow_id` | Keeping only the latest run of each workflow |
| `name` / `display_title` | Workflow name in the failure report |
| `status` / `conclusion` | Failure classification |
| `html_url` | Link to the run |
| `head_sha` | Keeping only runs for the current commit |
