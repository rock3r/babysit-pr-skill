#!/usr/bin/env python3
"""Watch GitLab MR pipelines and discussions for PR babysitting workflows.

This is an adapter around `gh_pr_watch.py`. It collects the GitLab state into the
same shapes (a `pr` dict, checks with a `bucket`, failed runs, review items) and lets
the core watcher decide the actions, so both hosts share one set of readiness rules,
one CLI, and one JSON output.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import gh_pr_watch as core  # noqa: E402

GLAB_COMMAND_TIMEOUT_SECONDS = core.GH_COMMAND_TIMEOUT_SECONDS
PAGE_SIZE = 100

# GitLab `detailed_merge_status` -> (mergeable, merge_state_status, review_decision) in
# the GitHub vocabulary that the core rules read. A value missing here means BLOCKED, so
# an unknown or future value can never read as ready.
DETAILED_MERGE_STATUS_MAP = {
    "mergeable": ("MERGEABLE", "CLEAN", ""),
    "conflict": ("CONFLICTING", "DIRTY", ""),
    "need_rebase": ("MERGEABLE", "BEHIND", ""),
    "draft_status": ("MERGEABLE", "DRAFT", ""),
    "draft": ("MERGEABLE", "DRAFT", ""),  # name used before GitLab 16
    "not_approved": ("MERGEABLE", "BLOCKED", "REVIEW_REQUIRED"),
    "requested_changes": ("MERGEABLE", "BLOCKED", "CHANGES_REQUESTED"),
    "checking": ("UNKNOWN", "UNKNOWN", ""),
    "unchecked": ("UNKNOWN", "UNKNOWN", ""),
    "preparing": ("UNKNOWN", "UNKNOWN", ""),
    "approvals_syncing": ("UNKNOWN", "UNKNOWN", ""),
}
UNKNOWN_MERGE_STATUS = ("MERGEABLE", "BLOCKED", "")

MR_STATE_MAP = {"opened": "OPEN", "merged": "MERGED", "closed": "CLOSED", "locked": "LOCKED"}

# Pipeline statuses after which a `created` job will never start on its own: the
# pipeline either finished or waits for someone to play a manual job.
PIPELINE_SETTLED_STATUSES = {"manual", "success", "failed", "canceled", "skipped"}

# Job statuses that wait on earlier stages or a `when: delayed` timer. They count as
# pending but are left out of hung detection.
WAITING_JOB_STATUSES = {"created", "scheduled"}

# Pipelines that GitLab runs on an MR ref test the merge result, so their sha differs
# from the MR head by design.
MERGE_REF_PATTERN = re.compile(r"^refs/merge-requests/\d+/(merge|train)$")

# GitLab bot users: project and group access tokens, and service accounts.
BOT_USERNAME_PATTERN = re.compile(r"^(project|group)_\d+_bot|^service_account_")

# GitLab's Developer role: the lowest that can push, like a GitHub collaborator.
TRUSTED_ACCESS_LEVEL = 30

_AUTHENTICATED_USERNAME_CACHE = {}
# (host, project id, user id) -> access level on the project, 0 for non-members.
_MEMBER_ACCESS_CACHE = {}
# (host, user id) -> whether GitLab marks the account as a bot.
_USER_IS_BOT_CACHE = {}


def run_glab(args):
    """Run `glab` and return its stdout. Every GitLab call goes through here."""
    cmd = ["glab"] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GLAB_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as err:
        raise core.GhCommandError("`glab` command not found") from err
    except subprocess.TimeoutExpired as err:
        raise core.GhCommandError(f"GitLab CLI command timed out: {' '.join(cmd)}") from err
    except subprocess.CalledProcessError as err:
        parts = [f"GitLab CLI command failed: {' '.join(cmd)}"]
        for label, text in (("stdout", err.stdout), ("stderr", err.stderr)):
            if (text or "").strip():
                parts.append(f"{label}: {text.strip()}")
        raise core.GhCommandError("\n".join(parts)) from err
    return proc.stdout or ""


def glab_json(args):
    raw = run_glab(args).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise core.GhCommandError(f"Failed to parse JSON from glab output for {' '.join(args)}") from err


def glab_api(host, endpoint, method="GET"):
    args = ["api", "--hostname", host]
    if method != "GET":
        args.extend(["-X", method])
    args.append(endpoint)
    return glab_json(args)


def glab_api_list(host, endpoint):
    """All pages of a list endpoint."""
    sep = "&" if "?" in endpoint else "?"
    items = []
    page = 1
    while True:
        data = glab_api(host, f"{endpoint}{sep}per_page={PAGE_SIZE}&page={page}")
        if data is None:
            break
        if not isinstance(data, list):
            raise core.GhCommandError(f"Expected a list from GitLab endpoint {endpoint}")
        items.extend(data)
        if len(data) < PAGE_SIZE:
            break
        page += 1
    return items


def project_ref(path_or_id):
    return quote(str(path_or_id), safe="")


def parse_mr_url(url):
    """(host, project path, iid) from an MR URL, or None. Handles nested groups."""
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("GitLab MR URL must not contain userinfo")
    if parsed.port is not None:
        raise ValueError(f"GitLab MR URLs with a port are not supported by glab --hostname: {parsed.netloc}")
    match = re.fullmatch(r"/(?P<path>.+?)/-/merge_requests/(?P<iid>\d+)(/.*)?", parsed.path)
    if not match or not parsed.hostname:
        return None
    return parsed.hostname, match.group("path"), int(match.group("iid"))


def map_detailed_merge_status(value):
    return DETAILED_MERGE_STATUS_MAP.get(value, UNKNOWN_MERGE_STATUS)


def normalize_mr(mr, host):
    detailed = mr.get("detailed_merge_status")
    if not detailed:
        # ValueError, not GhCommandError: the loops would retry an old GitLab forever.
        raise ValueError(
            f"GitLab {host} has no detailed_merge_status; GitLab 15.6 or newer is required"
        )
    mergeable, merge_state_status, review_decision = map_detailed_merge_status(detailed)
    raw_state = str(mr.get("state") or "")
    if raw_state == "locked":
        mergeable, merge_state_status = "UNKNOWN", "UNKNOWN"
    elif mr.get("draft") or mr.get("work_in_progress"):
        merge_state_status = "DRAFT"
    web_url = str(mr.get("web_url") or "")
    parsed = parse_mr_url(web_url)
    pipeline = mr.get("head_pipeline") if isinstance(mr.get("head_pipeline"), dict) else {}
    pr = {
        "number": int(mr["iid"]),
        "url": web_url,
        "repo": parsed[1] if parsed else str((mr.get("references") or {}).get("full") or "").split("!")[0],
        "host": host,
        "project_id": mr.get("project_id"),
        "pipeline_project_id": pipeline.get("project_id") or mr.get("source_project_id") or mr.get("project_id"),
        "head_sha": str(mr.get("sha") or ""),
        "head_branch": str(mr.get("source_branch") or ""),
        "base_branch": str(mr.get("target_branch") or ""),
        "merge_commit_sha": str(mr.get("merge_commit_sha") or ""),
        "squash_commit_sha": str(mr.get("squash_commit_sha") or ""),
        # A squash merge creates a new commit, so the MR head is not on the target
        # branch even when the project only allows fast-forward merges.
        "squash": bool(mr.get("squash") or mr.get("squash_on_merge")),
        "state": MR_STATE_MAP.get(raw_state, raw_state.upper()),
        "merged": raw_state == "merged",
        "closed": raw_state not in ("opened", "locked"),
        "mergeable": mergeable,
        "merge_state_status": merge_state_status,
        "review_decision": review_decision,
        "detailed_merge_status": str(detailed),
        "labels": [str(label) for label in mr.get("labels") or [] if label],
    }
    if merge_state_status in core.MERGE_BEHIND_STATES:
        # `need_rebase` means GitLab refuses to merge until rebased (fast-forward/semi-linear).
        pr["up_to_date_required"] = True
    return pr


def resolve_mr(pr_spec, repo_override=None):
    """Return (pr dict, raw MR payload)."""
    parsed = parse_mr_url(pr_spec)
    if parsed is None:
        if pr_spec != "auto" and not re.fullmatch(r"\d+", str(pr_spec)):
            raise ValueError("--pr must be 'auto', an MR IID, or an MR URL")
        cmd = ["mr", "view"]
        if pr_spec != "auto":
            cmd.append(str(pr_spec))
        if repo_override:
            cmd.extend(["-R", repo_override])
        cmd.extend(["-F", "json"])
        view = glab_json(cmd)
        parsed = parse_mr_url((view or {}).get("web_url") if isinstance(view, dict) else None)
        if parsed is None:
            raise core.GhCommandError("Unable to determine the MR URL from `glab mr view`")
    host, path, iid = parsed
    mr = glab_api(host, f"projects/{project_ref(path)}/merge_requests/{iid}")
    if not isinstance(mr, dict):
        raise core.GhCommandError("Unexpected MR payload from the GitLab API")
    return normalize_mr(mr, host), mr


def default_state_file_for_mr(pr):
    host = re.sub(r"[^A-Za-z0-9._-]", "-", pr["host"])
    return Path(f"/tmp/babysit-mr-{host}-{pr['repo'].replace('/', '-')}-mr{pr['number']}.json")


# --- CI: jobs become checks -------------------------------------------------------


def job_bucket(job, pipeline_status):
    """GitLab job status -> the `bucket` that core.summarize_checks reads. None: ignore."""
    status = str(job.get("status") or "")
    if status == "success":
        return "pass"
    if status == "failed":
        # GitLab shows an allowed failure as a warning; the pipeline still passes.
        return None if job.get("allow_failure") else "fail"
    if status == "canceled":
        return "cancel"
    if status == "skipped":
        return None
    if status == "manual":
        # A blocking manual job waits for a person; an optional one never blocks.
        return None if job.get("allow_failure") else "skipping"
    if status == "created" and pipeline_status in PIPELINE_SETTLED_STATUSES:
        return None  # waits behind a manual gate and never starts on its own
    # created, pending, running, preparing, scheduled, ... and unknown values fail closed.
    return "pending"


def head_pipeline_for(pr, mr):
    """The MR's head pipeline, or None when it belongs to an older push."""
    pipeline = mr.get("head_pipeline")
    if not isinstance(pipeline, dict) or not pipeline.get("id"):
        return None
    pipeline_sha = str(pipeline.get("sha") or "")
    if pipeline_sha == pr["head_sha"]:
        return pipeline
    if not MERGE_REF_PATTERN.match(str(pipeline.get("ref") or "")):
        return None
    # A merged-results or merge-train pipeline tests a merge commit. Right after a push,
    # head_pipeline can still be the one for the previous head, so the merge commit must
    # have the current head as a parent.
    commit = glab_api(
        pr["host"], f"projects/{project_ref(pr['pipeline_project_id'])}/repository/commits/{pipeline_sha}"
    )
    if not isinstance(commit, dict) or pr["head_sha"] not in (commit.get("parent_ids") or []):
        return None
    return pipeline


