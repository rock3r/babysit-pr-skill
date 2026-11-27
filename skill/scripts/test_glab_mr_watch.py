import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

SCRIPT_DIR = os.path.dirname(__file__)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import gh_pr_watch as core
import glab_mr_watch as glab

HOST = "gitlab.example.com"
PATH = "group/sub/project"
ENC = "group%2Fsub%2Fproject"
MR_URL = f"https://{HOST}/{PATH}/-/merge_requests/7"
SHA = "a" * 40


def configured(overrides=None):
    config, _warnings = core.build_config(overrides or {})
    return patch.object(core, "CONFIG", config)


def job(name, status, allow_failure=False, stage="build", job_id=None):
    return {
        "id": job_id or abs(hash(name)) % 10000,
        "name": name,
        "stage": stage,
        "status": status,
        "allow_failure": allow_failure,
        "web_url": f"https://{HOST}/{PATH}/-/jobs/{name}",
        "started_at": None,
        "finished_at": None,
    }


def mr_payload(detailed="mergeable", state="opened", pipeline_status="success", pipeline_sha=SHA, pipeline=True):
    payload = {
        "iid": 7,
        "project_id": 11,
        "source_project_id": 11,
        "state": state,
        "web_url": MR_URL,
        "sha": SHA,
        "source_branch": "feature",
        "target_branch": "main",
        "detailed_merge_status": detailed,
        "labels": ["backend"],
        "head_pipeline": None,
    }
    if pipeline:
        payload["head_pipeline"] = {
            "id": 99,
            "project_id": 11,
            "sha": pipeline_sha,
            "ref": "feature",
            "status": pipeline_status,
        }
    return payload


def note(note_id, author, body="please fix", resolvable=False, resolved=False, diff=False, system=False,
         updated_at="2026-01-01T00:00:00Z"):
    return {
        "id": note_id,
        "type": "DiffNote" if diff else None,
        "body": body,
        "author": {"username": author, "id": user_id(author)},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": updated_at,
        "system": system,
        "resolvable": resolvable,
        "resolved": resolved,
        "position": {"new_path": "src/a.py", "new_line": 3, "head_sha": SHA} if diff else None,
    }


def user_id(username):
    return sum(ord(ch) for ch in username)


def discussion(disc_id, *notes):
    return {"id": disc_id, "notes": list(notes)}


class FakeGlab:
    """Stands in for `glab`: answers by endpoint, records every call."""

    def __init__(self, mr, jobs=(), bridges=(), discussions=(), username="me", members=None, commits=None,
                 failing=(), bots=(), non_bots=()):
        self.calls = []
        self.failing = set(failing)
        self.routes = {
            f"projects/{ENC}/merge_requests/7": mr,
            "projects/11/pipelines/99/jobs": list(jobs),
            "projects/11/pipelines/99/bridges": list(bridges),
            "projects/11/merge_requests/7/discussions": list(discussions),
            "user": {"username": username},
        }
        # Trusted Developer by default; pass members to change access levels.
        for name, level in (members if members is not None else {"alice": 30}).items():
            self.routes[f"projects/11/members/all/{user_id(name)}"] = {"access_level": level}
        for name in bots:
            self.routes[f"users/{user_id(name)}"] = {"id": user_id(name), "username": name, "bot": True}
        for name in non_bots:
            self.routes[f"users/{user_id(name)}"] = {"id": user_id(name), "username": name, "bot": False}
        for sha, parents in (commits or {}).items():
            self.routes[f"projects/11/repository/commits/{sha}"] = {"id": sha, "parent_ids": parents}

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["mr", "view"]:
            return json.dumps({"web_url": MR_URL})
        assert args[:3] == ["api", "--hostname", HOST], args
        endpoint = args[-1]
        if "-X" in args:
            return "{}"
        path, _, query = endpoint.partition("?")
        if path in self.failing:
            raise core.GhCommandError(f"GitLab CLI command failed: glab api {path}\nstderr: glab: 500 (HTTP 500)")
        if path not in self.routes:
            raise core.GhCommandError(f"GitLab CLI command failed: glab api {path}\nstderr: glab: 404 Not Found (HTTP 404)")
        payload = self.routes[path]
        if isinstance(payload, list) and query:
            params = parse_qs(query)
            if "page" in params:
                page = int(params["page"][0])
                per_page = int(params.get("per_page", [glab.PAGE_SIZE])[0])
                start = (page - 1) * per_page
                payload = payload[start : start + per_page]
        return json.dumps(payload)


class GlabTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, "state.json")
        glab._AUTHENTICATED_USERNAME_CACHE.clear()
        glab._MEMBER_ACCESS_CACHE.clear()
        glab._USER_IS_BOT_CACHE.clear()
        cfg = configured()
        cfg.start()
        self.addCleanup(cfg.stop)

    def args(self, **overrides):
        values = {"pr": MR_URL, "repo": None, "state_file": self.state_file, "max_flaky_retries": 3}
        values.update(overrides)
        return SimpleNamespace(**values)

    def seed_state(self, terminal_seconds_ago=None, seen=(), pending_since=None):
        now = int(time.time())
        state, _ = core.load_state(core.Path(self.state_file))
        state["last_snapshot_at"] = now
        state["last_seen_head_sha"] = SHA
        if terminal_seconds_ago is not None:
            state["checks_terminal_sha"] = SHA
            state["checks_went_terminal_at"] = now - terminal_seconds_ago
        for key, seconds_ago in (pending_since or {}).items():
            state["pending_checks_first_seen_at"][key] = now - seconds_ago
        for note_id, body in seen:
            state["seen_review_comment_ids"].append(str(note_id))
            state["seen_review_comment_updated_at"][str(note_id)] = glab.body_hash(body)
        core.save_state(core.Path(self.state_file), state)

    def snapshot(self, fake, **arg_overrides):
        with patch.object(glab, "run_glab", fake):
            snapshot, _path = glab.collect_snapshot(self.args(**arg_overrides))
        return snapshot


class DetailedMergeStatusTests(unittest.TestCase):
    def test_every_documented_row(self):
        rows = {
            "mergeable": ("MERGEABLE", "CLEAN", ""),
            "conflict": ("CONFLICTING", "DIRTY", ""),
            "need_rebase": ("MERGEABLE", "BEHIND", ""),
            "draft_status": ("MERGEABLE", "DRAFT", ""),
            "draft": ("MERGEABLE", "DRAFT", ""),
            "not_approved": ("MERGEABLE", "BLOCKED", "REVIEW_REQUIRED"),
            "requested_changes": ("MERGEABLE", "BLOCKED", "CHANGES_REQUESTED"),
            "checking": ("UNKNOWN", "UNKNOWN", ""),
            "unchecked": ("UNKNOWN", "UNKNOWN", ""),
            "preparing": ("UNKNOWN", "UNKNOWN", ""),
            "approvals_syncing": ("UNKNOWN", "UNKNOWN", ""),
        }
        for value, expected in rows.items():
            self.assertEqual(glab.map_detailed_merge_status(value), expected, value)

    def test_unknown_and_blocking_values_fail_closed(self):
        for value in ("ci_must_pass", "discussions_not_resolved", "not_open", "security_policy_x", "future_value"):
            self.assertEqual(glab.map_detailed_merge_status(value), ("MERGEABLE", "BLOCKED", ""), value)

    def test_missing_field_is_a_version_error(self):
        payload = mr_payload()
        del payload["detailed_merge_status"]
        with self.assertRaisesRegex(ValueError, "GitLab 15.6 or newer"):
            glab.normalize_mr(payload, HOST)

    def test_states(self):
        with configured():
            merged = glab.normalize_mr(mr_payload(state="merged"), HOST)
            closed = glab.normalize_mr(mr_payload(state="closed"), HOST)
            opened = glab.normalize_mr(mr_payload(), HOST)
        self.assertEqual((merged["state"], merged["merged"], merged["closed"]), ("MERGED", True, True))
        self.assertEqual((closed["state"], closed["merged"], closed["closed"]), ("CLOSED", False, True))
        self.assertEqual((opened["state"], opened["merged"], opened["closed"]), ("OPEN", False, False))
        self.assertEqual(opened["repo"], PATH)
        self.assertEqual(opened["labels"], ["backend"])

    def test_need_rebase_always_requires_up_to_date(self):
        with configured():
            self.assertTrue(glab.normalize_mr(mr_payload("need_rebase"), HOST)["up_to_date_required"])
        with configured({"require_up_to_date": False}):
            self.assertTrue(glab.normalize_mr(mr_payload("need_rebase"), HOST)["up_to_date_required"])

    def test_draft_and_wip_flags_override_detailed_merge_status(self):
        for flag in ("draft", "work_in_progress"):
            payload = mr_payload("not_approved")
            payload[flag] = True
            with configured():
                self.assertEqual(glab.normalize_mr(payload, HOST)["merge_state_status"], "DRAFT")


class JobBucketTests(unittest.TestCase):
    def test_every_row(self):
        rows = [
            (job("a", "success"), "running", "pass"),
            (job("a", "failed"), "failed", "fail"),
            (job("a", "failed", allow_failure=True), "success", None),
            (job("a", "canceled"), "canceled", "cancel"),
            (job("a", "skipped"), "success", None),
            (job("a", "manual", allow_failure=True), "success", None),
            (job("a", "manual"), "manual", "skipping"),
            (job("a", "created"), "manual", None),
            (job("a", "created"), "failed", None),
            (job("a", "created"), "running", "pending"),
            (job("a", "pending"), "running", "pending"),
            (job("a", "running"), "running", "pending"),
            (job("a", "waiting_for_resource"), "running", "pending"),
            (job("a", "brand_new_status"), "running", "pending"),
        ]
        for payload, pipeline_status, expected in rows:
            self.assertEqual(glab.job_bucket(payload, pipeline_status), expected, (payload["status"], pipeline_status))


