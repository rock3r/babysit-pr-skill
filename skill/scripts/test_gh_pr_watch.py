import io
import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(__file__)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import gh_pr_watch as watch


def configured(overrides=None):
    """Patch the watcher's active config with `overrides` merged over the defaults."""
    config, _warnings = watch.build_config(overrides or {})
    return patch.object(watch, "CONFIG", config)


class ConfigTests(unittest.TestCase):
    def _write(self, tmp_dir, payload, name="config.json"):
        path = watch.Path(tmp_dir) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return path

    def test_defaults_match_the_documented_behaviour(self):
        config, warnings = watch.build_config({})

        self.assertEqual(warnings, [])
        self.assertIsNone(config["local_gate"])
        self.assertEqual(config["expected_skipped_checks"], [])
        self.assertEqual(config["required_checks"], [])
        self.assertEqual(config["retry_eligible_workflow_keywords"], ["e2e"])
        self.assertEqual(config["hung_check_minutes"], 30)
        self.assertEqual(config["trusted_author_associations"], ["OWNER", "MEMBER", "COLLABORATOR"])
        self.assertEqual(config["review_bot_login_keywords"], ["codex"])
        self.assertEqual(config["max_session_minutes"], 90)
        self.assertTrue(config["codex"]["enabled"])
        self.assertFalse(config["codex"]["required"])
        self.assertFalse(config["coderabbit"]["enabled"])
        self.assertFalse(config["pr_af"]["enabled"])
        self.assertEqual(config["pr_af"]["label"], "pr-af")
        self.assertEqual(config["pr_af"]["missing_check_grace_minutes"], 5)
        self.assertFalse(config["cleanup"]["branch_delete_requires_approval"])

    def test_missing_default_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config, warnings = watch.load_config(watch.Path(tmp_dir) / "config.json", explicit=False)

        self.assertEqual(config, watch.build_config({})[0])
        self.assertEqual(warnings, [])

    def test_missing_explicit_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(watch.ConfigError):
                watch.load_config(watch.Path(tmp_dir) / "nope.json", explicit=True)

    def test_partial_file_keeps_defaults_for_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(tmp_dir, {"local_gate": "make check", "pr_af": {"enabled": True, "check_names": ["x"]}})
            config, warnings = watch.load_config(path, explicit=True)

        self.assertEqual(warnings, [])
        self.assertEqual(config["local_gate"], "make check")
        self.assertTrue(config["pr_af"]["enabled"])
        self.assertEqual(config["pr_af"]["label"], "pr-af")
        self.assertEqual(config["retry_eligible_workflow_keywords"], ["e2e"])

    def test_unknown_keys_warn_instead_of_failing(self):
        config, warnings = watch.build_config({"surprise": 1, "pr_af": {"colour": "blue"}})

        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("surprise" in warning for warning in warnings))
        self.assertTrue(any("pr_af.colour" in warning for warning in warnings))
        self.assertNotIn("surprise", config)

    def test_wrong_types_are_rejected(self):
        bad_values = [
            {"local_gate": 42},
            {"expected_skipped_checks": "recordings"},
            {"expected_skipped_checks": [1]},
            {"hung_check_minutes": "30"},
            {"hung_check_minutes": 0},
            {"hung_check_minutes": True},
            {"max_session_minutes": -1},
            {"codex": {"enabled": "yes"}},
            {"codex": []},
            {"pr_af": {"missing_check_grace_minutes": -1}},
            {"cleanup": {"branch_delete_requires_approval": 1}},
            {"version": "1"},
        ]
        for raw in bad_values:
            with self.subTest(raw=raw):
                with self.assertRaises(watch.ConfigError):
                    watch.build_config(raw)

    def test_top_level_must_be_an_object(self):
        with self.assertRaises(watch.ConfigError):
            watch.build_config(["not", "an", "object"])

    def test_invalid_json_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(tmp_dir, "{ not json")
            with self.assertRaises(watch.ConfigError):
                watch.load_config(path, explicit=True)

    def test_newer_config_version_is_rejected(self):
        with self.assertRaises(watch.ConfigError):
            watch.build_config({"version": 2})

    def test_enabled_pr_af_without_names_warns(self):
        _config, warnings = watch.build_config({"pr_af": {"enabled": True}})

        self.assertTrue(any("pr_af" in warning for warning in warnings))

    def test_default_config_path_is_next_to_the_scripts_directory(self):
        expected = watch.Path(watch.__file__).resolve().parent.parent / "config.json"

        self.assertEqual(watch.default_config_path(), expected)

    def test_example_config_is_valid_and_documents_every_key(self):
        example = watch.Path(SCRIPT_DIR).resolve().parent / "config.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))

        config, warnings = watch.build_config(raw)

        self.assertEqual(warnings, [])
        self.assertEqual(set(raw), set(watch.DEFAULT_CONFIG))
        for section, value in watch.DEFAULT_CONFIG.items():
            if isinstance(value, dict):
                self.assertEqual(set(raw[section]), set(value), section)

    def test_print_config_reports_the_effective_config_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(tmp_dir, {"local_gate": "npm test", "extra": True})
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["gh_pr_watch.py", "--config", str(path), "--print-config"]), \
                    patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                code = watch.main()

        self.assertEqual(code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["config"]["local_gate"], "npm test")
        self.assertEqual(printed["config_file"], str(path))
        self.assertIn("extra", stderr.getvalue())

    def test_bad_config_makes_main_fail_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(tmp_dir, {"hung_check_minutes": "soon"})
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["gh_pr_watch.py", "--config", str(path), "--print-config"]), \
                    patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", stderr):
                code = watch.main()

        self.assertEqual(code, 1)
        self.assertIn("hung_check_minutes", stderr.getvalue())

    def test_max_session_minutes_comes_from_config_when_the_flag_is_absent(self):
        with configured({"max_session_minutes": 15}), \
                patch.object(sys, "argv", ["gh_pr_watch.py", "--snapshot"]):
            args = watch.parse_args()

        self.assertEqual(args.max_session_minutes, 15)

    def test_max_session_minutes_flag_overrides_config(self):
        with configured({"max_session_minutes": 15}), \
                patch.object(sys, "argv", ["gh_pr_watch.py", "--snapshot", "--max-session-minutes", "40"]):
            args = watch.parse_args()

        self.assertEqual(args.max_session_minutes, 40)


class ConfiguredBehaviourTests(unittest.TestCase):
    def test_expected_skipped_checks_come_from_config(self):
        checks = [
            {"name": "deploy", "bucket": "skipping", "state": "SKIPPING"},
            {"name": "Deploy", "bucket": "skipping", "state": "SKIPPING"},
            {"name": "other", "bucket": "skipping", "state": "SKIPPING"},
            # A neutral result is not a skip, so it still needs a look.
            {"name": "deploy", "bucket": "neutral", "state": "NEUTRAL"},
        ]

        with configured({"expected_skipped_checks": ["deploy"]}):
            summary = watch.summarize_checks(checks)

        self.assertEqual(summary["skipping_count"], 2)

    def test_no_skip_is_expected_by_default(self):
        checks = [{"name": "recordings", "bucket": "skipping", "state": "SKIPPED"}]

        with configured():
            self.assertEqual(watch.summarize_checks(checks)["skipping_count"], 1)

    def test_retry_keywords_come_from_config(self):
        with configured({"retry_eligible_workflow_keywords": ["android"]}):
            self.assertTrue(watch.is_retry_eligible_workflow_name("Android tests"))
            self.assertFalse(watch.is_retry_eligible_workflow_name("E2E"))

    def test_hung_threshold_comes_from_config(self):
        with configured({"hung_check_minutes": 12}):
            self.assertEqual(watch.hung_threshold_for_check("anything"), 12 * 60)

    def test_trusted_author_associations_come_from_config(self):
        item = {"author": "someone", "author_association": "CONTRIBUTOR"}

        with configured():
            self.assertFalse(watch.is_trusted_human_review_author(item, "octocat"))
        with configured({"trusted_author_associations": ["CONTRIBUTOR"]}):
            self.assertTrue(watch.is_trusted_human_review_author(item, "octocat"))

    def test_review_bot_login_keywords_come_from_config(self):
        with configured():
            self.assertFalse(watch.is_actionable_review_bot_login("cursor[bot]"))
        with configured({"review_bot_login_keywords": ["codex", "cursor"]}):
            self.assertTrue(watch.is_actionable_review_bot_login("cursor[bot]"))
            # A human login never counts as a review bot, whatever its name.
            self.assertFalse(watch.is_actionable_review_bot_login("cursor-fan"))