def get_mr_checks(pr, mr):
    """Return (checks, failed_runs) for the MR head pipeline."""
    pipeline = head_pipeline_for(pr, mr)
    if pipeline is None:
        return [], []
    host = pr["host"]
    base = f"projects/{project_ref(pr['pipeline_project_id'])}/pipelines/{pipeline['id']}"
    jobs = [(job, False) for job in glab_api_list(host, f"{base}/jobs")]
    jobs += [(job, True) for job in glab_api_list(host, f"{base}/bridges")]
    pipeline_status = str(pipeline.get("status") or "")

    checks = []
    failed_runs = []
    for job, is_bridge in jobs:
        bucket = job_bucket(job, pipeline_status)
        if bucket is None:
            continue
        name = str(job.get("name") or "")
        stage = str(job.get("stage") or "")
        checks.append(
            {
                "name": name,
                "state": str(job.get("status") or ""),
                "bucket": bucket,
                "link": str(job.get("web_url") or ""),
                "workflow": stage,
                "startedAt": job.get("started_at") or "",
                "completedAt": job.get("finished_at") or "",
            }
        )
        if bucket in ("fail", "cancel"):
            failed_runs.append(
                {
                    "run_id": job.get("id"),
                    "workflow_name": name,
                    "stage": stage,
                    "status": str(job.get("status") or ""),
                    "conclusion": str(job.get("status") or ""),
                    "html_url": str(job.get("web_url") or ""),
                    # A bridge reruns a whole downstream pipeline: never automatic.
                    "retry_eligible": (not is_bridge)
                    and (core.is_retry_eligible_workflow_name(name) or core.is_retry_eligible_workflow_name(stage)),
                }
            )
    if pipeline_status in ("failed", "canceled") and not failed_runs:
        checks.append(
            {
                "name": "pipeline",
                "state": pipeline_status,
                "bucket": "cancel" if pipeline_status == "canceled" else "fail",
                "link": str(pipeline.get("web_url") or ""),
                "workflow": "pipeline",
                "startedAt": pipeline.get("started_at") or "",
                "completedAt": pipeline.get("finished_at") or "",
            }
        )
        failed_runs.append(
            {
                "run_id": pipeline.get("id"),
                "workflow_name": "pipeline",
                "stage": "pipeline",
                "status": pipeline_status,
                "conclusion": pipeline_status,
                "html_url": str(pipeline.get("web_url") or ""),
                "retry_eligible": False,
            }
        )
    elif (
        pipeline_status
        and pipeline_status not in PIPELINE_SETTLED_STATUSES
        and not any(c["bucket"] == "pending" for c in checks)
    ):
        checks.append(
            {
                "name": "pipeline",
                "state": pipeline_status,
                "bucket": "pending",
                "link": str(pipeline.get("web_url") or ""),
                "workflow": "pipeline",
                "startedAt": pipeline.get("started_at") or "",
                "completedAt": pipeline.get("finished_at") or "",
            }
        )
    failed_runs.sort(key=lambda run: (run["workflow_name"], str(run["run_id"])))
    return checks, failed_runs