class MrUrlTests(unittest.TestCase):
    def test_self_hosted_nested_group(self):
        self.assertEqual(
            glab.parse_mr_url("https://git.corp.example/a/b/c/-/merge_requests/42/diffs"),
            ("git.corp.example", "a/b/c", 42),
        )

    def test_rejects_port_and_userinfo(self):
        with self.assertRaisesRegex(ValueError, "port"):
            glab.parse_mr_url("https://git.corp.example:8443/a/b/c/-/merge_requests/42/diffs")
        with self.assertRaisesRegex(ValueError, "userinfo"):
            glab.parse_mr_url("https://gitlab.com@evil.example/a/b/-/merge_requests/42")

    def test_rejects_github_and_garbage(self):
        self.assertIsNone(glab.parse_mr_url("https://github.com/o/r/pull/1"))
        self.assertIsNone(glab.parse_mr_url("12"))

    def test_iid_goes_through_glab_mr_view(self):
        fake = FakeGlab(mr_payload())
        with configured(), patch.object(glab, "run_glab", fake):
            pr, _mr = glab.resolve_mr("7", repo_override=PATH)
        self.assertEqual(fake.calls[0], ["mr", "view", "7", "-R", PATH, "-F", "json"])
        self.assertEqual((pr["host"], pr["repo"], pr["number"]), (HOST, PATH, 7))

    def test_state_file_name(self):
        pr = {"host": HOST, "repo": PATH, "number": 7}
        self.assertEqual(
            str(glab.default_state_file_for_mr(pr)),
            f"/tmp/babysit-mr-{HOST}-group-sub-project-mr7.json",
        )


