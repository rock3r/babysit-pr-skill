import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
                 failing=()):
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
        if "page=" in query and "page=1" not in query.split("&"):
            return "[]"
        if path not in self.routes:
            raise core.GhCommandError(f"GitLab CLI command failed: glab api {path}\nstderr: glab: 404 Not Found (HTTP 404)")
        return json.dumps(self.routes[path])


class GlabTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, "state.json")
        glab._AUTHENTICATED_USERNAME_CACHE.clear()
        glab._MEMBER_ACCESS_CACHE.clear()
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

    def test_need_rebase_respects_require_up_to_date_false(self):
        with configured():
            self.assertTrue(glab.normalize_mr(mr_payload("need_rebase"), HOST)["up_to_date_required"])
        with configured({"require_up_to_date": False}):
            self.assertFalse(glab.normalize_mr(mr_payload("need_rebase"), HOST)["up_to_date_required"])


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
    def test_self_hosted_nested_group_with_port(self):
        self.assertEqual(
            glab.parse_mr_url("https://git.corp.example:8443/a/b/c/-/merge_requests/42/diffs"),
            ("git.corp.example:8443", "a/b/c", 42),
        )

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
        fake = self.ready_fake(
            discussion("d1", note(1, "project_11_bot_abc", body="deployed")),
            discussion("d2", note(2, "service_account_codex_reviewer", body="finding")),
        )
        snapshot = self.snapshot(fake)
        self.assertEqual([item["id"] for item in snapshot["new_review_items"]], ["2"])


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


if __name__ == "__main__":
    unittest.main()