def retry_job(pr, run_id):
    """Replacement for core.rerun_failed_run: retry one failed GitLab job."""
    glab_api(pr["host"], f"projects/{project_ref(pr['pipeline_project_id'])}/jobs/{run_id}/retry", method="POST")


# --- Post-merge pipeline ------------------------------------------------------------


def collect_merge_pipeline_target(args):
    """Resolve the MR and the base-branch commit to watch after a merge."""
    pr, _mr = resolve_mr(args.pr, repo_override=args.repo)
    candidates = [pr.get("merge_commit_sha"), pr.get("squash_commit_sha")]
    # Only a fast-forward merge moves the target branch to the MR head itself. With
    # any other method the merge commit SHA can lag the merge, so wait for it rather
    # than watch the head. A squash on a fast-forward project also creates a new
    # commit, so the head fallback does not apply there either.
    if (
        pr.get("merged")
        and not any(candidates)
        and not pr.get("squash")
        and project_merge_method(pr) == "ff"
    ):
        candidates.append(pr.get("head_sha"))
    return core.merge_pipeline_target_from_pr(pr, candidates)


def project_merge_method(pr):
    project = glab_api(pr["host"], f"projects/{project_ref(pr.get('project_id') or pr['repo'])}")
    return str(project.get("merge_method") or "") if isinstance(project, dict) else ""


