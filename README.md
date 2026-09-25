# babysit-pr skill

`babysit-pr` is an agent skill that watches a GitHub pull request until it is ready to merge. A Python script polls
CI, review bots, and review threads, and tells the agent what needs attention. The agent diagnoses failures, fixes
them, triages review findings, and stops when the owner must decide.

This repository holds the one maintained copy of the skill. Other repositories vendor a pinned version of it with
`sync.py`, and keep only their own `config.json` next to it.

## What is in this repository

| Path | Purpose |
|---|---|
| `skill/` | Exactly what `sync.py` copies into a repository. |
| `skill/SKILL.md` | The instructions for the agent. |
| `skill/scripts/gh_pr_watch.py` | The watcher. Python 3.9 or newer, standard library only. |
| `skill/scripts/test_gh_pr_watch.py` | The watcher's tests. They travel with the skill, so repositories can run them in their own CI. |
| `skill/references/` | Failure heuristics and notes on the GitHub calls that the watcher makes. |
| `skill/config.example.json` | The default config. `sync.py` copies it to `config.json` on the first sync. |
| `sync.py` | Copies a pinned version of `skill/` into a repository. |
| `tests/` | Tests for `sync.py`. |

## Requirements

- Python 3.9 or newer.
- The GitHub CLI (`gh`), authenticated with access to the repository.
- Git, to run `sync.py`.

## Add the skill to a repository

Clone this repository once, then run `sync.py` with the path of the repository that should get the skill:

```bash
git clone https://github.com/rock3r/babysit-pr-skill.git
python3 babysit-pr-skill/sync.py ~/src/my-repo --ref v2.2.0
```

`sync.py` does the following:

1. It copies `skill/` at the given tag into `~/src/my-repo/.agents/skills/babysit-pr/`.
2. It deletes files in that folder that are no longer part of the skill.
3. It never deletes or changes files that belong to the repository: `config.json`, `skill-source.json`, and every path
   that matches a glob in `sync.keep` in `config.json`. When there is no `config.json`, it copies
   `config.example.json` to `config.json`.
4. It writes the tag and commit SHA to `VERSION`.
5. It prints every file that it added, updated, removed, or kept.

Without `--ref`, it uses the newest `v*` tag. `--dry-run` prints the changes and writes nothing. Review the result,
edit `config.json` for the repository, and commit the folder.

## Update to a new version

```bash
git -C babysit-pr-skill pull --tags
python3 babysit-pr-skill/sync.py ~/src/my-repo --ref v2.3.0
```

Read [CHANGELOG.md](CHANGELOG.md) for the versions in between. Your `config.json` stays as it is. New config keys
use their defaults until you add them.

## Configure the skill

The watcher reads `config.json` from the skill folder: `.agents/skills/babysit-pr/config.json`. `--config <path>`
reads another file, and `--print-config` prints the settings that the watcher uses.