class RetryEligibilityTests(unittest.TestCase):
    def _base_pr(self):
        return {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
        }

    def _failed_checks_summary(self):
        return {
            "all_terminal": True,
            "failed_count": 1,
            "pending_count": 0,
            "passed_count": 0,
        }

    def test_recommend_actions_does_not_retry_non_flaky_ci_failures(self):
        actions = watch.recommend_actions(
            pr=self._base_pr(),
            checks_summary=self._failed_checks_summary(),
            failed_runs=[
                {
                    "run_id": 123,
                    "workflow_name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://example.invalid/ci",
                }
            ],
            new_review_items=[],
            hung_checks=[],
            retries_used=0,
            max_retries=3,
            checks_terminal_elapsed=120,
        )

        self.assertIn("diagnose_ci_failure", actions)
        self.assertIn("stop_non_retryable_failure", actions)
        self.assertNotIn("retry_failed_checks", actions)

    def test_recommend_actions_retries_e2e_failures(self):
        actions = watch.recommend_actions(
            pr=self._base_pr(),
            checks_summary=self._failed_checks_summary(),
            failed_runs=[
                {
                    "run_id": 456,
                    "workflow_name": "E2E",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://example.invalid/e2e",
                }
            ],
            new_review_items=[],
            hung_checks=[],
            retries_used=0,
            max_retries=3,
            checks_terminal_elapsed=120,
        )

        self.assertIn("retry_failed_checks", actions)

    def test_mixed_failures_prioritize_non_retryable_stop(self):
        actions = watch.recommend_actions(
            pr=self._base_pr(),
            checks_summary=self._failed_checks_summary(),
            failed_runs=[
                {
                    "run_id": 111,
                    "workflow_name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "retry_eligible": False,
                },
                {
                    "run_id": 222,
                    "workflow_name": "E2E",
                    "status": "completed",
                    "conclusion": "failure",
                    "retry_eligible": True,
                },
            ],
            new_review_items=[],
            hung_checks=[],
            retries_used=0,
            max_retries=3,
            checks_terminal_elapsed=120,
        )

        self.assertIn("stop_non_retryable_failure", actions)
        self.assertNotIn("retry_failed_checks", actions)

    def test_is_pr_ready_to_merge_blocks_on_blocking_review_items(self):
        ready = watch.is_pr_ready_to_merge(
            pr=self._base_pr(),
            checks_summary={
                "all_terminal": True,
                "failed_count": 0,
                "pending_count": 0,
                "passed_count": 2,
            },
            new_review_items=[],
            checks_terminal_elapsed=120,
            blocking_review_items=[{"id": "1", "kind": "review_comment"}],
        )

        self.assertFalse(ready)

    def test_is_pr_ready_to_merge_blocks_while_codex_gate_is_unknown(self):
        # A failed reactions lookup must not be read as "Codex is done".
        ready = watch.is_pr_ready_to_merge(
            pr=self._base_pr(),
            checks_summary={
                "all_terminal": True,
                "failed_count": 0,
                "pending_count": 0,
                "passed_count": 1,
            },
            new_review_items=[],
            checks_terminal_elapsed=120,
            blocking_review_items=[],
            codex_gate={"reviewing": False, "status": "unknown"},
        )

        self.assertFalse(ready)

    def test_summarize_checks_counts_cancelled_as_failed(self):
        checks = [{"name": "check", "workflow": "CI", "bucket": "cancel", "state": "CANCELLED"}]

        summary = watch.summarize_checks(checks)

        self.assertEqual(summary["failed_count"], 1)

    @staticmethod
    def _fake_gh_run(returncode, stdout, stderr=""):
        # Behaves like subprocess.run, including check=True raising on a nonzero exit.
        def run(cmd, check=False, **_kwargs):
            if check and returncode != 0:
                raise watch.subprocess.CalledProcessError(
                    returncode, cmd, output=stdout, stderr=stderr
                )
            return watch.subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        return run

    def test_get_pr_checks_reads_json_when_checks_are_pending_or_failing(self):
        # `gh pr checks` exits 8 while checks are pending and 1 when one failed,
        # but still prints the requested JSON in both cases.
        payload = '[{"name": "check", "bucket": "pending", "state": "IN_PROGRESS"}]'
        for code in (1, 8):
            with patch.object(watch.subprocess, "run", side_effect=self._fake_gh_run(code, payload)):
                checks = watch.get_pr_checks("21", repo="owner/repo")
            self.assertEqual(checks[0]["bucket"], "pending", f"exit code {code}")

    def test_get_pr_checks_still_fails_without_json(self):
        fake = self._fake_gh_run(1, "", stderr="no pull requests found")
        with patch.object(watch.subprocess, "run", side_effect=fake):
            with self.assertRaises(watch.GhCommandError):
                watch.get_pr_checks("21", repo="owner/repo")

    def test_recommend_actions_surfaces_merge_conflict(self):
        pr = self._base_pr()
        pr["mergeable"] = "CONFLICTING"
        pr["merge_state_status"] = "DIRTY"

        actions = watch.recommend_actions(
            pr=pr,
            checks_summary={
                "all_terminal": False,
                "failed_count": 0,
                "pending_count": 0,
                "passed_count": 0,
            },
            failed_runs=[],
            new_review_items=[],
            hung_checks=[],
            retries_used=0,
            max_retries=3,
            checks_terminal_elapsed=None,
            blocking_review_items=[],
        )

        self.assertIn("diagnose_merge_conflict", actions)

    def test_fetch_new_review_items_excludes_resolved_blocking_comments(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }

        review_comment_payload = [
            {
                "id": 42,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "author_association": "NONE",
                "created_at": "2026-01-01T00:00:00Z",
                "body": "Please fix this.",
                "path": "foo.kt",
                "line": 1,
                "commit_id": "abc123",
                "html_url": "https://example.invalid/comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], review_comment_payload, []],
        ), patch.object(
            watch,
            "get_unresolved_review_comment_ids",
            return_value={"ids": set(), "truncated": False},
        ):
            new_items, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        self.assertEqual(len(new_items), 1)
        self.assertEqual(blocking_items, [])

    def test_fetch_new_review_items_ignores_github_actions_review_comments(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }

        review_comment_payload = [
            {
                "id": 42,
                "user": {"login": "github-actions[bot]"},
                "author_association": "NONE",
                "created_at": "2026-01-01T00:00:00Z",
                "body": "Generic workflow comment.",
                "path": "foo.kt",
                "line": 1,
                "commit_id": "abc123",
                "html_url": "https://example.invalid/comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], review_comment_payload, []],
        ), patch.object(
            watch,
            "get_unresolved_review_comment_ids",
            return_value={"ids": {"42"}, "truncated": False},
        ):
            new_items, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        self.assertEqual(new_items, [])
        self.assertEqual(blocking_items, [])

    def test_fetch_new_review_items_blocks_unresolved_comment_even_if_stale(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }

        review_comment_payload = [
            {
                "id": 42,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "author_association": "NONE",
                "created_at": "2025-01-01T00:00:00Z",
                "body": "Please fix this.",
                "path": "foo.kt",
                "line": 1,
                "commit_id": "abc123",
                "html_url": "https://example.invalid/comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], review_comment_payload, []],
        ), patch.object(
            watch,
            "get_unresolved_review_comment_ids",
            return_value={"ids": {"42"}, "truncated": False},
        ):
            _, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        self.assertEqual(len(blocking_items), 1)
        self.assertEqual(blocking_items[0]["id"], "42")

    def test_fetch_new_review_items_blocks_unresolved_comment_on_old_commit(self):
        """Unresolved threads block regardless of which commit they were posted on."""
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }

        review_comment_payload = [
            {
                "id": 99,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "author_association": "NONE",
                "created_at": "2026-01-01T00:00:00Z",
                "body": "FYI",
                "path": "foo.kt",
                "line": 1,
                "commit_id": "different-sha",
                "html_url": "https://example.invalid/comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], review_comment_payload, []],
        ), patch.object(
            watch,
            "get_unresolved_review_comment_ids",
            return_value={"ids": {"99"}, "truncated": False},
        ) as unresolved_lookup:
            _, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        unresolved_lookup.assert_called_once()
        self.assertEqual(len(blocking_items), 1)
        self.assertEqual(blocking_items[0]["id"], "99")

    def test_fetch_new_review_items_resurfaces_edited_issue_comment(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": ["1"],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "seen_issue_comment_updated_at": {"1": "2026-01-01T00:00:00Z"},
            "seen_review_comment_updated_at": {},
            "seen_review_updated_at": {},
            "last_review_poll_at": None,
        }

        issue_payload = [
            {
                "id": 1,
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T01:00:00Z",
                "body": "Updated guidance",
                "html_url": "https://example.invalid/issue-comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[issue_payload, [], []],
        ):
            new_items, _ = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=False,
                authenticated_login="octocat",
            )

        self.assertEqual(len(new_items), 1)
        self.assertEqual(new_items[0]["id"], "1")

    def test_fetch_new_review_items_ignores_self_authored_comments(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "seen_issue_comment_updated_at": {},
            "seen_review_comment_updated_at": {},
            "seen_review_updated_at": {},
            "last_review_poll_at": None,
        }

        issue_payload = [
            {
                "id": 2,
                "user": {"login": "octocat"},
                "author_association": "MEMBER",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "body": "my own note",
                "html_url": "https://example.invalid/issue-comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[issue_payload, [], []],
        ):
            new_items, _ = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        self.assertEqual(new_items, [])

    def test_fetch_new_review_items_blocks_on_own_unresolved_threads(self):
        # The agent authenticates as the owner, so the owner's own open threads
        # must still block even though they are not surfaced as new items.
        pr = {"repo": "owner/repo", "number": 716, "head_sha": "abc123"}
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }
        review_comment_payload = [
            {
                "id": 7,
                "user": {"login": "octocat"},
                "author_association": "OWNER",
                "created_at": "2025-01-01T00:00:00Z",
                "body": "Rename this before merging.",
                "path": "foo.kt",
                "line": 1,
                "commit_id": "abc123",
                "html_url": "https://example.invalid/comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], review_comment_payload, []],
        ), patch.object(
            watch,
            "get_unresolved_review_comment_ids",
            return_value={"ids": {"7"}, "truncated": False},
        ):
            new_items, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        self.assertEqual(new_items, [])
        self.assertEqual([item["id"] for item in blocking_items], ["7"])

    def test_fetch_new_review_items_does_not_block_on_seen_issue_comment_without_edits(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": ["5"],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "seen_issue_comment_updated_at": {"5": "2026-01-01T00:00:00Z"},
            "seen_review_comment_updated_at": {},
            "seen_review_updated_at": {},
            "last_review_poll_at": None,
        }

        issue_payload = [
            {
                "id": 5,
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "body": "Please rename this",
                "html_url": "https://example.invalid/issue-comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[issue_payload, [], []],
        ):
            new_items, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=False,
                authenticated_login="octocat",
            )

        self.assertEqual(new_items, [])
        self.assertEqual(blocking_items, [])

    def test_fetch_new_review_items_resurfaces_edited_old_issue_comment(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": ["5"],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "seen_issue_comment_updated_at": {"5": "2026-01-01T00:00:00Z"},
            "seen_review_comment_updated_at": {},
            "seen_review_updated_at": {},
            "last_review_poll_at": None,
        }

        issue_payload = [
            {
                "id": 5,
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "body": "Updated after long delay",
                "html_url": "https://example.invalid/issue-comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[issue_payload, [], []],
        ):
            new_items, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=False,
                authenticated_login="octocat",
            )

        self.assertEqual(len(new_items), 1)
        self.assertEqual(blocking_items, [])

    def test_fetch_new_review_items_ignores_approved_reviews(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }

        review_payload = [
            {
                "id": 77,
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
                "state": "APPROVED",
                "submitted_at": "2026-01-01T00:00:00Z",
                "body": "Looks good",
                "html_url": "https://example.invalid/review",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], [], review_payload],
        ):
            new_items, _ = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        self.assertEqual(new_items, [])

    def test_normalize_reviews_prefers_updated_at_over_submitted_at(self):
        items = [
            {
                "id": 1,
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
                "state": "COMMENTED",
                "submitted_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T01:00:00Z",
                "created_at": "2026-01-01T00:00:00Z",
                "body": "note",
                "html_url": "https://example.invalid/review",
            }
        ]

        normalized = watch.normalize_reviews(items)

        self.assertEqual(normalized[0]["updated_at"], "2026-01-01T01:00:00Z")

    def test_fetch_new_review_items_fails_closed_when_unresolved_lookup_errors(self):
        pr = {
            "repo": "owner/repo",
            "number": 716,
            "head_sha": "abc123",
        }
        state = {
            "seen_issue_comment_ids": [],
            "seen_review_comment_ids": [],
            "seen_review_ids": [],
            "last_review_poll_at": None,
        }

        review_comment_payload = [
            {
                "id": 42,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "author_association": "NONE",
                "created_at": "2025-01-01T00:00:00Z",
                "body": "Please fix this.",
                "path": "foo.kt",
                "line": 1,
                "commit_id": "abc123",
                "html_url": "https://example.invalid/comment",
            }
        ]

        with patch.object(
            watch,
            "gh_api_list_paginated",
            side_effect=[[], review_comment_payload, []],
        ), patch.object(
            watch,
            "get_unresolved_review_comment_ids",
            side_effect=watch.GhCommandError("boom"),
        ):
            _, blocking_items = watch.fetch_new_review_items(
                pr,
                state,
                fresh_state=True,
                authenticated_login="octocat",
            )

        # Without thread state an old comment may still be open: block rather than guess.
        self.assertEqual([item["id"] for item in blocking_items], ["42"])

    def test_hung_checks_from_checks_flags_never_started_pending_checks(self):
        checks = [
            {
                "name": "CI",
                "bucket": "pending",
                "state": "PENDING",
                "startedAt": "",
                "workflow": "CI",
                "link": "https://example.invalid/check",
            }
        ]
        pending_first_seen = {"ci|CI|https://example.invalid/check": 100}

        with patch.object(watch.time, "time", return_value=watch.hung_threshold_for_check("CI") + 101):
            hung = watch.hung_checks_from_checks(checks, pending_first_seen)

        self.assertEqual(len(hung), 1)
        self.assertEqual(hung[0]["name"], "CI")

    def test_reset_state_for_new_head_sha_clears_pending_map(self):
        state = {
            "last_seen_head_sha": "oldsha",
            "pending_checks_first_seen_at": {"ci|CI|url": 100},
        }

        watch.reset_state_for_new_head_sha(state, "newsha")

        self.assertEqual(state["pending_checks_first_seen_at"], {})

    def test_load_state_resets_seen_tracking_when_stale(self):
        stale_state = {
            "seen_issue_comment_ids": ["1"],
            "seen_review_comment_ids": ["2"],
            "seen_review_ids": ["3"],
            "seen_issue_comment_updated_at": {"1": "2026-01-01T00:00:00Z"},
            "seen_review_comment_updated_at": {"2": "2026-01-01T00:00:00Z"},
            "seen_review_updated_at": {"3": "2026-01-01T00:00:00Z"},
            "last_review_poll_at": "2026-01-01T00:00:00Z",
            "pending_checks_first_seen_at": {"ci|CI|url": 1},
            "checks_went_terminal_at": 100,
            "checks_terminal_sha": "abc",
            "last_snapshot_at": 0,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = watch.Path(tmp_dir) / "state.json"
            path.write_text(json.dumps(stale_state))
            loaded, fresh = watch.load_state(path)

        self.assertTrue(fresh)
        self.assertEqual(loaded["seen_issue_comment_ids"], [])
        self.assertEqual(loaded["seen_review_comment_ids"], [])
        self.assertEqual(loaded["seen_review_ids"], [])
        self.assertEqual(loaded["seen_issue_comment_updated_at"], {})
        self.assertEqual(loaded["seen_review_comment_updated_at"], {})
        self.assertEqual(loaded["seen_review_updated_at"], {})
        self.assertEqual(loaded["pending_checks_first_seen_at"], {})
        self.assertIsNone(loaded["checks_went_terminal_at"])
        self.assertIsNone(loaded["checks_terminal_sha"])

    def test_is_ci_green_false_when_blocking_review_items_present(self):
        snapshot = {
            "pr": {"review_decision": "APPROVED"},
            "checks": {
                "all_terminal": True,
                "failed_count": 0,
                "pending_count": 0,
            },
            "blocking_review_items": [{"id": "1"}],
            "checks_terminal_elapsed_seconds": 120,
        }

        self.assertFalse(watch.is_ci_green(snapshot))

    def test_run_watch_backs_off_on_unchanged_green_state(self):
        sleeps = []
        events = []
        snapshot = {
            "pr": {
                "closed": False,
                "merged": False,
                "head_sha": "abc123",
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "merge_state_status": "CLEAN",
                "review_decision": "APPROVED",
            },
            "checks": {
                "all_terminal": True,
                "failed_count": 0,
                "pending_count": 0,
                "passed_count": 3,
            },
            "new_review_items": [],
            "blocking_review_items": [],
            "actions": ["idle"],
            "checks_terminal_elapsed_seconds": 120,
        }

        with patch.object(watch, "collect_snapshot", return_value=(snapshot, watch.Path("/tmp/state.json"))), \
             patch.object(watch, "print_event", side_effect=lambda event, payload: events.append((event, payload))):

            class StopLoop(Exception):
                pass

            def fake_sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) >= 2:
                    raise StopLoop()

            with patch.object(watch.time, "sleep", side_effect=fake_sleep):
                with self.assertRaises(StopLoop):
                    watch.run_watch(SimpleNamespace(poll_seconds=30, max_session_minutes=10))

        self.assertEqual(sleeps, [30, 60])
        snapshot_events = [payload for event, payload in events if event == "snapshot"]
        self.assertEqual([item["next_poll_seconds"] for item in snapshot_events[:2]], [30, 60])

    def test_retry_failed_now_skips_non_retryable_failures(self):
        snapshot = {
            "pr": {
                "closed": False,
                "merged": False,
                "repo": "owner/repo",
                "head_sha": "abc123",
            },
            "checks": {
                "failed_count": 1,
                "all_terminal": True,
                "pending_count": 0,
                "passed_count": 0,
            },
            "failed_runs": [
                {
                    "run_id": 999,
                    "workflow_name": "CI",
                    "conclusion": "failure",
                    "status": "completed",
                    "retry_eligible": False,
                }
            ],
            "retry_state": {
                "current_sha_retries_used": 0,
                "max_flaky_retries": 3,
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = os.path.join(tmp_dir, "state.json")
            with patch.object(watch, "collect_snapshot", return_value=(snapshot, watch.Path(state_file))):
                result = watch.retry_failed_now(SimpleNamespace())

        self.assertFalse(result["rerun_attempted"])
        self.assertEqual(result["reason"], "contains_non_retryable_failed_runs")


class NeedsAgentAttentionTests(unittest.TestCase):
    def test_idle_does_not_need_attention(self):
        self.assertFalse(watch.needs_agent_attention(["idle"]))

    def test_wait_codex_does_not_need_attention(self):
        self.assertFalse(watch.needs_agent_attention(["wait_codex"]))

    def test_combined_passive_waits_do_not_need_attention(self):
        self.assertFalse(watch.needs_agent_attention(["idle", "wait_codex"]))

    def test_stop_ready_to_merge_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(["stop_ready_to_merge"]))

    def test_diagnose_ci_failure_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(["diagnose_ci_failure"]))

    def test_retry_failed_checks_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(["retry_failed_checks"]))

    def test_process_review_comment_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(["process_review_comment"]))

    def test_stop_pr_closed_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(["stop_pr_closed"]))

    def test_mixed_passive_and_active_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(["wait_codex", "diagnose_ci_failure"]))

    def test_empty_actions_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention([]))

    def test_none_actions_needs_attention(self):
        self.assertTrue(watch.needs_agent_attention(None))