def newest_pipeline(pipelines):
    candidates = [
        pipeline for pipeline in pipelines or []
        if isinstance(pipeline, dict) and pipeline.get("id") is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pipeline: pipeline.get("id") or 0)


def pipeline_jobs(host, project_id, pipeline_id):
    """Jobs and bridge jobs of one pipeline, as one list."""
    base = f"projects/{project_ref(project_id)}/pipelines/{pipeline_id}"
    jobs = glab_api_list(host, f"{base}/jobs")
    jobs += glab_api_list(host, f"{base}/bridges")
    return [job for job in jobs if isinstance(job, dict)]


def first_pipeline_job_name(jobs, statuses, require_blocking=False):
    matching = [
        job for job in jobs
        if str(job.get("status") or "") in statuses
        and (not require_blocking or not job.get("allow_failure"))
    ]
    if not matching:
        return "pipeline"
    matching.sort(key=lambda job: (job.get("id") or 0, str(job.get("name") or "")))
    return str(matching[0].get("name") or "") or "pipeline"


def pipeline_job_name_for(host, project_id, pipeline_id, statuses, require_blocking=False):
    """A named job when the jobs lookup works, else the generic pipeline name."""
    try:
        jobs = pipeline_jobs(host, project_id, pipeline_id)
    except core.GhCommandError:
        return "pipeline"
    return first_pipeline_job_name(jobs, statuses, require_blocking=require_blocking)