class SnapshotTests(GlabTestCase):
    def test_green_mergeable_after_grace_is_ready(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(mr_payload(), jobs=[job("test", "success"), job("lint", "success")])
        snapshot = self.snapshot(fake)
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])
        self.assertEqual(snapshot["pr"]["detailed_merge_status"], "mergeable")
        self.assertIsNone(snapshot["codex_gate"])

    def test_green_inside_grace_period_waits(self):
        fake = FakeGlab(mr_payload(), jobs=[job("test", "success")])
        self.assertEqual(self.snapshot(fake)["actions"], ["idle"])

    def test_green_branch_pipeline_with_skipped_and_optional_manual_jobs(self):
        self.seed_state(terminal_seconds_ago=120)
        jobs = [
            job("build", "success"),
            job("deploy", "success", stage="deploy"),
            job("distribute", "skipped"),
            job("apk", "manual", allow_failure=True),
            job("flaky_lint", "failed", allow_failure=True),
        ]
        snapshot = self.snapshot(FakeGlab(mr_payload(), jobs=jobs))
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])
        self.assertEqual(snapshot["checks"]["check_count"], 2)

    def test_pipeline_waiting_on_blocking_manual_job(self):
        self.seed_state(terminal_seconds_ago=120)
        jobs = [
            job("build_branch", "manual", stage="build"),
            job("deploy_branch", "created", stage="deploy"),
            job("apk", "manual", allow_failure=True),
        ]
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="manual"), jobs=jobs))
        self.assertEqual(snapshot["actions"], ["diagnose_skipping_checks"])
        self.assertEqual(snapshot["checks"]["pending_count"], 0)
        self.assertEqual(snapshot["hung_checks"], [])

    def test_blocking_manual_job_listed_as_expected_skip_is_ignored(self):
        self.seed_state(terminal_seconds_ago=120)
        jobs = [job("build", "success"), job("build_branch", "manual")]
        with configured({"expected_skipped_checks": ["build_branch"]}):
            snapshot = self.snapshot(FakeGlab(mr_payload(), jobs=jobs))
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])

    def test_need_rebase_reports_branch_behind(self):
        fake = FakeGlab(mr_payload("need_rebase"), jobs=[job("test", "success")])
        self.assertIn("diagnose_branch_behind", self.snapshot(fake)["actions"])

    def test_conflict_and_draft(self):
        self.seed_state(terminal_seconds_ago=120)
        conflict = self.snapshot(FakeGlab(mr_payload("conflict"), jobs=[job("test", "success")]))
        self.assertIn("diagnose_merge_conflict", conflict["actions"])
        draft = self.snapshot(FakeGlab(mr_payload("draft_status"), jobs=[job("test", "success")]))
        self.assertEqual(draft["actions"], ["stop_draft_pr"])

    def test_unknown_merge_status_never_ready(self):
        self.seed_state(terminal_seconds_ago=120)
        snapshot = self.snapshot(FakeGlab(mr_payload("ci_still_running"), jobs=[job("test", "success")]))
        self.assertNotIn("stop_ready_to_merge", snapshot["actions"])
        self.assertEqual(snapshot["actions"], ["diagnose_merge_blocked"])

    def test_pending_job_never_ready(self):
        self.seed_state(terminal_seconds_ago=120)
        jobs = [job("test", "success"), job("e2e", "running")]
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="running"), jobs=jobs))
        self.assertEqual(snapshot["actions"], ["idle"])

    def test_jobs_waiting_for_earlier_stages_never_hang(self):
        link = f"https://{HOST}/{PATH}/-/jobs/"
        self.seed_state(pending_since={f"deploy|deploy|{link}deploy": 3600, f"later|deploy|{link}later": 3600})
        jobs = [job("build", "running"), job("deploy", "created", stage="deploy"),
                job("later", "scheduled", stage="deploy")]
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="running"), jobs=jobs))
        self.assertEqual(snapshot["hung_checks"], [])
        self.assertEqual(snapshot["actions"], ["idle"])

    def test_job_stuck_pending_still_hangs(self):
        link = f"https://{HOST}/{PATH}/-/jobs/"
        self.seed_state(pending_since={f"build|build|{link}build": 3600})
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="pending"), jobs=[job("build", "pending")]))
        self.assertEqual([check["name"] for check in snapshot["hung_checks"]], ["build"])
        self.assertIn("diagnose_hung_check", snapshot["actions"])

    def test_failed_user_lookup_fails_the_snapshot(self):
        fake = FakeGlab(mr_payload(), jobs=[job("test", "success")], failing={"user"})
        with self.assertRaises(core.GhCommandError):
            self.snapshot(fake)

    def test_failed_job_is_non_retryable(self):
        jobs = [job("unit", "failed", job_id=5), job("lint", "success")]
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="failed"), jobs=jobs))
        self.assertEqual(snapshot["actions"], ["diagnose_ci_failure", "stop_non_retryable_failure"])
        self.assertEqual(snapshot["failed_runs"][0]["run_id"], 5)
        self.assertFalse(snapshot["failed_runs"][0]["retry_eligible"])

    def test_canceled_job_counts_as_failure(self):
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="canceled"), jobs=[job("unit", "canceled")]))
        self.assertIn("diagnose_ci_failure", snapshot["actions"])

    def test_failed_e2e_stage_retries_through_the_jobs_api(self):
        jobs = [job("android_ui", "failed", stage="e2e", job_id=5), job("unit", "success")]
        fake = FakeGlab(mr_payload(pipeline_status="failed"), jobs=jobs)
        self.assertEqual(self.snapshot(fake)["actions"], ["diagnose_ci_failure", "retry_failed_checks"])

        with patch.object(glab, "run_glab", fake), patch.object(core, "collect_snapshot", glab.collect_snapshot), \
                patch.object(core, "rerun_failed_run", glab.retry_job):
            result = core.retry_failed_now(self.args())
        self.assertEqual(result["reason"], "rerun_triggered")
        self.assertIn(["api", "--hostname", HOST, "-X", "POST", "projects/11/jobs/5/retry"], fake.calls)

    def test_failed_bridge_is_never_retry_eligible(self):
        bridges = [job("e2e_downstream", "failed", stage="e2e", job_id=6)]
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="failed"), bridges=bridges))
        self.assertEqual(snapshot["actions"], ["diagnose_ci_failure", "stop_non_retryable_failure"])

    def test_head_pipeline_for_an_older_sha_gives_no_checks(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(mr_payload(pipeline_sha="b" * 40), jobs=[job("test", "success")])
        snapshot = self.snapshot(fake)
        self.assertEqual(snapshot["checks"]["check_count"], 0)
        self.assertNotIn("stop_ready_to_merge", snapshot["actions"])
        self.assertFalse(any("pipelines/99" in call[-1] for call in fake.calls))

    def merge_ref_payload(self):
        payload = mr_payload(pipeline_sha="c" * 40)
        payload["head_pipeline"]["ref"] = "refs/merge-requests/7/merge"
        return payload

    def test_merge_ref_pipeline_of_the_current_head_counts(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(self.merge_ref_payload(), jobs=[job("test", "success")], commits={"c" * 40: ["f" * 40, SHA]})
        self.assertEqual(self.snapshot(fake)["actions"], ["stop_ready_to_merge"])

    def test_merge_ref_pipeline_of_a_previous_head_gives_no_checks(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(self.merge_ref_payload(), jobs=[job("test", "success")],
                        commits={"c" * 40: ["f" * 40, "b" * 40]})
        snapshot = self.snapshot(fake)
        self.assertEqual(snapshot["checks"]["check_count"], 0)
        self.assertNotIn("stop_ready_to_merge", snapshot["actions"])

    def test_no_pipeline_gives_no_checks(self):
        self.seed_state(terminal_seconds_ago=120)
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline=False)))
        self.assertEqual(snapshot["actions"], ["diagnose_no_checks"])

    def test_closed_mr_stops(self):
        snapshot = self.snapshot(FakeGlab(mr_payload("not_open", state="closed"), jobs=[job("test", "success")]))
        self.assertEqual(snapshot["actions"], ["stop_pr_closed"])
        self.assertFalse(snapshot["pr"]["merged"])

    def test_locked_mr_keeps_polling(self):
        self.seed_state(terminal_seconds_ago=120)
        for detailed in ("not_open", "mergeable"):
            snapshot = self.snapshot(FakeGlab(mr_payload(detailed, state="locked"), jobs=[job("test", "success")]))
            self.assertEqual(snapshot["pr"]["state"], "LOCKED")
            self.assertFalse(snapshot["pr"]["closed"])
            self.assertFalse(snapshot["pr"]["merged"])
            self.assertEqual(snapshot["actions"], ["idle"])

    def test_failed_pipeline_with_all_green_jobs_blocks_readiness(self):
        self.seed_state(terminal_seconds_ago=120)
        for status in ("failed", "canceled"):
            snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status=status), jobs=[job("test", "success")]))
            self.assertEqual(snapshot["actions"], ["diagnose_ci_failure", "stop_non_retryable_failure"])
            self.assertEqual(snapshot["checks"]["failed_count"], 1)

    def test_running_pipeline_with_all_green_jobs_stays_pending(self):
        self.seed_state(terminal_seconds_ago=120)
        snapshot = self.snapshot(FakeGlab(mr_payload(pipeline_status="running"), jobs=[job("test", "success")]))
        self.assertEqual(snapshot["actions"], ["idle"])
        self.assertEqual(snapshot["checks"]["pending_count"], 1)

    def test_glab_api_list_paginates_across_multiple_pages(self):
        self.seed_state(terminal_seconds_ago=120)
        jobs = [job(f"job{i}", "success", job_id=i) for i in range(1, 5)]
        fake = FakeGlab(mr_payload(), jobs=jobs)
        with patch.object(glab, "PAGE_SIZE", 2):
            snapshot = self.snapshot(fake)
        self.assertEqual(snapshot["checks"]["passed_count"], 4)
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])
        job_calls = [c[-1] for c in fake.calls if "/jobs?" in c[-1]]
        self.assertEqual(len(job_calls), 3)