class RunOnceTests(unittest.TestCase):
    def _idle_snapshot(self):
        return {
            "pr": {"closed": False, "merged": False},
            "checks": {"all_terminal": False, "failed_count": 0, "pending_count": 1, "passed_count": 0},
            "actions": ["idle"],
        }

    def _actionable_snapshot(self):
        return {
            "pr": {"closed": False, "merged": False},
            "checks": {"all_terminal": True, "failed_count": 1, "pending_count": 0, "passed_count": 1},
            "actions": ["diagnose_ci_failure", "stop_non_retryable_failure"],
        }

    def test_returns_immediately_when_actionable(self):
        """If the first snapshot already needs attention, return without sleeping."""
        actionable = self._actionable_snapshot()
        sleeps = []

        with patch.object(watch, "collect_snapshot", return_value=(actionable, watch.Path("/tmp/s.json"))), \
             patch.object(watch.time, "sleep", side_effect=sleeps.append):
            result = watch.run_once(SimpleNamespace(
                pr="auto", repo=None, state_file=None, poll_seconds=30, max_flaky_retries=3, max_session_minutes=90,
            ))

        self.assertEqual(result["actions"], ["diagnose_ci_failure", "stop_non_retryable_failure"])
        self.assertEqual(sleeps, [])

    def test_polls_until_actionable(self):
        """Should keep polling through idle snapshots and return on the first actionable one."""
        idle = self._idle_snapshot()
        actionable = self._actionable_snapshot()
        call_count = [0]
        sleeps = []

        def fake_collect(args):
            call_count[0] += 1
            if call_count[0] <= 3:
                return idle, watch.Path("/tmp/s.json")
            return actionable, watch.Path("/tmp/s.json")

        with patch.object(watch, "collect_snapshot", side_effect=fake_collect), \
             patch.object(watch.time, "sleep", side_effect=sleeps.append):
            result = watch.run_once(SimpleNamespace(
                pr="auto", repo=None, state_file=None, poll_seconds=30, max_flaky_retries=3, max_session_minutes=90,
            ))

        self.assertEqual(call_count[0], 4)
        self.assertEqual(len(sleeps), 3)
        self.assertIn("diagnose_ci_failure", result["actions"])

    def test_waits_through_codex(self):
        """wait_codex should not cause early return."""
        waiting = {
            "pr": {"closed": False, "merged": False},
            "checks": {"all_terminal": True, "failed_count": 0, "pending_count": 0, "passed_count": 2},
            "actions": ["wait_codex"],
        }
        ready = {
            "pr": {"closed": False, "merged": False},
            "checks": {"all_terminal": True, "failed_count": 0, "pending_count": 0, "passed_count": 2},
            "actions": ["stop_ready_to_merge"],
        }
        call_count = [0]

        def fake_collect(args):
            call_count[0] += 1
            if call_count[0] <= 2:
                return waiting, watch.Path("/tmp/s.json")
            return ready, watch.Path("/tmp/s.json")

        with patch.object(watch, "collect_snapshot", side_effect=fake_collect), \
             patch.object(watch.time, "sleep", lambda s: None):
            result = watch.run_once(SimpleNamespace(
                pr="auto", repo=None, state_file=None, poll_seconds=30, max_flaky_retries=3, max_session_minutes=90,
            ))

        self.assertEqual(call_count[0], 3)
        self.assertIn("stop_ready_to_merge", result["actions"])

    def test_session_timeout(self):
        """Should return stop_session_timeout when max session time elapses."""
        idle = self._idle_snapshot()
        fake_time = [0.0]

        def advancing_sleep(seconds):
            fake_time[0] += seconds

        def fake_time_fn():
            return fake_time[0]

        with patch.object(watch, "collect_snapshot", return_value=(idle, watch.Path("/tmp/s.json"))), \
             patch.object(watch.time, "sleep", side_effect=advancing_sleep), \
             patch.object(watch.time, "time", side_effect=fake_time_fn):
            result = watch.run_once(SimpleNamespace(
                pr="auto", repo=None, state_file=None, poll_seconds=30, max_flaky_retries=3, max_session_minutes=1,
            ))

        self.assertIn("stop_session_timeout", result["actions"])

    def test_retries_on_gh_error(self):
        """GhCommandError during polling should be retried, not crash."""
        actionable = self._actionable_snapshot()
        call_count = [0]
        sleeps = []

        def flaky_collect(args):
            call_count[0] += 1
            if call_count[0] == 1:
                raise watch.GhCommandError("rate limited")
            return actionable, watch.Path("/tmp/s.json")

        with patch.object(watch, "collect_snapshot", side_effect=flaky_collect), \
             patch.object(watch.time, "sleep", side_effect=sleeps.append):
            result = watch.run_once(SimpleNamespace(
                pr="auto", repo=None, state_file=None, poll_seconds=30, max_flaky_retries=3, max_session_minutes=90,
            ))

        self.assertEqual(call_count[0], 2)
        self.assertEqual(len(sleeps), 1)
        self.assertIn("diagnose_ci_failure", result["actions"])