def find_merge_pipelines(host, project_id, merge_sha, base_branch):
    """Every base-branch pipeline for the merge commit, or an empty list."""
    endpoint = (
        f"projects/{project_ref(project_id)}/pipelines"
        f"?sha={quote(merge_sha, safe='')}&ref={quote(base_branch, safe='')}"
    )
    matching = [
        item for item in glab_api_list(host, endpoint)
        if isinstance(item, dict) and str(item.get("ref") or "") == base_branch
    ]
    if matching:
        return matching

    # Some instances ignore `ref` when `sha` is present. Filter the SHA-only
    # answer ourselves rather than watching a pipeline from another ref.
    endpoint = f"projects/{project_ref(project_id)}/pipelines?sha={quote(merge_sha, safe='')}"
    return [
        item for item in glab_api_list(host, endpoint)
        if isinstance(item, dict) and str(item.get("ref") or "") == base_branch
    ]


def find_merge_pipeline(host, project_id, merge_sha, base_branch):
    """The newest base-branch pipeline for the merge commit, or None."""
    return newest_pipeline(find_merge_pipelines(host, project_id, merge_sha, base_branch))


def merge_pipeline_signature(pipelines):
    """Identity of every observed pipeline, so a late pipeline restarts the hold."""
    return tuple(sorted(
        (
            str(pipeline.get("id") or ""),
            str(pipeline.get("status") or ""),
        )
        for pipeline in pipelines or []
        if isinstance(pipeline, dict)
    ))


def get_merge_pipeline_status(target):
    """One post-merge state for the base-branch pipeline on the merge commit."""
    pr = target["pr"]
    host = pr["host"]
    project_id = pr.get("project_id") or pr.get("pipeline_project_id")
    if not project_id:
        raise ValueError("unable to determine the GitLab project for the merged merge request")

    pipelines = find_merge_pipelines(host, project_id, target["merge_sha"], target["base_branch"])
    signature = merge_pipeline_signature(pipelines)
    pipeline = newest_pipeline(pipelines)
    if pipeline is None:
        return {"state": "missing", "runs": [], "signature": signature}

    status = str(pipeline.get("status") or "")
    if status == "success":
        return {"state": "success", "pipeline": pipeline, "signature": signature}
    if status == "failed":
        return {
            "state": "failed",
            "job": pipeline_job_name_for(
                host, project_id, pipeline["id"], {"failed"}, require_blocking=True
            ),
            "pipeline": pipeline,
            "signature": signature,
        }
    if status == "canceled":
        return {"state": "canceled", "pipeline": pipeline, "signature": signature}
    if status == "skipped":
        return {"state": "skipped", "pipeline": pipeline, "signature": signature}
    if status in ("manual", "blocked"):
        return {
            "state": "manual",
            "job": pipeline_job_name_for(
                host, project_id, pipeline["id"], {"manual"}, require_blocking=True
            ),
            "pipeline": pipeline,
            "signature": signature,
        }
    return {"state": "pending", "pipeline": pipeline, "signature": signature}


# --- Review items -------------------------------------------------------------------


def get_authenticated_username(host):
    if host not in _AUTHENTICATED_USERNAME_CACHE:
        data = glab_api(host, "user")
        if not isinstance(data, dict) or not data.get("username"):
            raise core.GhCommandError(f"Unable to determine the authenticated GitLab user on {host}")
        _AUTHENTICATED_USERNAME_CACHE[host] = str(data["username"])
    return _AUTHENTICATED_USERNAME_CACHE[host]


def _glab_api_or_none_on_404(host, endpoint):
    """Fetch an API object, treating HTTP 404 as None and re-raising any other failure."""
    try:
        return glab_api(host, endpoint)
    except core.GhCommandError as err:
        if not re.search(r"\bHTTP 404\b", str(err)):
            raise
        return None