class ReviewItemTests(GlabTestCase):
    def ready_fake(self, *discussions, username="me"):
        return FakeGlab(mr_payload(), jobs=[job("test", "success")], discussions=discussions, username=username)

    def test_new_note_then_seen(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = self.ready_fake(discussion("d1", note(1, "alice")))
        first = self.snapshot(fake)
        self.assertEqual(first["actions"], ["process_review_comment"])
        self.assertEqual(first["new_review_items"][0]["url"], f"{MR_URL}#note_1")
        second = self.snapshot(fake)
        self.assertEqual(second["new_review_items"], [])
        self.assertEqual(second["actions"], ["stop_ready_to_merge"])

    def test_edited_body_is_new_again(self):
        self.seed_state(terminal_seconds_ago=120, seen=[(1, "old text")])
        snapshot = self.snapshot(self.ready_fake(discussion("d1", note(1, "alice", body="new text"))))
        self.assertEqual([item["id"] for item in snapshot["new_review_items"]], ["1"])

    def test_resolved_thread_with_new_updated_at_is_not_new(self):
        self.seed_state(terminal_seconds_ago=120, seen=[(1, "please fix")])
        thread = discussion("d1", note(1, "alice", resolvable=True, resolved=True, diff=True,
                                       updated_at="2026-02-02T00:00:00Z"))
        snapshot = self.snapshot(self.ready_fake(thread))
        self.assertEqual(snapshot["new_review_items"], [])
        self.assertEqual(snapshot["blocking_review_items"], [])
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])

    def test_unresolved_thread_keeps_blocking_after_it_was_seen(self):
        self.seed_state(terminal_seconds_ago=120, seen=[(1, "please fix")])
        thread = discussion("d1", note(1, "alice", resolvable=True, diff=True))
        snapshot = self.snapshot(self.ready_fake(thread))
        self.assertEqual(snapshot["new_review_items"], [])
        self.assertEqual(snapshot["blocking_review_items"][0]["discussion_id"], "d1")
        self.assertEqual(snapshot["blocking_review_items"][0]["path"], "src/a.py")
        self.assertEqual(snapshot["actions"], ["process_review_comment"])

    def test_own_unresolved_thread_blocks_but_is_not_new(self):
        self.seed_state(terminal_seconds_ago=120)
        snapshot = self.snapshot(self.ready_fake(discussion("d1", note(1, "me", resolvable=True))))
        self.assertEqual(snapshot["new_review_items"], [])
        self.assertEqual(len(snapshot["blocking_review_items"]), 1)

    def test_system_notes_are_skipped(self):
        self.seed_state(terminal_seconds_ago=120)
        snapshot = self.snapshot(self.ready_fake(discussion("d1", note(1, "alice", system=True, resolvable=True))))
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])

    def test_outsider_notes_and_threads_are_ignored(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = self.ready_fake(discussion("d1", note(1, "mallory", resolvable=True, diff=True)))
        snapshot = self.snapshot(fake)
        self.assertEqual((snapshot["new_review_items"], snapshot["blocking_review_items"]), ([], []))
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])

    def test_reporter_access_is_not_trusted(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(mr_payload(), jobs=[job("test", "success")], members={"alice": 20},
                        discussions=[discussion("d1", note(1, "alice"))])
        self.assertEqual(self.snapshot(fake)["new_review_items"], [])

    def test_member_reply_makes_an_outsider_thread_block(self):
        self.seed_state(terminal_seconds_ago=120)
        thread = discussion("d1", note(1, "mallory", resolvable=True), note(2, "alice", body="agreed", resolvable=True))
        snapshot = self.snapshot(self.ready_fake(thread))
        self.assertEqual([item["id"] for item in snapshot["new_review_items"]], ["2"])
        self.assertEqual([item["id"] for item in snapshot["blocking_review_items"]], ["2"])

    def test_failed_member_lookup_fails_the_snapshot(self):
        fake = FakeGlab(mr_payload(), jobs=[job("test", "success")],
                        discussions=[discussion("d1", note(1, "alice"))],
                        failing={f"projects/11/members/all/{user_id('alice')}"})
        with self.assertRaises(core.GhCommandError):
            self.snapshot(fake)

    def test_bot_notes_need_a_keyword(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(
            mr_payload(),
            jobs=[job("test", "success")],
            discussions=[
                discussion("d1", note(1, "project_11_bot_abc", body="deployed")),
                discussion("d2", note(2, "service_account_codex_reviewer", body="finding")),
            ],
            bots={"project_11_bot_abc", "service_account_codex_reviewer"},
        )
        snapshot = self.snapshot(fake)
        self.assertEqual([item["id"] for item in snapshot["new_review_items"]], ["2"])

    def test_lookalike_bot_username_from_non_member_is_ignored(self):
        self.seed_state(terminal_seconds_ago=120)
        fake = FakeGlab(
            mr_payload(),
            jobs=[job("test", "success")],
            discussions=[discussion("d1", note(1, "project_1_bot_codex", body="finding", resolvable=True))],
            non_bots={"project_1_bot_codex"},
        )
        snapshot = self.snapshot(fake)
        self.assertEqual((snapshot["new_review_items"], snapshot["blocking_review_items"]), ([], []))
        self.assertEqual(snapshot["actions"], ["stop_ready_to_merge"])

    def test_deleted_line_diff_note_preserves_old_line_and_side(self):
        self.seed_state(terminal_seconds_ago=120)
        deleted_note = note(1, "alice", resolvable=True, diff=True)
        deleted_note["position"] = {
            "old_path": "src/old.py",
            "new_path": None,
            "old_line": 12,
            "new_line": None,
            "head_sha": SHA,
        }
        snapshot = self.snapshot(self.ready_fake(discussion("d1", deleted_note)))
        for key in ("new_review_items", "blocking_review_items"):
            item = snapshot[key][0]
            self.assertEqual((item["path"], item["line"], item["side"]), ("src/old.py", 12, "old"))


class MergePipelineTests(unittest.TestCase):
    def target(self, **overrides):
        pr = {"host": HOST, "project_id": 11, "repo": PATH}
        pr.update(overrides)
        return {"pr": pr, "repo": PATH, "base_branch": "main", "merge_sha": SHA}

    def test_the_merge_commit_is_the_preferred_sha(self):
        pr = {
            "merged": True,
            "repo": PATH,
            "base_branch": "main",
            "head_sha": "h" * 40,
            "merge_commit_sha": "m" * 40,
            "squash_commit_sha": "s" * 40,
        }

        with patch.object(glab, "resolve_mr", return_value=(pr, {})):
            target = glab.collect_merge_pipeline_target(SimpleNamespace(pr="7", repo=None))

        self.assertEqual(target["merge_sha"], "m" * 40)

    def test_the_squash_commit_is_the_next_choice_on_a_fast_forward_project(self):
        pr = {
            "merged": True,
            "host": HOST,
            "project_id": 11,
            "repo": PATH,
            "base_branch": "main",
            "head_sha": "h" * 40,
            "merge_commit_sha": "",
            "squash_commit_sha": "s" * 40,
        }

        with patch.object(glab, "resolve_mr", return_value=(pr, {})), \
                patch.object(glab, "glab_api", return_value={"merge_method": "ff"}) as api:
            target = glab.collect_merge_pipeline_target(SimpleNamespace(pr="7", repo=None))

        self.assertEqual(target["merge_sha"], "s" * 40)
        api.assert_called_once_with(HOST, "projects/11")

    def test_a_squash_commit_outside_a_fast_forward_project_is_not_the_target(self):
        # With merge or rebase_merge the target gets its own commit and
        # `squash_commit_sha` is a source-branch commit. Watching it would miss
        # the real target pipeline, so wait for `merge_commit_sha` instead.
        pr = {
            "merged": True,
            "host": HOST,
            "project_id": 11,
            "repo": PATH,
            "base_branch": "main",
            "head_sha": "h" * 40,
            "merge_commit_sha": "",
            "squash_commit_sha": "s" * 40,
            "squash": True,
        }

        for merge_method in ("merge", "rebase_merge"):
            with patch.object(glab, "resolve_mr", return_value=(pr, {})), \
                    patch.object(glab, "glab_api", return_value={"merge_method": merge_method}):
                target = glab.collect_merge_pipeline_target(SimpleNamespace(pr="7", repo=None))

            self.assertEqual(target["merge_sha"], "", merge_method)

    def _head_only_target(self, merge_method, squash=False):
        pr = {
            "merged": True,
            "host": HOST,
            "project_id": 11,
            "repo": PATH,
            "base_branch": "main",
            "head_sha": "h" * 40,
            "merge_commit_sha": "",
            "squash_commit_sha": "",
            "squash": squash,
        }

        with patch.object(glab, "resolve_mr", return_value=(pr, {})), \
                patch.object(glab, "glab_api", return_value={"merge_method": merge_method}) as api:
            target = glab.collect_merge_pipeline_target(SimpleNamespace(pr="7", repo=None))
        api.assert_called_once_with(HOST, "projects/11")
        return target

    def test_the_head_sha_is_the_last_choice_on_a_fast_forward_project(self):
        self.assertEqual(self._head_only_target("ff")["merge_sha"], "h" * 40)

    def test_a_squashed_fast_forward_merge_waits_instead_of_watching_the_head(self):
        self.assertEqual(self._head_only_target("ff", squash=True)["merge_sha"], "")

    def test_a_merge_commit_project_waits_for_the_merge_sha(self):
        self.assertEqual(self._head_only_target("merge")["merge_sha"], "")

    def test_an_unmerged_mr_is_rejected(self):
        pr = {"merged": False, "repo": PATH, "base_branch": "main", "head_sha": SHA}

        with patch.object(glab, "resolve_mr", return_value=(pr, {})):
            with self.assertRaisesRegex(ValueError, "not merged"):
                glab.collect_merge_pipeline_target(SimpleNamespace(pr="7", repo=None))

    def _status(self, pipelines, jobs=None, bridges=None):
        calls = []

        def fake_list(host, endpoint):
            calls.append(endpoint)
            if "/pipelines?" in endpoint:
                return pipelines
            if endpoint.endswith("/jobs"):
                return jobs or []
            if endpoint.endswith("/bridges"):
                return bridges or []
            raise AssertionError(endpoint)

        with patch.object(glab, "glab_api_list", side_effect=fake_list):
            status = glab.get_merge_pipeline_status(self.target())
        return status, calls

    def test_a_successful_pipeline_is_success(self):
        status, calls = self._status([{"id": 5, "status": "success", "ref": "main"}])

        self.assertEqual(status["state"], "success")
        self.assertIn(f"sha={SHA}", calls[0])
        self.assertIn("ref=main", calls[0])

    def test_a_running_pipeline_is_pending(self):
        status, _calls = self._status([{"id": 5, "status": "running", "ref": "main"}])

        self.assertEqual(status["state"], "pending")

    def test_a_failed_pipeline_reports_the_failed_job(self):
        status, _calls = self._status(
            [{"id": 5, "status": "failed", "ref": "main"}],
            jobs=[job("build", "failed")],
        )

        self.assertEqual((status["state"], status["job"]), ("failed", "build"))

    def test_a_failed_optional_job_is_not_named_as_the_cause(self):
        status, _calls = self._status(
            [{"id": 5, "status": "failed", "ref": "main"}],
            jobs=[job("lint", "failed", allow_failure=True, job_id=10), job("deploy", "failed", job_id=12)],
        )

        self.assertEqual((status["state"], status["job"]), ("failed", "deploy"))

    def test_a_failed_pipeline_falls_back_to_the_pipeline_name(self):
        status, _calls = self._status([{"id": 5, "status": "failed", "ref": "main"}])

        self.assertEqual((status["state"], status["job"]), ("failed", "pipeline"))

    def test_a_failed_job_lookup_error_falls_back_to_the_pipeline_name(self):
        def fake_list(host, endpoint):
            if "/pipelines?" in endpoint:
                return [{"id": 5, "status": "failed", "ref": "main"}]
            raise glab.core.GhCommandError("boom")

        with patch.object(glab, "glab_api_list", side_effect=fake_list):
            status = glab.get_merge_pipeline_status(self.target())

        self.assertEqual((status["state"], status["job"]), ("failed", "pipeline"))

    def test_a_canceled_pipeline_is_reported(self):
        status, _calls = self._status([{"id": 5, "status": "canceled", "ref": "main"}])

        self.assertEqual(status["state"], "canceled")

    def test_a_skipped_pipeline_is_reported(self):
        status, _calls = self._status([{"id": 5, "status": "skipped", "ref": "main"}])

        self.assertEqual(status["state"], "skipped")

    def test_a_manual_pipeline_reports_the_blocking_job(self):
        status, _calls = self._status(
            [{"id": 5, "status": "manual", "ref": "main"}],
            jobs=[job("deploy", "manual", allow_failure=False)],
        )

        self.assertEqual((status["state"], status["job"]), ("manual", "deploy"))

    def test_an_optional_manual_job_does_not_name_the_blocker(self):
        status, _calls = self._status(
            [{"id": 5, "status": "manual", "ref": "main"}],
            jobs=[job("optional-deploy", "manual", allow_failure=True)],
        )

        self.assertEqual((status["state"], status["job"]), ("manual", "pipeline"))

    def test_a_second_successful_pipeline_changes_the_signature(self):
        first = {"id": 5, "status": "success", "ref": "main"}
        later = {"id": 6, "status": "success", "ref": "main"}

        with patch.object(glab, "glab_api_list", return_value=[first]):
            before = glab.get_merge_pipeline_status(self.target())
        with patch.object(glab, "glab_api_list", return_value=[first, later]):
            after = glab.get_merge_pipeline_status(self.target())

        self.assertEqual((before["state"], after["state"]), ("success", "success"))
        self.assertNotEqual(
            core.merge_pipeline_settle_key(before),
            core.merge_pipeline_settle_key(after),
        )

    def test_a_missing_pipeline_falls_back_to_a_sha_only_lookup(self):
        calls = []

        def fake_list(host, endpoint):
            calls.append(endpoint)
            if "ref=main" in endpoint:
                return []
            if "/pipelines?" in endpoint:
                return [{"id": 5, "status": "success", "ref": "main"}]
            raise AssertionError(endpoint)

        with patch.object(glab, "glab_api_list", side_effect=fake_list):
            status = glab.get_merge_pipeline_status(self.target())

        self.assertEqual(status["state"], "success")
        self.assertEqual(len(calls), 2)

    def test_a_sha_only_lookup_ignores_other_refs(self):
        status, _calls = self._status([{"id": 5, "status": "success", "ref": "feature"}])

        self.assertEqual(status["state"], "missing")

    def test_normalize_mr_carries_the_merge_sha_fields(self):
        payload = mr_payload(state="merged")
        payload["merge_commit_sha"] = "m" * 40
        payload["squash_commit_sha"] = "s" * 40
        payload["squash"] = True

        pr = glab.normalize_mr(payload, HOST)

        self.assertEqual(pr["merge_commit_sha"], "m" * 40)
        self.assertEqual(pr["squash_commit_sha"], "s" * 40)
        self.assertTrue(pr["squash"])

    def test_normalize_mr_reads_squash_on_merge_too(self):
        payload = mr_payload(state="merged")
        payload["squash_on_merge"] = True

        pr = glab.normalize_mr(payload, HOST)

        self.assertTrue(pr["squash"])


class ConfigTests(unittest.TestCase):
    def test_codex_and_pr_af_are_forced_off(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"codex": {"required": True}, "pr_af": {"enabled": True, "check_names": ["x"]}}, handle)
            with configured(), patch("sys.stderr") as stderr:
                glab.activate_config(["--config", path])
                config = core.CONFIG
        self.assertFalse(config["codex"]["enabled"])
        self.assertFalse(config["codex"]["required"])
        self.assertFalse(config["pr_af"]["enabled"])
        self.assertTrue(any("do not apply to GitLab" in str(c) for c in stderr.write.call_args_list))

    def test_main_swaps_core_hooks(self):
        with patch.object(core, "collect_snapshot", core.collect_snapshot), \
                patch.object(core, "rerun_failed_run", core.rerun_failed_run), \
                patch.object(core, "activate_config", core.activate_config), \
                patch.object(core, "collect_merge_pipeline_target", None), \
                patch.object(core, "get_merge_pipeline_status", None), \
                patch.object(core, "MERGE_PIPELINE_SETTLE_SECONDS", 45), \
                patch.object(core, "main", return_value=42) as mock_main:
            exit_code = glab.main()
            self.assertEqual(exit_code, 42)
            self.assertIs(core.collect_snapshot, glab.collect_snapshot)
            self.assertIs(core.rerun_failed_run, glab.retry_job)
            self.assertIs(core.activate_config, glab.activate_config)
            self.assertIs(core.collect_merge_pipeline_target, glab.collect_merge_pipeline_target)
            self.assertIs(core.get_merge_pipeline_status, glab.get_merge_pipeline_status)
            self.assertEqual(core.MERGE_PIPELINE_SETTLE_SECONDS, 45)
            mock_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