A missing file or a missing key uses the default. An unknown key prints a warning on stderr and is ignored. A value
of the wrong type stops the watcher with an error that names the key.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | `1` | The config format. A newer version than the watcher knows is an error. |
| `local_gate` | string or `null` | `null` | The command that the agent runs before every push. |
| `expected_skipped_checks` | list of strings | `[]` | Check names that are skipped on every PR by design. Their `skipping` result does not block. A `neutral` result still does. |
| `required_checks` | list of strings | `[]` | Check names that must pass before the PR counts as ready. |
| `retry_eligible_workflow_keywords` | list of strings | `["e2e"]` | The watcher reruns a failed workflow when its name contains one of these words. |
| `hung_check_minutes` | integer | `30` | A check that stays pending longer than this is reported as hung. |
| `trusted_author_associations` | list of strings | `["OWNER", "MEMBER", "COLLABORATOR"]` | Comments from these author associations are review items. |
| `review_bot_login_keywords` | list of strings | `["codex"]` | Comments from `[bot]` accounts whose login contains one of these words are review items. |
| `max_session_minutes` | integer | `90` | The default for `--max-session-minutes`. |
| `require_up_to_date` | `"auto"`, `true`, or `false` | `"auto"` | Whether a PR that is behind its base must be updated before merge. `"auto"` reads the base branch's required status checks and rulesets once per run: only a strict (up-to-date) requirement counts. Only a definitive answer means "not required": a 404 that says the branch has no protection or no required checks, or `strict: false`, and in both cases no strict ruleset. A 403 (the endpoint needs admin access, and free private repositories answer 403 too), any other 404, and every other failure count as required. A repository that knows it has no strict rule can set `false`. |
| `codex.enabled` | boolean | `true` | Watch the Codex review bot. When `false`, the watcher makes no Codex calls. |
| `codex.required` | boolean | `false` | Require a Codex review of the head even on a PR where Codex never posted. |
| `codex.idle_wait_minutes` | integer | `10` | Without `codex.required`: how long to wait after the checks finish for Codex to start a review of the head. After that, the missing review no longer blocks readiness, and `codex_gate.idle_wait_expired` says so. |
| `pr_af.enabled` | boolean | `false` | Watch the label-triggered PR-AF review. When `false`, the watcher makes no PR-AF calls. |
| `pr_af.label` | string | `"pr-af"` | The PR label that asks for a PR-AF review. |
| `pr_af.workflow_names` | list of strings | `[]` | Names of the PR-AF workflow. |
| `pr_af.check_names` | list of strings | `[]` | Names of the PR-AF check or job. A check matches when its name or workflow is in either list. |
| `pr_af.review_body_markers` | list of strings | `[]` | Text that marks a comment as a PR-AF finding. |
| `pr_af.review_author_login` | string | `"github-actions[bot]"` | The login that PR-AF posts as. |
| `pr_af.missing_check_grace_minutes` | integer | `5` | How long a labelled head waits for its PR-AF check to appear. |
| `cleanup.branch_delete_requires_approval` | boolean | `false` | Tells the agent to ask the owner before it deletes a merged branch. |
| `sync.keep` | list of strings | `[]` | Paths in the skill folder that belong to the repository, relative to that folder. Globs work, and `*` also matches `/`. `sync.py` never deletes or overwrites them. The watcher ignores this key. |

`sync.py` always keeps `skill-source.json`, a sidecar file that several repositories keep next to their skills. It
is a reserved name, like `config.json` and `VERSION`, so it needs no `sync.keep` entry.

Name matching ignores case. For PR-AF names it also ignores extra spaces.

An example for a Gradle project whose `recordings` job only runs on `main`, with the PR-AF review switched on:

```json
{
  "version": 1,
  "local_gate": "./gradlew check",
  "expected_skipped_checks": ["recordings"],
  "required_checks": ["check"],
  "pr_af": {
    "enabled": true,
    "workflow_names": ["PR-AF Review"],
    "check_names": ["pr-af-review"],
    "review_body_markers": ["pr-af review —", "reviewed by [pr-af]"]
  }
}
```

## Run the tests in a repository that vendors the skill

```bash
python3 -m unittest discover -s .agents/skills/babysit-pr/scripts -p 'test_*.py'
```

The tests do not read the repository's `config.json` and make no network calls.

## Versions

Releases use semantic versioning and a `vX.Y.Z` tag.

| Change | Version part |
|---|---|
| A config key changes meaning or type, or an action is removed or renamed | major |
| A new action, a new config key, or a new gate that is off by default | minor |
| A fix that keeps the actions and the config as they are | patch |

## Development

```bash
python3 -m unittest discover -s skill/scripts -p 'test_*.py'
python3 -m unittest discover -s tests
```

CI runs both on Python 3.9 and 3.12. Change behaviour test first: write the test, watch it fail for the right
reason, then change the code. To release, update `CHANGELOG.md`, wait for green CI on `main`, then tag `vX.Y.Z` and
create a GitHub release.

## Origin and license

The first version of the watcher followed the PR babysitter pattern from the OpenAI Codex project. Version 1.0.0
merges the copies that several repositories carried, and moves their project-specific values into `config.json`.

Licensed under the [Apache License 2.0](LICENSE).