def is_trusted_member(host, project_id, user_id):
    """Developer access or higher on the project, the GitLab match for GitHub's trusted
    OWNER/MEMBER/COLLABORATOR associations. Only a 404 means "not a member"; any other
    failure raises, so an outsider's note can never slip through a failed lookup."""
    if not user_id or not project_id:
        return False
    key = (host, str(project_id), str(user_id))
    if key not in _MEMBER_ACCESS_CACHE:
        member = _glab_api_or_none_on_404(host, f"projects/{project_ref(project_id)}/members/all/{user_id}")
        _MEMBER_ACCESS_CACHE[key] = int((member or {}).get("access_level") or 0)
    return _MEMBER_ACCESS_CACHE[key] >= TRUSTED_ACCESS_LEVEL


def is_verified_gitlab_bot(host, author):
    """True when the username matches a GitLab bot pattern AND GitLab marks the account `bot: true`."""
    username = str(author.get("username") or "")
    if not is_gitlab_bot(username):
        return False
    if isinstance(author.get("bot"), bool):
        return author["bot"]
    user_id = author.get("id")
    if not user_id:
        return False
    key = (host, str(user_id))
    if key not in _USER_IS_BOT_CACHE:
        user = _glab_api_or_none_on_404(host, f"users/{user_id}")
        _USER_IS_BOT_CACHE[key] = bool((user or {}).get("bot"))
    return _USER_IS_BOT_CACHE[key]


def author_role(pr, note, authenticated_username):
    """"self", "reviewer", or None for notes the watcher ignores (outsiders, other bots)."""
    author = note.get("author") if isinstance(note.get("author"), dict) else {}
    username = str(author.get("username") or "")
    if not username:
        return None
    if username == authenticated_username:
        return "self"
    if is_verified_gitlab_bot(pr["host"], author):
        return "reviewer" if is_actionable_bot(username) else None
    return "reviewer" if is_trusted_member(pr["host"], pr["project_id"], author.get("id")) else None


def is_gitlab_bot(username):
    return bool(BOT_USERNAME_PATTERN.match(str(username or "")))


def is_actionable_bot(username):
    lower = str(username or "").lower()
    return any(keyword.lower() in lower for keyword in core.CONFIG["review_bot_login_keywords"])


def body_hash(body):
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()[:16]


def normalize_note(note, discussion_id, mr_url):
    position = note.get("position") if isinstance(note.get("position"), dict) else {}
    new_line = position.get("new_line")
    old_line = position.get("old_line")
    if new_line is not None:
        line, side = new_line, "new"
        path = position.get("new_path") or position.get("old_path")
    elif old_line is not None:
        line, side = old_line, "old"
        path = position.get("old_path") or position.get("new_path")
    else:
        line, side = None, None
        path = position.get("new_path") or position.get("old_path")
    return {
        "kind": "review_comment" if note.get("type") == "DiffNote" else "issue_comment",
        "id": str(note.get("id") or ""),
        "discussion_id": str(discussion_id or ""),
        "author": str((note.get("author") or {}).get("username") or ""),
        "created_at": str(note.get("created_at") or ""),
        "updated_at": str(note.get("updated_at") or ""),
        "body": str(note.get("body") or ""),
        "path": path,
        "line": line,
        "side": side,
        "commit_id": position.get("head_sha"),
        "url": f"{mr_url}#note_{note.get('id')}",
    }


