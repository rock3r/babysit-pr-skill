import io
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sync  # noqa: E402

SKILL_DIR = Path(".agents") / "skills" / "babysit-pr"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class SourceRepo:
    """A throwaway skill repository with tagged versions."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        git(self.root, "init", "-q", "-b", "main")

    def write(self, rel_path, content, executable=False):
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)

    def remove(self, rel_path):
        (self.root / rel_path).unlink()

    def commit(self, tag=None):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        if tag:
            git(self.root, "tag", tag)
        return git(self.root, "rev-parse", "HEAD")


class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.source = SourceRepo(self.tmp / "source")
        self.source.write("README.md", "not vendored\n")
        self.source.write("skill/SKILL.md", "skill v1\n")
        self.source.write("skill/config.example.json", '{"version": 1}\n')
        self.source.write("skill/scripts/gh_pr_watch.py", "print('v1')\n", executable=True)
        self.source.write("skill/references/old.md", "old\n")
        self.v1 = self.source.commit(tag="v1.0.0")
        self.source.write("skill/SKILL.md", "skill v2\n")
        self.source.remove("skill/references/old.md")
        self.source.write("skill/references/new.md", "new\n")
        self.v2 = self.source.commit(tag="v1.1.0")
        self.target = self.tmp / "consumer"
        self.target.mkdir()
        self.skill = self.target / SKILL_DIR

    def run_sync(self, ref=None, dry_run=False):
        return sync.sync(self.source.root, self.target, ref=ref, dry_run=dry_run)

    def test_first_sync_copies_the_skill_and_creates_config_from_the_example(self):
        report = self.run_sync("v1.0.0")

        self.assertEqual((self.skill / "SKILL.md").read_text(), "skill v1\n")
        self.assertEqual((self.skill / "references" / "old.md").read_text(), "old\n")
        self.assertEqual((self.skill / "config.json").read_text(), '{"version": 1}\n')
        self.assertFalse((self.skill / "README.md").exists())
        self.assertTrue(report["config_created"])
        self.assertIn("SKILL.md", report["added"])

    def test_version_file_records_the_tag_and_the_commit(self):
        self.run_sync("v1.0.0")

        version = (self.skill / "VERSION").read_text()
        self.assertIn("tag: v1.0.0", version)
        self.assertIn(f"commit: {self.v1}", version)

    def test_existing_config_is_never_touched(self):
        self.skill.mkdir(parents=True)
        (self.skill / "config.json").write_text('{"local_gate": "make check"}\n')

        report = self.run_sync("v1.1.0")

        self.assertEqual((self.skill / "config.json").read_text(), '{"local_gate": "make check"}\n')
        self.assertFalse(report["config_created"])

    def test_upgrade_removes_files_that_left_the_skill_and_reports_changes(self):
        self.run_sync("v1.0.0")
        (self.skill / "skill-source.json").write_text("{}\n")
        (self.skill / "agents").mkdir()
        (self.skill / "agents" / "stale.yaml").write_text("x\n")

        report = self.run_sync("v1.1.0")

        self.assertFalse((self.skill / "references" / "old.md").exists())
        self.assertFalse((self.skill / "skill-source.json").exists())
        self.assertFalse((self.skill / "agents").exists())
        self.assertEqual((self.skill / "references" / "new.md").read_text(), "new\n")
        self.assertEqual(report["added"], ["references/new.md"])
        self.assertEqual(report["updated"], ["SKILL.md"])
        self.assertEqual(
            sorted(report["removed"]), ["agents/stale.yaml", "references/old.md", "skill-source.json"])
        self.assertIn("tag: v1.1.0", (self.skill / "VERSION").read_text())

    def test_downgrade_to_an_older_tag_works(self):
        self.run_sync("v1.1.0")
        self.run_sync("v1.0.0")

        self.assertEqual((self.skill / "SKILL.md").read_text(), "skill v1\n")
        self.assertTrue((self.skill / "references" / "old.md").exists())
        self.assertFalse((self.skill / "references" / "new.md").exists())

    def test_default_ref_is_the_newest_version_tag(self):
        self.source.write("skill/SKILL.md", "skill v10\n")
        self.source.commit(tag="v1.10.0")
        self.source.write("skill/SKILL.md", "skill v9\n")
        self.source.commit(tag="v1.9.0")
        self.source.write("skill/SKILL.md", "untagged\n")
        self.source.commit()

        report = self.run_sync()

        self.assertEqual(report["tag"], "v1.10.0")
        self.assertEqual((self.skill / "SKILL.md").read_text(), "skill v10\n")

    def test_a_commit_without_a_tag_is_recorded_as_untagged(self):
        self.source.write("skill/SKILL.md", "untagged\n")
        head = self.source.commit()

        self.run_sync("main")

        version = (self.skill / "VERSION").read_text()
        self.assertIn("tag: none", version)
        self.assertIn(f"commit: {head}", version)

    def test_unknown_ref_fails_cleanly(self):
        with self.assertRaises(sync.SyncError):
            self.run_sync("v9.9.9")
        self.assertFalse(self.skill.exists())

    def test_missing_target_fails_cleanly(self):
        with self.assertRaises(sync.SyncError):
            sync.sync(self.source.root, self.tmp / "missing", ref="v1.0.0")

    def test_dry_run_changes_nothing(self):
        report = self.run_sync("v1.0.0", dry_run=True)

        self.assertFalse(self.skill.exists())
        self.assertIn("SKILL.md", report["added"])
        self.assertTrue(report["config_created"])

    def test_executable_bit_follows_the_source(self):
        self.run_sync("v1.0.0")

        mode = (self.skill / "scripts" / "gh_pr_watch.py").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        self.assertFalse((self.skill / "SKILL.md").stat().st_mode & stat.S_IXUSR)

    def test_reserved_names_in_the_source_are_never_vendored(self):
        self.source.write("skill/config.json", '{"secret": true}\n')
        self.source.write("skill/VERSION", "bogus\n")
        self.source.commit(tag="v2.0.0")

        self.run_sync("v2.0.0")

        self.assertEqual((self.skill / "config.json").read_text(), '{"version": 1}\n')
        self.assertIn("tag: v2.0.0", (self.skill / "VERSION").read_text())

    def test_python_caches_are_left_alone(self):
        self.run_sync("v1.0.0")
        cache = self.skill / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "gh_pr_watch.cpython-39.pyc").write_bytes(b"\x00")

        report = self.run_sync("v1.1.0")

        self.assertTrue((cache / "gh_pr_watch.cpython-39.pyc").exists())
        self.assertNotIn("scripts/__pycache__/gh_pr_watch.cpython-39.pyc", report["removed"])

    def test_the_rest_of_the_target_repository_is_left_alone(self):
        other = self.target / ".agents" / "skills" / "other" / "SKILL.md"
        other.parent.mkdir(parents=True)
        other.write_text("other\n")
        (self.target / "README.md").write_text("mine\n")

        self.run_sync("v1.1.0")

        self.assertEqual(other.read_text(), "other\n")
        self.assertEqual((self.target / "README.md").read_text(), "mine\n")

    def test_command_line_prints_what_changed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = sync.main([str(self.target), "--ref", "v1.0.0", "--source", str(self.source.root)])

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn("v1.0.0", out)
        self.assertIn("added", out)
        self.assertIn("SKILL.md", out)
        self.assertIn("config.json", out)

    def test_command_line_reports_errors_without_a_traceback(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = sync.main([str(self.target), "--ref", "nope", "--source", str(self.source.root)])

        self.assertEqual(code, 1)
        self.assertIn("nope", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class RealSkillTests(unittest.TestCase):
    """The skill in this repository must stay vendorable."""

    def test_skill_has_no_reserved_file_names(self):
        skill = REPO_ROOT / "skill"
        self.assertFalse((skill / "config.json").exists())
        self.assertFalse((skill / "VERSION").exists())
        self.assertTrue((skill / "config.example.json").exists())
        self.assertTrue((skill / "SKILL.md").exists())

    def test_watcher_tests_live_next_to_the_watcher(self):
        # Consumers run: python3 -m unittest discover -s .agents/skills/babysit-pr/scripts -p 'test_*.py'
        scripts = REPO_ROOT / "skill" / "scripts"
        self.assertTrue((scripts / "gh_pr_watch.py").exists())
        self.assertTrue((scripts / "test_gh_pr_watch.py").exists())


if __name__ == "__main__":
    unittest.main()