class CodexHeadReviewTests(unittest.TestCase):
    """Codex must have finished a review of the current head, not merely be idle."""

    HEAD = "b5d394b666fc83b4fa9f779dc85512544662ef0a"

    @staticmethod
    def _summary(status, sha):
        return {
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "body": (
                "<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n"
                "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
                f"| 📝 **Code Review** | {status} <relative-time>x</relative-time> | `{sha}` | New commits |\n"
            ),
        }

    def test_no_summary_comment_means_codex_is_not_active_on_the_pr(self):
        review = watch.summarize_codex_head_review([], self.HEAD)
        self.assertEqual(review, {"active": False, "head_reviewed": False})

    def test_completed_review_of_the_current_head_counts(self):
        comments = [self._summary("✅ **Completed**", self.HEAD[:7])]
        review = watch.summarize_codex_head_review(comments, self.HEAD)
        self.assertEqual(review, {"active": True, "head_reviewed": True})

    def test_running_review_of_the_current_head_does_not_count(self):
        comments = [self._summary("🔄 **Running** since", self.HEAD[:7])]
        review = watch.summarize_codex_head_review(comments, self.HEAD)
        self.assertEqual(review, {"active": True, "head_reviewed": False})

    def test_completed_review_of_an_older_head_does_not_count(self):
        comments = [self._summary("✅ **Completed**", "734f214")]
        review = watch.summarize_codex_head_review(comments, self.HEAD)
        self.assertEqual(review, {"active": True, "head_reviewed": False})

    def test_summary_from_a_non_codex_author_is_ignored(self):
        comment = self._summary("✅ **Completed**", self.HEAD[:7])
        comment["user"] = {"login": "someone-else"}
        review = watch.summarize_codex_head_review([comment], self.HEAD)
        self.assertEqual(review, {"active": False, "head_reviewed": False})

    def _ready(self, codex_gate):
        pr = {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "",
        }
        checks = {
            "all_terminal": True,
            "failed_count": 0,
            "pending_count": 0,
            "passed_count": 1,
            "skipping_count": 0,
        }
        return watch.is_pr_ready_to_merge(
            pr, checks, new_review_items=[], checks_terminal_elapsed=120,
            blocking_review_items=[], codex_gate=codex_gate,
        )

    def test_idle_codex_without_a_review_of_the_head_blocks_readiness(self):
        # No 👀 yet on a fresh head: idle, but Codex has not reviewed this commit.
        gate = {"reviewing": False, "status": "idle", "active": True, "head_reviewed": False}
        self.assertFalse(self._ready(gate))

    def test_completed_review_of_the_head_allows_readiness(self):
        gate = {"reviewing": False, "status": "idle", "active": True, "head_reviewed": True}
        self.assertTrue(self._ready(gate))

    def test_pr_without_codex_is_not_held_by_the_head_check(self):
        gate = {"reviewing": False, "status": "idle", "active": False, "head_reviewed": False}
        self.assertTrue(self._ready(gate))

    def test_waiting_for_a_head_review_emits_wait_codex(self):
        pr = {"closed": False, "merged": False, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN", "review_decision": ""}
        checks = {"all_terminal": True, "failed_count": 0, "pending_count": 0, "passed_count": 1, "skipping_count": 0}
        actions = watch.recommend_actions(
            pr, checks, failed_runs=[], new_review_items=[], hung_checks=[],
            retries_used=0, max_retries=3, checks_terminal_elapsed=120,
            blocking_review_items=[],
            codex_gate={"reviewing": False, "status": "idle", "active": True, "head_reviewed": False},
        )
        self.assertIn("wait_codex", actions)
        self.assertNotIn("stop_ready_to_merge", actions)


class DraftPrTests(unittest.TestCase):
    def _actions(self, merge_state_status, failed_count=0):
        pr = {
            "closed": False, "merged": False, "mergeable": "MERGEABLE",
            "merge_state_status": merge_state_status, "review_decision": "",
        }
        checks = {
            "all_terminal": True, "failed_count": failed_count, "pending_count": 0,
            "passed_count": 1, "skipping_count": 0,
        }
        return watch.recommend_actions(
            pr, checks, failed_runs=[], new_review_items=[], hung_checks=[],
            retries_used=0, max_retries=3, checks_terminal_elapsed=120,
            blocking_review_items=[],
            codex_gate={"reviewing": False, "status": "idle", "active": True, "head_reviewed": True},
        )

    def test_green_draft_stops_and_asks_the_owner_to_mark_it_ready(self):
        # Without this the watcher idles until the session timeout on a PR that can never merge.
        actions = self._actions("DRAFT")
        self.assertIn("stop_draft_pr", actions)
        self.assertNotIn("stop_ready_to_merge", actions)
        self.assertTrue(watch.should_stop_watching(actions))

    def test_draft_with_a_failure_still_diagnoses_the_failure(self):
        actions = self._actions("DRAFT", failed_count=1)
        self.assertIn("diagnose_ci_failure", actions)

    def test_non_draft_pr_gets_no_draft_action(self):
        self.assertNotIn("stop_draft_pr", self._actions("CLEAN"))

    def test_green_draft_is_not_reported_as_an_unexplained_block(self):
        self.assertNotIn("diagnose_merge_blocked", self._actions("DRAFT"))


def _green_checks(**overrides):
    checks = {"all_terminal": True, "failed_count": 0, "pending_count": 0,
              "passed_count": 2, "skipping_count": 0}
    checks.update(overrides)
    return checks


def _open_pr(**overrides):
    pr = {"closed": False, "merged": False, "mergeable": "MERGEABLE",
          "merge_state_status": "CLEAN", "review_decision": ""}
    pr.update(overrides)
    return pr


def _actions_for(pr, checks=None, **kwargs):
    params = {
        "failed_runs": [], "new_review_items": [], "hung_checks": [], "retries_used": 0,
        "max_retries": 3, "checks_terminal_elapsed": 120, "blocking_review_items": [],
        "codex_gate": {"reviewing": False, "status": "idle", "active": True, "head_reviewed": True},
    }
    params.update(kwargs)
    return watch.recommend_actions(pr, checks or _green_checks(), **params)


class BranchBehindTests(unittest.TestCase):
    def test_behind_head_is_not_ready(self):
        ready = watch.is_pr_ready_to_merge(
            _open_pr(merge_state_status="BEHIND"), _green_checks(), new_review_items=[],
            checks_terminal_elapsed=120, blocking_review_items=[],
        )
        self.assertFalse(ready)

    def test_behind_branch_is_surfaced_without_a_conflict(self):
        actions = _actions_for(
            _open_pr(merge_state_status="BEHIND"),
            _green_checks(all_terminal=False, pending_count=1),
            checks_terminal_elapsed=None,
        )
        self.assertIn("diagnose_branch_behind", actions)
        self.assertNotIn("diagnose_merge_conflict", actions)

    def test_green_behind_branch_asks_for_an_update(self):
        actions = _actions_for(_open_pr(merge_state_status="BEHIND"))
        self.assertIn("diagnose_branch_behind", actions)
        self.assertNotIn("stop_ready_to_merge", actions)
        self.assertTrue(watch.should_stop_watching(actions))


class WaitForBotsBeforeBranchUpdateTests(unittest.TestCase):
    """Updating the branch starts new bot reviews, so let running reviews finish first."""

    def test_conflict_waiting_on_codex_does_not_need_attention(self):
        self.assertFalse(watch.needs_agent_attention(["diagnose_merge_conflict", "wait_codex"]))
        self.assertFalse(watch.should_stop_watching(["diagnose_merge_conflict", "wait_codex"]))

    def test_behind_branch_waiting_on_codex_does_not_need_attention(self):
        self.assertFalse(watch.needs_agent_attention(["diagnose_branch_behind", "wait_codex"]))
        self.assertFalse(watch.should_stop_watching(["diagnose_branch_behind", "wait_codex"]))

    def test_conflict_and_behind_branch_stop_once_no_bot_is_running(self):
        self.assertTrue(watch.should_stop_watching(["diagnose_merge_conflict"]))
        self.assertTrue(watch.should_stop_watching(["diagnose_branch_behind"]))

    def test_bot_wait_does_not_hide_other_actionable_states(self):
        self.assertTrue(watch.needs_agent_attention(
            ["diagnose_branch_behind", "wait_codex", "process_review_comment"]))
        self.assertTrue(watch.needs_agent_attention(
            ["diagnose_merge_conflict", "wait_codex", "diagnose_ci_failure"]))


class MergeBlockedTests(unittest.TestCase):
    """Green checks with a BLOCKED merge state that no other action explains."""

    def test_unexplained_block_is_a_terminal_diagnose_action(self):
        actions = _actions_for(_open_pr(merge_state_status="BLOCKED"))
        self.assertEqual(actions, ["diagnose_merge_blocked"])
        self.assertTrue(watch.needs_agent_attention(actions))
        self.assertTrue(watch.should_stop_watching(actions))

    def test_block_waits_out_the_grace_period_first(self):
        actions = _actions_for(_open_pr(merge_state_status="BLOCKED"), checks_terminal_elapsed=10)
        self.assertEqual(actions, ["idle"])

    def test_required_review_explains_the_block(self):
        for decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            with self.subTest(decision=decision):
                actions = _actions_for(_open_pr(merge_state_status="BLOCKED", review_decision=decision))
                self.assertEqual(actions, ["idle"])

    def test_pending_checks_explain_the_block(self):
        actions = _actions_for(
            _open_pr(merge_state_status="BLOCKED"),
            _green_checks(all_terminal=False, pending_count=1), checks_terminal_elapsed=None,
        )
        self.assertEqual(actions, ["idle"])

    def test_running_codex_review_explains_the_block(self):
        actions = _actions_for(
            _open_pr(merge_state_status="BLOCKED"),
            codex_gate={"reviewing": True, "status": "in_progress", "active": True, "head_reviewed": False},
        )
        self.assertEqual(actions, ["wait_codex"])

    def test_open_review_items_explain_the_block(self):
        actions = _actions_for(
            _open_pr(merge_state_status="BLOCKED"),
            blocking_review_items=[{"id": "1", "kind": "review_comment"}],
        )
        self.assertEqual(actions, ["process_review_comment"])

    def test_clean_pr_is_never_reported_as_blocked(self):
        self.assertEqual(_actions_for(_open_pr()), ["stop_ready_to_merge"])


class RequiredChecksTests(unittest.TestCase):
    def test_empty_check_set_is_never_ready(self):
        # Right after a push GitHub may not have registered any check yet.
        actions = _actions_for(_open_pr(), _green_checks(passed_count=0))
        self.assertEqual(actions, ["idle"])

    def test_empty_check_set_is_not_reported_as_an_unexplained_block(self):
        actions = _actions_for(_open_pr(merge_state_status="BLOCKED"), _green_checks(passed_count=0))
        self.assertEqual(actions, ["idle"])

    def test_empty_check_set_is_not_green_for_backoff(self):
        snapshot = {
            "pr": {"review_decision": "APPROVED"},
            "checks": _green_checks(passed_count=0),
            "blocking_review_items": [],
            "checks_terminal_elapsed_seconds": 120,
        }
        self.assertFalse(watch.is_ci_green(snapshot))

    def test_summary_lists_required_checks_that_have_not_passed(self):
        checks = [
            {"name": "Build", "bucket": "pass", "state": "SUCCESS"},
            {"name": "third-party", "bucket": "pass", "state": "SUCCESS"},
        ]
        with configured({"required_checks": ["build", "lint"]}):
            summary = watch.summarize_checks(checks)
        self.assertEqual(summary["required_missing"], ["lint"])

    def test_no_required_checks_by_default(self):
        with configured():
            summary = watch.summarize_checks([{"name": "x", "bucket": "pass", "state": "SUCCESS"}])
        self.assertEqual(summary["required_missing"], [])

    def test_a_lone_third_party_pass_does_not_make_the_pr_ready(self):
        checks = _green_checks(passed_count=1, required_missing=["lint"])
        ready = watch.is_pr_ready_to_merge(
            _open_pr(), checks, new_review_items=[], checks_terminal_elapsed=120, blocking_review_items=[])
        self.assertFalse(ready)

    def test_missing_required_check_is_diagnosed_after_the_grace_period(self):
        checks = _green_checks(required_missing=["lint"])
        self.assertEqual(_actions_for(_open_pr(), checks, checks_terminal_elapsed=10), ["idle"])
        actions = _actions_for(_open_pr(), checks)
        self.assertEqual(actions, ["diagnose_missing_required_checks"])
        self.assertTrue(watch.should_stop_watching(actions))


class SessionTimeoutTests(unittest.TestCase):
    def test_once_timeout_keeps_the_actions_of_the_final_snapshot(self):
        actionable = {
            "pr": {"closed": False, "merged": False},
            "checks": {"all_terminal": True, "failed_count": 1, "pending_count": 0, "passed_count": 1},
            "actions": ["diagnose_ci_failure", "stop_non_retryable_failure"],
        }
        with patch.object(watch, "collect_snapshot", return_value=(actionable, watch.Path("/tmp/s.json"))), \
                patch.object(watch.time, "time", side_effect=[0, 61]):
            result = watch.run_once(SimpleNamespace(
                pr="auto", repo=None, state_file=None, poll_seconds=30, max_flaky_retries=3,
                max_session_minutes=1,
            ))

        self.assertEqual(
            result["actions"], ["diagnose_ci_failure", "stop_non_retryable_failure", "stop_session_timeout"])

    def test_watch_timeout_includes_the_final_snapshot(self):
        events = []
        behind = {
            "pr": {"closed": False, "merged": False},
            "checks": {"all_terminal": True, "failed_count": 0, "pending_count": 0, "passed_count": 2},
            "actions": ["diagnose_branch_behind", "wait_codex"],
        }
        with patch.object(watch.time, "time", side_effect=[0, 61]), \
                patch.object(watch, "collect_snapshot", return_value=(behind, watch.Path("/tmp/s.json"))), \
                patch.object(watch, "print_event", side_effect=lambda event, payload: events.append((event, payload))):
            result = watch.run_watch(SimpleNamespace(poll_seconds=30, max_session_minutes=1))

        self.assertEqual(result, 0)
        self.assertEqual(len(events), 1)
        event, payload = events[0]
        self.assertEqual(event, "stop")
        self.assertEqual(payload["actions"], ["diagnose_branch_behind", "wait_codex", "stop_session_timeout"])
        self.assertEqual(payload["snapshot"]["actions"], payload["actions"])
        self.assertEqual(payload["state_file"], "/tmp/s.json")


class SnapshotOrderingTests(unittest.TestCase):
    def test_codex_gate_is_read_before_review_comments(self):
        # If Codex posts a finding and then marks the head reviewed between the two reads,
        # reading the gate last would pair "reviewed" with a scan that missed the finding.
        calls = []
        pr = {
            "repo": "owner/repo", "number": 21, "head_sha": "abc123",
            "closed": False, "merged": False, "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN", "review_decision": "",
        }
        args = SimpleNamespace(pr="21", repo=None, state_file=None, max_flaky_retries=3)

        def gate(_pr):
            calls.append("codex_gate")
            return {"reviewing": False, "status": "idle", "active": True, "head_reviewed": True}

        def reviews(*_args, **_kwargs):
            calls.append("review_items")
            return [], []

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(watch, "resolve_pr", return_value=pr), \
                patch.object(watch, "default_state_file_for", return_value=watch.Path(tmp) / "s.json"), \
                patch.object(watch, "get_pr_checks", return_value=[]), \
                patch.object(watch, "get_authenticated_login", return_value="octocat"), \
                patch.object(watch, "collect_codex_gate", side_effect=gate), \
                patch.object(watch, "fetch_new_review_items", side_effect=reviews), \
                patch.object(watch, "save_state", return_value=None):
            watch.collect_snapshot(args)

        self.assertEqual(calls, ["codex_gate", "review_items"])


class CodexGateTests(unittest.TestCase):
    def test_codex_reviewing_blocks_merge_readiness(self):
        pr = {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
        }
        checks = {
            "all_terminal": True,
            "failed_count": 0,
            "pending_count": 0,
            "passed_count": 2,
            "skipping_count": 0,
        }
        ready = watch.is_pr_ready_to_merge(
            pr, checks, new_review_items=[], checks_terminal_elapsed=120,
            blocking_review_items=[],
            codex_gate={"reviewing": True, "status": "in_progress"},
        )
        self.assertFalse(ready)

    def test_codex_idle_allows_merge_readiness(self):
        pr = {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
        }
        checks = {
            "all_terminal": True,
            "failed_count": 0,
            "pending_count": 0,
            "passed_count": 2,
            "skipping_count": 0,
        }
        ready = watch.is_pr_ready_to_merge(
            pr, checks, new_review_items=[], checks_terminal_elapsed=120,
            blocking_review_items=[],
            codex_gate={"reviewing": False, "status": "idle"},
        )
        self.assertTrue(ready)

    def test_recommend_actions_emits_wait_codex(self):
        pr = {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
        }
        actions = watch.recommend_actions(
            pr=pr,
            checks_summary={"all_terminal": True, "failed_count": 0, "pending_count": 0, "passed_count": 2, "skipping_count": 0},
            failed_runs=[],
            new_review_items=[],
            hung_checks=[],
            retries_used=0,
            max_retries=3,
            checks_terminal_elapsed=120,
            blocking_review_items=[],
            codex_gate={"reviewing": True, "status": "in_progress"},
        )
        self.assertIn("wait_codex", actions)
        self.assertNotIn("stop_ready_to_merge", actions)


class SkippingChecksTests(unittest.TestCase):
    def test_skipping_count_blocks_merge_readiness(self):
        pr = {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
        }
        checks = {
            "all_terminal": True,
            "failed_count": 0,
            "pending_count": 0,
            "passed_count": 2,
            "skipping_count": 1,
        }
        ready = watch.is_pr_ready_to_merge(
            pr, checks, new_review_items=[], checks_terminal_elapsed=120,
        )
        self.assertFalse(ready)

    def test_diagnose_skipping_checks_emitted(self):
        pr = {
            "closed": False,
            "merged": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
        }
        actions = watch.recommend_actions(
            pr=pr,
            checks_summary={"all_terminal": True, "failed_count": 0, "pending_count": 0, "passed_count": 2, "skipping_count": 1},
            failed_runs=[],
            new_review_items=[],
            hung_checks=[],
            retries_used=0,
            max_retries=3,
            checks_terminal_elapsed=120,
            blocking_review_items=[],
        )
        self.assertIn("diagnose_skipping_checks", actions)

    def test_codex_review_summary_status_comment_is_not_actionable(self):
        # Codex edits this status table on every review; it never carries a finding.
        item = {
            "kind": "issue_comment",
            "author": "chatgpt-codex-connector[bot]",
            "body": "<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n",
        }

        self.assertFalse(watch.is_actionable_review_bot_item(item))

    def test_codex_findings_are_still_actionable(self):
        item = {
            "kind": "review_comment",
            "author": "chatgpt-codex-connector[bot]",
            "body": "**P2** Avoid wrapping the initial stripe onto the right edge",
        }

        self.assertTrue(watch.is_actionable_review_bot_item(item))

    def test_summarize_checks_ignores_jobs_expected_to_skip_on_prs(self):
        # A job such as `recordings` may run only on pushes to main and v* tags.
        checks = [
            {"name": "check", "workflow": "CI", "bucket": "pass", "state": "SUCCESS"},
            {"name": "recordings", "workflow": "CI", "bucket": "skipping", "state": "SKIPPED"},
        ]

        with configured({"expected_skipped_checks": ["recordings"]}):
            summary = watch.summarize_checks(checks)

        self.assertEqual(summary["skipping_count"], 0)
        self.assertEqual(summary["passed_count"], 1)

    def test_summarize_checks_still_counts_unexpected_skips(self):
        checks = [
            {"name": "check", "workflow": "CI", "bucket": "skipping", "state": "SKIPPED"},
        ]

        self.assertEqual(watch.summarize_checks(checks)["skipping_count"], 1)

    def test_summarize_checks_counts_skipping(self):
        checks = [
            {"bucket": "pass", "state": "SUCCESS"},
            {"bucket": "skipping", "state": "SKIPPING"},
            {"bucket": "neutral", "state": "NEUTRAL"},
        ]
        summary = watch.summarize_checks(checks)
        self.assertEqual(summary["passed_count"], 1)
        self.assertEqual(summary["skipping_count"], 2)
        self.assertTrue(summary["all_terminal"])

    def test_should_stop_watching_on_skipping_checks(self):
        self.assertTrue(watch.should_stop_watching(["diagnose_skipping_checks"]))


class HungCheckZeroTimeTests(unittest.TestCase):
    """A queued check whose `startedAt` is a zero-time sentinel must not be
    flagged as hung. GitHub reports Go's zero time (``0001-01-01T00:00:00Z``)
    or the Unix epoch for checks that are queued but not yet started; parsing
    those yields a non-positive timestamp and
    a multi-billion-second elapsed, which previously tripped a false-positive
    ``diagnose_hung_check``."""

    def _pending_check(self, started_at):
        return {
            "name": "Third-party review",
            "bucket": "pending",
            "state": "QUEUED",
            "startedAt": started_at,
            "workflow": "",
            "link": "",
        }

    def test_ignores_go_zero_time_started_at(self):
        check = self._pending_check("0001-01-01T00:00:00Z")
        now = 1_000_000.0
        # First seen only 10 seconds ago -> nowhere near the hung threshold.
        pending_first_seen = {watch.pending_check_key(check): now - 10}

        with patch.object(watch.time, "time", return_value=now):
            hung = watch.hung_checks_from_checks([check], pending_first_seen)

        self.assertEqual(hung, [])

    def test_ignores_unix_epoch_started_at(self):
        check = self._pending_check("1970-01-01T00:00:00Z")
        now = 1_000_000.0
        pending_first_seen = {watch.pending_check_key(check): now - 10}

        with patch.object(watch.time, "time", return_value=now):
            hung = watch.hung_checks_from_checks([check], pending_first_seen)

        self.assertEqual(hung, [])

    def test_zero_time_falls_back_to_first_seen_when_genuinely_hung(self):
        """Rejecting the zero-time sentinel must fall back to first-seen
        tracking, not skip the check entirely: a genuinely old queued check is
        still flagged hung."""
        check = self._pending_check("0001-01-01T00:00:00Z")
        threshold = watch.hung_threshold_for_check("Third-party review")
        now = 1_000_000.0
        pending_first_seen = {watch.pending_check_key(check): now - threshold - 100}

        with patch.object(watch.time, "time", return_value=now):
            hung = watch.hung_checks_from_checks([check], pending_first_seen)

        self.assertEqual(len(hung), 1)
        self.assertEqual(hung[0]["name"], "Third-party review")


class ReviewBotLoginTests(unittest.TestCase):
    def test_github_actions_login_alone_is_not_actionable(self):
        self.assertFalse(watch.is_actionable_review_bot_login("github-actions[bot]"))

    def test_coderabbit_comments_are_not_actionable(self):
        self.assertFalse(watch.is_actionable_review_bot_login("coderabbitai[bot]"))


class GetPrIssueReactionsTests(unittest.TestCase):
    def test_paginates_reactions_via_list_helper(self):
        """Reactions must be fetched across all pages: a bot's 👀 can land on a
        later page on a busy PR, and missing it would let the watcher declare
        merge-readiness while a review is still in progress."""
        pages = [
            {"content": "+1", "user": {"login": "someone"}},
            {"content": "eyes", "user": {"login": "chatgpt-codex-connector[bot]"}},
        ]
        with patch.object(watch, "gh_api_list_paginated", return_value=pages) as paginated:
            result = watch.get_pr_issue_reactions("owner/repo", 1178)

        paginated.assert_called_once()
        endpoint = paginated.call_args[0][0]
        self.assertIn("issues/1178/reactions", endpoint)
        self.assertEqual(result, pages)

    def test_returns_none_on_error(self):
        with patch.object(
            watch, "gh_api_list_paginated", side_effect=watch.GhCommandError("boom")
        ):
            self.assertIsNone(watch.get_pr_issue_reactions("owner/repo", 1178))


class CodexGateReactionTests(unittest.TestCase):
    """`summarize_codex_gate` now operates on pre-fetched reactions."""

    def test_detects_eyes_reaction_from_codex(self):
        reactions = [{"content": "eyes", "user": {"login": "chatgpt-codex-connector[bot]"}}]
        gate = watch.summarize_codex_gate(reactions)
        self.assertTrue(gate["reviewing"])
        self.assertEqual(gate["status"], "in_progress")

    def test_idle_when_no_codex_eyes_reaction(self):
        reactions = [{"content": "+1", "user": {"login": "chatgpt-codex-connector[bot]"}}]
        gate = watch.summarize_codex_gate(reactions)
        self.assertFalse(gate["reviewing"])
        self.assertEqual(gate["status"], "idle")

    def test_unknown_when_reactions_unavailable(self):
        # A failed lookup cannot prove that Codex removed its 👀 reaction, so the gate
        # stays closed and the watcher keeps waiting until the lookup works again.
        gate = watch.summarize_codex_gate(None)
        self.assertTrue(gate["reviewing"])
        self.assertEqual(gate["status"], "unknown")

    def test_unknown_gate_emits_wait_codex_instead_of_idle(self):
        pr = {"closed": False, "merged": False, "mergeable": "MERGEABLE",
              "merge_state_status": "CLEAN", "review_decision": ""}
        checks = {"all_terminal": True, "failed_count": 0, "pending_count": 0,
                  "passed_count": 1, "skipping_count": 0}
        actions = watch.recommend_actions(
            pr, checks, failed_runs=[], new_review_items=[], hung_checks=[],
            retries_used=0, max_retries=3, checks_terminal_elapsed=120,
            blocking_review_items=[], codex_gate=watch.summarize_codex_gate(None),
        )
        self.assertEqual(actions, ["wait_codex"])

    def test_codex_identity_is_exact(self):
        self.assertTrue(watch.is_codex_bot_login("chatgpt-codex-connector[bot]"))
        self.assertTrue(watch.is_codex_bot_login("chatgpt-codex-connector"))
        self.assertFalse(watch.is_codex_bot_login("fake-chatgpt-codex-connector"))
        self.assertFalse(watch.is_codex_bot_login("codex-fan"))

    def test_eyes_reaction_from_a_lookalike_login_is_ignored(self):
        reactions = [{"content": "eyes", "user": {"login": "codex-fan"}}]
        self.assertFalse(watch.summarize_codex_gate(reactions)["reviewing"])


class GhCommandHardeningTests(unittest.TestCase):
    @staticmethod
    def _fake_gh_run(returncode, stdout, stderr=""):
        def run(cmd, check=False, **_kwargs):
            if check and returncode != 0:
                raise watch.subprocess.CalledProcessError(returncode, cmd, output=stdout, stderr=stderr)
            return watch.subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        return run

    def _run_emitting(self, payload):
        """Run gh_text against a real child process that writes `payload` as UTF-8."""
        emitter = "import sys; sys.stdout.buffer.write({}.encode('utf-8'))".format(ascii(payload))
        real_run = watch.subprocess.run

        def fake_run(cmd, **kwargs):
            return real_run([sys.executable, "-c", emitter], **kwargs)

        return patch.object(watch.subprocess, "run", side_effect=fake_run)

    def test_gh_text_requests_utf8_decoding_and_a_timeout(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(stdout="{}", stderr="")

        with patch.object(watch.subprocess, "run", side_effect=fake_run):
            watch.gh_text(["pr", "view"])

        self.assertEqual(captured.get("encoding"), "utf-8")
        self.assertEqual(captured.get("errors"), "replace")
        self.assertEqual(captured.get("timeout"), watch.GH_COMMAND_TIMEOUT_SECONDS)

    def test_gh_text_decodes_non_ascii_output(self):
        # An em dash or emoji in a review body must not fail on a non-UTF-8 locale.
        body = "Codex review — nit \U0001f41b"
        with self._run_emitting(body):
            self.assertEqual(watch.gh_text(["pr", "view"]), body)

    def test_gh_json_parses_non_ascii_output(self):
        body = "Review — found an issue \U0001f41b"
        with self._run_emitting(json.dumps({"body": body}, ensure_ascii=False)):
            self.assertEqual(watch.gh_json(["pr", "view", "--json", "body"])["body"], body)

    def test_gh_text_raises_when_stdout_is_missing(self):
        # A dead stdout reader thread must not leak out as an AttributeError on None.
        with patch.object(watch.subprocess, "run", return_value=SimpleNamespace(stdout=None, stderr="")):
            with self.assertRaises(watch.GhCommandError) as context:
                watch.gh_json(["pr", "view", "--json", "number"])
        self.assertIn("No output captured", str(context.exception))

    def test_gh_text_times_out(self):
        timeout = watch.subprocess.TimeoutExpired(["gh", "pr", "view"], watch.GH_COMMAND_TIMEOUT_SECONDS)
        with patch.object(watch.subprocess, "run", side_effect=timeout):
            with self.assertRaises(watch.GhCommandError) as context:
                watch.gh_text(["pr", "view"])
        self.assertIn("timed out", str(context.exception))

    def test_other_exit_codes_still_fail_even_with_output(self):
        fake = self._fake_gh_run(4, '{"message": "authentication required"}')
        with patch.object(watch.subprocess, "run", side_effect=fake):
            with self.assertRaises(watch.GhCommandError):
                watch.get_pr_checks("21", repo="owner/repo")

    def test_load_state_reads_non_ascii_state_files_as_utf8(self):
        # Check names with emoji reach the state file through pending_check_key().
        key = "build \U0001f680|CI — main|https://example.test/1"
        state = {"last_snapshot_at": watch.time.time(), "pending_checks_first_seen_at": {key: 1}}
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = watch.Path(tmp_dir) / "state.json"
            path.write_bytes(json.dumps(state, ensure_ascii=False).encode("utf-8"))
            loaded, _ = watch.load_state(path)
        self.assertEqual(loaded["pending_checks_first_seen_at"], {key: 1})


class ReviewThreadLookupTests(unittest.TestCase):
    def test_rejects_graphql_errors_instead_of_returning_no_blockers(self):
        with patch.object(watch, "gh_json", return_value={"data": None, "errors": [{"message": "boom"}]}):
            with self.assertRaises(watch.GhCommandError):
                watch.get_unresolved_review_comment_ids("owner/repo", 1)

    def test_rejects_a_payload_without_review_threads(self):
        with patch.object(watch, "gh_json", return_value={"data": {"repository": {"pullRequest": None}}}):
            with self.assertRaises(watch.GhCommandError):
                watch.get_unresolved_review_comment_ids("owner/repo", 1)

    def test_own_resolved_threads_do_not_block(self):
        pr = {"repo": "owner/repo", "number": 27, "head_sha": "abc123"}
        state = {"seen_issue_comment_ids": [], "seen_review_comment_ids": [], "seen_review_ids": [],
                 "last_review_poll_at": None}
        own = {"id": 8, "user": {"login": "octocat"}, "author_association": "OWNER",
               "created_at": "2025-01-01T00:00:00Z", "body": "Rename this.", "path": "a.py",
               "line": 1, "commit_id": "abc123", "html_url": "https://example.invalid/c"}
        with patch.object(watch, "gh_api_list_paginated", side_effect=[[], [own], []]), \
                patch.object(watch, "get_unresolved_review_comment_ids",
                             return_value={"ids": set(), "truncated": False}):
            new_items, blocking_items = watch.fetch_new_review_items(
                pr, state, fresh_state=True, authenticated_login="octocat")

        self.assertEqual(new_items, [])
        self.assertEqual(blocking_items, [])


if __name__ == "__main__":
    unittest.main()