def fetch_review_items(pr, state, authenticated_username):
    """Return (new_items, blocking_items) from the MR discussions.

    New items are compared by body hash, not `updated_at`: resolving a thread touches
    `updated_at` and would otherwise bring back a note that was already handled.
    """
    discussions = glab_api_list(
        pr["host"], f"projects/{project_ref(pr['project_id'])}/merge_requests/{pr['number']}/discussions"
    )
    seen_ids = {str(x) for x in state.get("seen_review_comment_ids") or []}
    seen_hashes = {str(k): str(v) for k, v in (state.get("seen_review_comment_updated_at") or {}).items()}

    new_items = []
    blocking_items = []
    for discussion in discussions:
        notes = [n for n in discussion.get("notes") or [] if isinstance(n, dict) and not n.get("system")]
        discussion_id = discussion.get("id")
        unresolved = any(n.get("resolvable") and not n.get("resolved") for n in notes)
        blocking_added = False
        for note in notes:
            role = author_role(pr, note, authenticated_username)
            if role is None:
                continue
            item = normalize_note(note, discussion_id, pr["url"])
            if unresolved and not blocking_added:
                # An open thread blocks once the user, a trusted member, or a review bot
                # wrote in it, as on GitHub.
                blocking_items.append(item)
                blocking_added = True
            if role == "self" or not item["id"]:
                continue
            digest = body_hash(item["body"])
            if item["id"] in seen_ids and seen_hashes.get(item["id"]) == digest:
                continue
            new_items.append(item)
            seen_ids.add(item["id"])
            seen_hashes[item["id"]] = digest

    order = lambda item: (item["created_at"], item["kind"], item["id"])  # noqa: E731
    new_items.sort(key=order)
    blocking_items.sort(key=order)
    state["seen_review_comment_ids"] = sorted(seen_ids)
    state["seen_review_comment_updated_at"] = seen_hashes
    state["last_review_poll_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return new_items, blocking_items


# --- Snapshot -------------------------------------------------------------------------


def collect_snapshot(args):
    """GitLab version of core.collect_snapshot, with the same output shape."""
    pr, mr = resolve_mr(args.pr, repo_override=args.repo)
    state_path = Path(args.state_file) if args.state_file else default_state_file_for_mr(pr)
    state, _fresh_state = core.load_state(state_path)
    if not state.get("started_at"):
        state["started_at"] = int(time.time())

    now = int(time.time())
    head_sha = pr["head_sha"]
    core.reset_state_for_new_head_sha(state, head_sha)

    checks, failed_runs = get_mr_checks(pr, mr)
    checks_summary = core.summarize_checks(checks)
    # Jobs waiting for earlier stages or a delay timer have not started, so they cannot hang.
    hung_candidates = [check for check in checks if check["state"] not in WAITING_JOB_STATUSES]
    pending_first_seen = core.update_pending_checks_first_seen(state, hung_candidates, now)
    hung_checks = core.hung_checks_from_checks(hung_candidates, pending_first_seen)

    # Without the username the agent's own replies would come back as new items, so a
    # failed lookup fails the snapshot and the loops retry.
    username = get_authenticated_username(pr["host"])
    new_review_items, blocking_review_items = fetch_review_items(pr, state, username)

    checks_terminal_elapsed = core.update_checks_terminal_elapsed(
        state, head_sha, checks_summary["all_terminal"], now
    )
    retries_used = core.current_retry_count(state, head_sha)
    actions = core.recommend_actions(
        pr,
        checks_summary,
        failed_runs,
        new_review_items,
        hung_checks,
        retries_used,
        args.max_flaky_retries,
        checks_terminal_elapsed=checks_terminal_elapsed,
        blocking_review_items=blocking_review_items,
        codex_gate=None,
        pr_af_gate=None,
    )

    state["pr"] = {"repo": pr["repo"], "number": pr["number"], "host": pr["host"]}
    state["last_seen_head_sha"] = head_sha
    state["last_snapshot_at"] = now
    core.save_state(state_path, state)

    snapshot = {
        "pr": pr,
        "checks": checks_summary,
        "failed_runs": failed_runs,
        "codex_gate": None,
        "pr_af_gate": None,
        "hung_checks": hung_checks,
        "new_review_items": new_review_items,
        "blocking_review_items": blocking_review_items,
        "actions": actions,
        "retry_state": {
            "current_sha_retries_used": retries_used,
            "max_flaky_retries": args.max_flaky_retries,
        },
        "checks_terminal_elapsed_seconds": checks_terminal_elapsed,
    }
    return snapshot, state_path


_core_activate_config = core.activate_config


def activate_config(argv):
    """Load the config, then switch off the GitHub-only Codex and PR-AF gates."""
    path = _core_activate_config(argv)
    config = core.CONFIG
    # `codex.enabled` is on by default, so only the explicit opt-ins get a warning.
    if config["codex"]["required"] or config["pr_af"]["enabled"]:
        sys.stderr.write("glab_mr_watch.py: codex and pr_af do not apply to GitLab; turned off\n")
    config["codex"]["enabled"] = False
    config["codex"]["required"] = False
    config["pr_af"]["enabled"] = False
    return path


def main():
    core.collect_snapshot = collect_snapshot
    core.rerun_failed_run = retry_job
    core.activate_config = activate_config
    core.collect_merge_pipeline_target = collect_merge_pipeline_target
    core.get_merge_pipeline_status = get_merge_pipeline_status
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
