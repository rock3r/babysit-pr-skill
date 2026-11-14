# CI classification and stop-and-ask heuristics

## Branch-caused vs flaky CI failures

Treat a failure as **branch-caused** (fix locally; do not rerun) when logs show a regression introduced by the PR:

- Compile, type-check, or lint failures in touched files or downstream modules.
- Deterministic unit or integration test failures in changed behaviour.
- Snapshot mismatches from UI or text edits in the branch.
- Static-analysis findings introduced by the latest push.
- Build-script or CI workflow edits in the PR that fail deterministically.

Treat a failure as **likely flaky or external** (eligible for `--retry-failed-now` once all checks for `head_sha` are
terminal and all failed workflows match `retry_eligible_workflow_keywords`) when logs show transient infrastructure
issues:

- DNS, network, or package-registry timeouts fetching dependencies.
- Runner provisioning, container pull, or startup failures.
- GitHub Actions platform incidents or external service rate limits / 5xx responses.
- Known non-deterministic timing or concurrency flakes in E2E suites unrelated to touched code.

When uncertain, inspect `gh run view <run-id> --log-failed` once before choosing a rerun.

## When to act vs defer on a review comment

Act autonomously only when the finding is technically sound, in scope per the 2x2 triage matrix, consistent with the
owner's stated intent, and fixable without unrelated refactors or weakening any repository security rule.

Do **not** apply a review suggestion on your own when:

- The comment is ambiguous and needs clarification.
- The requested change conflicts with explicit owner instructions.
- It requires a product, UX, API, or architectural decision the owner has not made.
- Applying it would weaken permissions, auth, validation, or another repository security invariant.

## Stop-and-ask triggers

Pause the loop and ask the owner whenever:

- The working tree has unrelated uncommitted changes that make safe edits uncertain.
- `gh` authentication, token scopes, or push permissions fail.
- CI continues failing after the per-SHA rerun budget is exhausted (`stop_exhausted_retries`).
- Reviewer feedback requires cross-team coordination or a product decision.
- `local_gate` fails after a fix for a non-obvious reason.
