#!/usr/bin/env python3
"""Copy a pinned version of the babysit-pr skill into a repository.

Usage:
    python3 sync.py <target-repo> [--ref vX.Y.Z] [--dry-run]

The script copies `skill/` at the given Git ref of this repository into
`<target-repo>/.agents/skills/babysit-pr/`. It deletes vendored files that are no
longer part of the skill, never touches `config.json`, and records the version in
`VERSION`. Without --ref it uses the newest `v*` tag.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/rock3r/babysit-pr-skill"
SKILL_PREFIX = "skill/"
TARGET_SUBDIR = Path(".agents") / "skills" / "babysit-pr"
CONFIG_NAME = "config.json"
EXAMPLE_CONFIG_NAME = "config.example.json"
VERSION_NAME = "VERSION"
# Files at the root of the vendored folder that belong to the consumer, not the skill.
RESERVED_NAMES = {CONFIG_NAME, VERSION_NAME}
# Build output that Python writes next to the scripts. It is never part of the skill.
IGNORED_DIR_NAMES = {"__pycache__"}


class SyncError(RuntimeError):
    pass


def run_git(source_repo, *args, binary=False):
    try:
        proc = subprocess.run(
            ["git", "-C", str(source_repo), *args],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as err:
        raise SyncError("`git` command not found") from err
    except subprocess.CalledProcessError as err:
        stderr = err.stderr.decode("utf-8", errors="replace").strip()
        raise SyncError(f"git {' '.join(args)} failed: {stderr}") from err
    return proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace").strip()


def newest_version_tag(source_repo):
    tags = run_git(source_repo, "tag", "--list", "v*", "--sort=-v:refname").splitlines()
    if not tags:
        raise SyncError("the skill repository has no v* tags; pass --ref")
    return tags[0]


def resolve_ref(source_repo, ref):
    """Return (commit SHA, tag name or None) for `ref`."""
    try:
        commit = run_git(source_repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except SyncError:
        commit = ""
    if not commit:
        raise SyncError(f"unknown ref '{ref}' in {source_repo}")
    tags = [
        tag for tag in run_git(source_repo, "tag", "--points-at", commit, "--sort=-v:refname").splitlines()
        if tag
    ]
    if ref in tags:
        tag = ref
    else:
        version_tags = [tag for tag in tags if tag.startswith("v")]
        tag = version_tags[0] if version_tags else (tags[0] if tags else None)
    return commit, tag


def read_skill_files(source_repo, commit):
    """Map each vendored path (relative to skill/) to (content bytes, executable)."""
    listing = run_git(source_repo, "ls-tree", "-r", "-z", commit, "--", SKILL_PREFIX, binary=True)
    files = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        mode, kind, blob = meta.decode().split()
        path = raw_path.decode("utf-8")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise SyncError(f"unsupported entry in the skill: {path} ({mode} {kind})")
        rel_path = path[len(SKILL_PREFIX):]
        if rel_path in RESERVED_NAMES:
            continue
        content = run_git(source_repo, "cat-file", "blob", blob, binary=True)
        files[rel_path] = (content, mode == "100755")
    if not files:
        raise SyncError(f"no skill files at {commit} (expected a skill/ directory)")
    return files


def existing_vendored_files(skill_dir):
    """Relative paths of files in the vendored folder that the skill owns."""
    found = []
    if not skill_dir.exists():
        return found
    for dir_path, dir_names, file_names in os.walk(skill_dir):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIR_NAMES]
        for name in file_names:
            rel_path = (Path(dir_path) / name).relative_to(skill_dir).as_posix()
            if rel_path in RESERVED_NAMES:
                continue
            found.append(rel_path)
    return sorted(found)


def is_executable(path):
    return bool(path.stat().st_mode & 0o100)


def prune_empty_dirs(skill_dir):
    for dir_path, _dir_names, _file_names in sorted(os.walk(skill_dir), key=lambda item: -len(item[0])):
        path = Path(dir_path)
        if path != skill_dir and not any(path.iterdir()):
            path.rmdir()


def version_text(tag, commit):
    return f"tag: {tag or 'none'}\ncommit: {commit}\nsource: {REPO_URL}\n"


def sync(source_repo, target_repo, ref=None, dry_run=False):
    """Vendor the skill at `ref` into `target_repo`. Returns a report dict."""
    source_repo = Path(source_repo)
    target_repo = Path(target_repo)
    if not target_repo.is_dir():
        raise SyncError(f"target repository not found: {target_repo}")

    ref = ref or newest_version_tag(source_repo)
    commit, tag = resolve_ref(source_repo, ref)
    files = read_skill_files(source_repo, commit)
    skill_dir = target_repo / TARGET_SUBDIR

    report = {
        "ref": ref,
        "tag": tag,
        "commit": commit,
        "target": str(skill_dir),
        "added": [],
        "updated": [],
        "removed": [],
        "unchanged": [],
        "config_created": False,
        "dry_run": dry_run,
    }

    for rel_path in existing_vendored_files(skill_dir):
        if rel_path not in files:
            report["removed"].append(rel_path)
            if not dry_run:
                (skill_dir / rel_path).unlink()

    for rel_path, (content, executable) in sorted(files.items()):
        path = skill_dir / rel_path
        if not path.exists():
            report["added"].append(rel_path)
        elif path.read_bytes() != content or is_executable(path) != executable:
            report["updated"].append(rel_path)
        else:
            report["unchanged"].append(rel_path)
            continue
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755 if executable else 0o644)

    config_path = skill_dir / CONFIG_NAME
    if not config_path.exists() and EXAMPLE_CONFIG_NAME in files:
        report["config_created"] = True
        if not dry_run:
            config_path.write_bytes(files[EXAMPLE_CONFIG_NAME][0])

    if not dry_run:
        (skill_dir / VERSION_NAME).write_text(version_text(tag, commit), encoding="utf-8")
        prune_empty_dirs(skill_dir)
    return report


def print_report(report):
    label = report["tag"] or "untagged commit"
    prefix = "Would sync" if report["dry_run"] else "Synced"
    print(f"{prefix} babysit-pr {label} ({report['commit'][:12]}) into {report['target']}")
    for key in ("added", "updated", "removed"):
        for rel_path in report[key]:
            print(f"  {key:<8} {rel_path}")
    if report["config_created"]:
        print(f"  created  {CONFIG_NAME} (copied from {EXAMPLE_CONFIG_NAME})")
    else:
        print(f"  kept     {CONFIG_NAME}")
    print(
        f"{len(report['added'])} added, {len(report['updated'])} updated, "
        f"{len(report['removed'])} removed, {len(report['unchanged'])} unchanged."
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Copy a pinned version of the babysit-pr skill into .agents/skills/babysit-pr/."
    )
    parser.add_argument("target", help="Path to the repository that vendors the skill")
    parser.add_argument("--ref", help="Tag, branch, or commit to copy (default: the newest v* tag)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, and change nothing")
    parser.add_argument(
        "--source",
        default=str(Path(__file__).resolve().parent),
        help="Path to the skill repository (default: the repository that contains this script)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = sync(args.source, args.target, ref=args.ref, dry_run=args.dry_run)
    except SyncError as err:
        sys.stderr.write(f"sync.py error: {err}\n")
        return 1
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
