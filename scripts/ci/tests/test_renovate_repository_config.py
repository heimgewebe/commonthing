from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "renovate.json"
WORKFLOWS = ROOT / ".github/workflows"
EXPECTED_RULE = {
    "description": "Attach proven governance metadata to allowlisted web tooling patches",
    "matchManagers": ["npm"],
    "matchFileNames": ["apps/web/package.json"],
    "matchPackageNames": ["eslint", "postcss"],
    "matchUpdateTypes": ["patch"],
    "prBodyNotes": [
        "<!-- weltgewebe-risk: R2 -->",
        "<!-- weltgewebe-attention-impact: none -->",
        "<!-- weltgewebe-attention-rationale: Allowlisted dependency-only patch update in apps/web; no attention-domain semantics, prioritization, triggers, or user-facing attention behavior changed. -->",
    ],
}
EXPECTED_GROUP_RULE = {
    "description": "Land the newly visible action pins as one review instead of one pull request per action",
    "matchManagers": ["github-actions", "custom.regex"],
    "matchFileNames": [".github/workflows/**"],
    "groupName": "github actions",
}
EXPECTED_ACTION_MANAGER = {
    "description": (
        "Read SHA-pinned GitHub Actions that declare their tag in the repo's "
        "`# tag: <tag>` provenance comment. The built-in github-actions manager "
        "only accepts a bare `# <tag>` comment and skips every ref here as "
        "unversioned-reference, so without this manager no pinned action is ever "
        "updated. A quoted reference is matched too, because the pinning guard "
        "accepts one; the rewrite drops the quotes and leaves the canonical form."
    ),
    "customType": "regex",
    "managerFilePatterns": ["/^\\.github/workflows/[^/]+\\.ya?ml$/"],
    "matchStrings": [
        "uses:\\s*[\"']?(?<depName>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"
        "@(?<currentDigest>[0-9a-f]{40})[\"']?"
        "[ \\t]+#[ \\t]*tag:[ \\t]*(?<currentValue>v[0-9][A-Za-z0-9._-]*)"
    ],
    "autoReplaceStringTemplate": (
        "uses: {{{depName}}}@{{{newDigest}}} # tag: {{{newValue}}}"
    ),
    "datasourceTemplate": "github-tags",
    "versioningTemplate": "github-actions",
    "depTypeTemplate": "action",
}


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def action_matcher() -> re.Pattern[str]:
    # Python spells the named group (?P<name>...); Renovate spells it (?<name>...).
    pattern = EXPECTED_ACTION_MANAGER["matchStrings"][0].replace("(?<", "(?P<")
    return re.compile(pattern)


class RenovateRepositoryConfigTests(unittest.TestCase):
    def test_config_is_narrow_and_non_automerge(self) -> None:
        self.assertEqual(
            load_config(),
            {
                "$schema": "https://docs.renovatebot.com/renovate-schema.json",
                "automerge": False,
                "customManagers": [EXPECTED_ACTION_MANAGER],
                "packageRules": [EXPECTED_RULE, EXPECTED_GROUP_RULE],
            },
        )

    def test_rule_cannot_expand_beyond_proven_patch_tooling(self) -> None:
        rule = load_config()["packageRules"][0]

        self.assertEqual(rule["matchManagers"], ["npm"])
        self.assertEqual(rule["matchFileNames"], ["apps/web/package.json"])
        self.assertEqual(rule["matchPackageNames"], ["eslint", "postcss"])
        self.assertEqual(rule["matchUpdateTypes"], ["patch"])
        self.assertNotIn("automerge", rule)

    def test_no_rule_enables_automerge(self) -> None:
        config = load_config()

        self.assertFalse(config["automerge"])
        for rule in config["packageRules"]:
            self.assertNotIn("automerge", rule)

    def test_action_manager_reads_the_repo_pinning_convention(self) -> None:
        matcher = action_matcher()
        match = matcher.search(
            "      - uses: actions/checkout@"
            "3d3c42e5aac5ba805825da76410c181273ba90b1 # tag: v7.0.1"
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("depName"), "actions/checkout")
        self.assertEqual(
            match.group("currentDigest"),
            "3d3c42e5aac5ba805825da76410c181273ba90b1",
        )
        self.assertEqual(match.group("currentValue"), "v7.0.1")

    def test_action_manager_reads_a_quoted_reference(self) -> None:
        # The pinning guard strips quotes (clean_uses), so a quoted ref is a
        # legal pin. If the manager missed it, that pin would be invisible to
        # Renovate while passing every check.
        matcher = action_matcher()
        sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"

        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                match = matcher.search(
                    f"      - uses: {quote}actions/checkout@{sha}{quote}"
                    " # tag: v7.0.1"
                )
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.group("depName"), "actions/checkout")
                self.assertEqual(match.group("currentDigest"), sha)
                self.assertEqual(match.group("currentValue"), "v7.0.1")

    def test_action_manager_ignores_refs_without_a_tag(self) -> None:
        matcher = action_matcher()
        commit = "3edfce9056124e459a23f683a21433670d47daca"

        for line in (
            f"      - uses: actions/cache@{commit}",
            f"      - uses: actions/cache@{commit} # provenance: untagged",
            f"      - uses: actions/cache@{commit} # provenance: untagged (1 commit after v6.1.0)",
        ):
            with self.subTest(line=line):
                self.assertIsNone(matcher.search(line))

    def test_every_tagged_pin_in_the_workflows_is_matched(self) -> None:
        matcher = action_matcher()
        declared = re.compile(
            r"^\s*-?\s*uses:\s*[\"']?[A-Za-z0-9._/-]+@[0-9a-f]{40}[\"']?"
            r"[ \t]+#[ \t]*tag:"
        )
        missed: list[str] = []
        matched = 0

        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            for line in workflow.read_text(encoding="utf-8").splitlines():
                if not declared.match(line):
                    continue
                if matcher.search(line):
                    matched += 1
                else:
                    missed.append(f"{workflow.name}: {line.strip()}")

        self.assertEqual(missed, [])
        self.assertGreater(matched, 0)


if __name__ == "__main__":
    unittest.main()
