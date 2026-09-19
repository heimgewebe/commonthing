from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[3]
DEPLOY_SCRIPT = ROOT / "scripts" / "ops" / "deploy-exact-commit-vps.sh"
RECONCILE_SCRIPT = ROOT / "scripts" / "ops" / "reconcile-production-main-vps.sh"
VALIDATOR = ROOT / "scripts" / "ops" / "validate_web_deploy_archive.py"

GIT_MAINTENANCE_ENV = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "maintenance.auto",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "gc.auto",
    "GIT_CONFIG_VALUE_1": "0",
}


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    effective_env = None if env is None else env.copy()
    if argv and Path(argv[0]).name == "git":
        effective_env = os.environ.copy() if effective_env is None else effective_env
        effective_env.update(GIT_MAINTENANCE_ENV)
        effective_env["GIT_CONFIG_GLOBAL"] = os.devnull
        effective_env["GIT_CONFIG_NOSYSTEM"] = "1"

    result = subprocess.run(
        argv,
        cwd=cwd,
        env=effective_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {argv!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class GitFixtureIsolationTests(unittest.TestCase):
    def test_run_ignores_inherited_global_git_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            hooks = root / "hooks"
            hooks.mkdir()
            pre_push = hooks / "pre-push"
            pre_push.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            pre_push.chmod(0o755)
            global_config = root / "global.gitconfig"
            global_config.write_text(
                f"[core]\n\thooksPath = {hooks}\n",
                encoding="utf-8",
            )
            remote = root / "remote.git"
            seed = root / "seed"

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": str(global_config),
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                run(["git", "init", "--bare", str(remote)])
                run(["git", "init", "-b", "main", str(seed)])
                run(["git", "config", "user.name", "Integration Test"], cwd=seed)
                run(
                    ["git", "config", "user.email", "integration@example.invalid"],
                    cwd=seed,
                )
                (seed / "fixture.txt").write_text("fixture\n", encoding="utf-8")
                run(["git", "add", "fixture.txt"], cwd=seed)
                run(["git", "commit", "-m", "fixture"], cwd=seed)
                run(["git", "remote", "add", "origin", str(remote)], cwd=seed)
                result = run(["git", "push", "-u", "origin", "main"], cwd=seed)

            self.assertEqual(result.returncode, 0)

    def test_privileged_skips_without_passwordless_sudo(self) -> None:
        denied = subprocess.CompletedProcess(
            args=["/usr/bin/sudo", "-n", "true"],
            returncode=1,
            stdout="",
            stderr="sudo: a password is required",
        )
        with (
            mock.patch.object(os, "geteuid", return_value=1000),
            mock.patch.object(shutil, "which", return_value="/usr/bin/sudo"),
            mock.patch.object(subprocess, "run", return_value=denied) as run_mock,
        ):
            with self.assertRaisesRegex(unittest.SkipTest, "passwordless sudo"):
                DeployExactCommitIntegrationTests.privileged(["chown", "target"])
        run_mock.assert_called_once_with(
            ["/usr/bin/sudo", "-n", "true"],
            text=True,
            capture_output=True,
            check=False,
        )


class DeployExactCommitIntegrationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(self.restore_test_ownership)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.seed = self.root / "seed"
        self.releases = self.root / "releases"
        self.state = self.root / "state"
        self.runtime_env = self.root / "runtime.env"
        self.bin = self.root / "bin"
        self.artifact = self.state / "artifacts" / "web.tar.gz"
        self.runtime_env.write_text("TEST_RUNTIME=1\n", encoding="utf-8")
        self.bin.mkdir()

        run(["git", "init", "--bare", str(self.remote)])
        run(["git", "init", "-b", "main", str(self.seed)])
        run(["git", "config", "user.name", "Integration Test"], cwd=self.seed)
        run(
            ["git", "config", "user.email", "integration@example.invalid"],
            cwd=self.seed,
        )
        (self.seed / "apps/web").mkdir(parents=True)
        (self.seed / "scripts").mkdir()
        (self.seed / "map-style").mkdir()
        (self.seed / "apps/web/placeholder.txt").write_text("web\n", encoding="utf-8")
        (self.seed / "map-style/style-germany.json").write_text(
            '{"name":"Germany fixture"}\n', encoding="utf-8"
        )
        (self.seed / "map-style/style-germany-dark.json").write_text(
            '{"name":"Germany dark fixture"}\n', encoding="utf-8"
        )
        self.schauwerk_manifest = self.root / "schauwerk-manifest.json"
        self.schauwerk_manifest.write_text(
            '{"fixture":"schauwerk"}\n', encoding="utf-8"
        )
        (self.seed / "infra/schauwerk-editor").mkdir(parents=True)
        (self.seed / "infra/schauwerk-editor/release-lock.json").write_text(
            json.dumps(
                {
                    "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
                    "source_repository": "heimgewebe/schauwerk",
                    "source_commit": "c" * 40,
                    "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
                    "image_digest": "sha256:" + "d" * 64,
                    "public_base_path": "/schaubild",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        up_script = self.seed / "scripts/weltgewebe-up"
        up_script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ -e /proc/$$/fd/9 ]]; then
                  printf 'production lock descriptor leaked into weltgewebe-up\n' >&2
                  exit 94
                fi
                if [[ -n "${TEST_UP_UMASK_LOG:-}" ]]; then
                  umask >> "$TEST_UP_UMASK_LOG"
                fi
                if [[ -n "${TEST_UP_LOG:-}" ]]; then
                  printf '%s\n' "$*" >> "$TEST_UP_LOG"
                fi
                if [[ ("${ADVANCE_REMOTE_ON_MIGRATION:-0}" == "1" && " $* " == *" --deploy-scope migration "*) || ("${ADVANCE_REMOTE_ON_DEPLOY:-0}" == "1" && " $* " != *" --deploy-scope migration "*) ]]; then
                  work="$(mktemp -d)"
                  git clone --quiet "$TEST_REMOTE" "$work/repo"
                  git -C "$work/repo" config user.name "Integration Test"
                  git -C "$work/repo" config user.email "integration@example.invalid"
                  printf 'advanced\\n' > "$work/repo/advanced.txt"
                  git -C "$work/repo" add advanced.txt
                  git -C "$work/repo" commit --quiet -m advance
                  git -C "$work/repo" push --quiet origin HEAD:main
                  rm -rf "$work"
                fi
                if [[ "${BREAK_REMOTE_ON_MIGRATION:-0}" == "1" && " $* " == *" --deploy-scope migration "* ]]; then
                  mv "$TEST_REMOTE" "${TEST_REMOTE}.offline"
                fi
                if [[ "${FAIL_FULL_DEPLOY:-0}" == "1" && " $* " != *" --deploy-scope migration "* ]]; then
                  exit 42
                fi
                if [[ "${TEMPFAIL_FULL_DEPLOY:-0}" == "1" && " $* " != *" --deploy-scope migration "* ]]; then
                  exit 75
                fi
                if [[ -n "${TEST_DEPLOY_MARKER:-}" && " $* " != *" --deploy-scope migration "* ]]; then
                  touch "$TEST_DEPLOY_MARKER"
                fi
                """
            ),
            encoding="utf-8",
        )
        up_script.chmod(0o755)
        run(["git", "add", "."], cwd=self.seed)
        run(["git", "commit", "-m", "fixture"], cwd=self.seed)
        run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.seed)
        run(["git", "push", "-u", "origin", "main"], cwd=self.seed)
        run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "symbolic-ref",
                "HEAD",
                "refs/heads/main",
            ]
        )
        run(["git", "clone", "--no-local", "--branch", "main", str(self.remote), str(self.source)])
        self.commit = run(["git", "rev-parse", "HEAD"], cwd=self.source).stdout.strip()

        (self.source / "build/basemap").mkdir(parents=True)
        (self.source / "build/basemap/map.pmtiles").write_bytes(b"pmtiles")
        germany_pmtiles = self.source / "build/basemap/basemap-germany-fixture.pmtiles"
        germany_meta = self.source / "build/basemap/basemap-germany-fixture.meta.json"
        germany_pmtiles.write_bytes(b"PMTiles" + b"\0" * 120)
        germany_meta.write_text('{"fixture":true}\n', encoding="utf-8")
        (self.source / "build/basemap/basemap-germany.pmtiles").symlink_to(germany_pmtiles.name)
        (self.source / "build/basemap/basemap-germany.meta.json").symlink_to(germany_meta.name)

        self.make_artifact()
        self.make_command_shims()

        run(
            self.privileged(
                [
                    "chown",
                    "-R",
                    "root:root",
                    str(self.source),
                    str(self.state),
                    str(self.runtime_env),
                ]
            )
        )
        run(
            self.privileged(
                [
                    "chmod",
                    "-R",
                    "go-w",
                    str(self.source),
                    str(self.state),
                    str(self.runtime_env),
                ]
            )
        )

    def restore_test_ownership(self) -> None:
        if not self.root.exists():
            return
        try:
            command = self.privileged(
                [
                    "chown",
                    "-R",
                    f"{os.getuid()}:{os.getgid()}",
                    str(self.root),
                ]
            )
        except unittest.SkipTest:
            return
        run(command, check=False)

    @staticmethod
    def privileged(argv: list[str]) -> list[str]:
        if os.geteuid() == 0:
            return argv
        sudo = shutil.which("sudo")
        if sudo is None:
            raise unittest.SkipTest(
                "sudo is required for the root-bound deployment integration test"
            )
        probe = subprocess.run(
            [sudo, "-n", "true"],
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest(
                "passwordless sudo is unavailable for the root-bound deployment integration test"
            )
        return [sudo, "-n", *argv]

    def make_artifact(self, *, basemap_variant: str = "germany") -> None:
        tree = self.root / "artifact-tree/build"
        if tree.parent.exists():
            shutil.rmtree(tree.parent)
        (tree / "_app").mkdir(parents=True)
        (tree / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (tree / "_app/version.json").write_text(
            json.dumps({"commit": self.commit, "version": self.commit[:8]}) + "\n",
            encoding="utf-8",
        )
        germany_style_sha = hashlib.sha256(
            (self.source / "map-style/style-germany.json").read_bytes()
        ).hexdigest()
        if basemap_variant == "germany":
            identity = {
                "schema_version": 1,
                "mode": "local-sovereign",
                "variant": "germany",
                "style_path": "/local-basemap/style-germany.json",
                "source_commit": self.commit,
                "style_sha256": germany_style_sha,
            }
        elif basemap_variant == "regional":
            identity = {
                "schema_version": 1,
                "mode": "local-sovereign",
                "variant": "regional",
                "style_path": "/local-basemap/style.json",
                "source_commit": self.commit,
                "style_sha256": "0" * 64,
            }
        else:
            raise ValueError(f"unsupported fixture basemap variant: {basemap_variant}")
        (tree / "_app/basemap-build.json").write_text(
            json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        staged_artifact = self.root / "web-artifact-staging.tar.gz"
        with tarfile.open(staged_artifact, "w:gz") as bundle:
            bundle.add(tree, arcname="build")
        staged_artifact_sha = run(
            ["sha256sum", str(staged_artifact)]
        ).stdout.split()[0]
        if os.access(self.artifact.parent, os.W_OK):
            staged_artifact.replace(self.artifact)
        else:
            run(
                self.privileged(
                    [
                        "install",
                        "-o",
                        "root",
                        "-g",
                        "root",
                        "-m",
                        "0600",
                        str(staged_artifact),
                        str(self.artifact),
                    ]
                )
            )
            staged_artifact.unlink()
        self.artifact_sha = staged_artifact_sha

    def make_command_shims(self) -> None:
        docker = self.bin / "docker"
        docker.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                expected_config="$WELTGEWEBE_DEPLOY_STATE_ROOT/docker-config"
                [[ "${DOCKER_CONFIG:-}" == "$expected_config" ]] || {
                  printf 'unexpected DOCKER_CONFIG: %s\n' "${DOCKER_CONFIG:-<unset>}" >&2
                  exit 91
                }
                [[ -d "$DOCKER_CONFIG" ]] || {
                  printf 'DOCKER_CONFIG directory is missing\n' >&2
                  exit 92
                }
                [[ "$(stat -c %a "$DOCKER_CONFIG")" == "700" ]] || {
                  printf 'DOCKER_CONFIG mode is not 700\n' >&2
                  exit 93
                }
                if [[ "$1" == "system" && "${2:-}" == "df" ]]; then
                  printf '{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Build Cache"}\n'
                  exit 0
                fi
                cat "$TEST_WEB_ARTIFACT"
                """
            ),
            encoding="utf-8",
        )
        docker.chmod(0o755)

        df = self.bin / "df"
        df.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ "${1:-}" == "-B1" && "${2:-}" == "--output=avail" ]]; then
                  printf 'Avail\n100000000000\n'
                  exit 0
                fi
                exec /usr/bin/df "$@"
                """
            ),
            encoding="utf-8",
        )
        df.chmod(0o755)

        curl = self.bin / "curl"
        curl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                headers=""
                range=""
                url="${!#}"
                while (($#)); do
                  case "$1" in
                    -D)
                      headers="$2"
                      shift 2
                      ;;
                    --range)
                      range="$2"
                      shift 2
                      ;;
                    *) shift ;;
                  esac
                done
                public_commit="${PUBLIC_COMMIT:-$TEST_COMMIT}"
                if [[ -n "${TEST_DEPLOY_MARKER:-}" && -e "$TEST_DEPLOY_MARKER" ]]; then
                  public_commit="$TEST_COMMIT"
                fi
                if [[ "$url" == *"/schaubild/manifest.json" ]]; then
                  if [[ "${TEST_PUBLIC_SCHAUWERK_MANIFEST_BROKEN:-0}" == "1" && ( -z "${TEST_DEPLOY_MARKER:-}" || ! -e "$TEST_DEPLOY_MARKER" ) ]]; then
                    printf '{"fixture":"stale-schauwerk"}\n'
                  else
                    cat "$TEST_SCHAUWERK_MANIFEST"
                  fi
                elif [[ "$url" == *"/api/version" ]]; then
                  if [[ -n "$headers" ]]; then
                    {
                      printf 'HTTP/1.1 200 OK\\r\\n'
                      printf 'X-Weltgewebe-API-Build: %s\\r\\n' "$public_commit"
                      printf 'X-Weltgewebe-Build: %s\\r\\n' "${public_commit:0:8}"
                      printf '\\r\\n'
                    } > "$headers"
                  fi
                  printf '{"commit":"%s","version":"0.1.0"}\\n' "$public_commit"
                elif [[ "$url" == *"/_app/basemap-build.json" ]]; then
                  public_variant="${TEST_PUBLIC_BASEMAP_VARIANT:-germany}"
                  if [[ -n "${TEST_DEPLOY_MARKER:-}" && -e "$TEST_DEPLOY_MARKER" ]]; then
                    public_variant="germany"
                  fi
                  if [[ "$public_variant" == "germany" ]]; then
                    style_sha="$(sha256sum "$WELTGEWEBE_SOURCE_CHECKOUT/map-style/style-germany.json" | awk '{print $1}')"
                    printf '{"schema_version":1,"mode":"local-sovereign","variant":"germany","style_path":"/local-basemap/style-germany.json","source_commit":"%s","style_sha256":"%s"}\\n' \
                      "$public_commit" "$style_sha"
                  else
                    printf '{"schema_version":1,"mode":"local-sovereign","variant":"regional","style_path":"/local-basemap/style.json","source_commit":"%s","style_sha256":"%064d"}\\n' \
                      "$public_commit" 0
                  fi
                elif [[ "$url" == *"/local-basemap/style-germany.json" ]]; then
                  if [[ "${TEST_PUBLIC_LIGHT_STYLE_BROKEN:-0}" == "1" ]]; then
                    printf '{"name":"broken light style"}\\n'
                  else
                    cat "$WELTGEWEBE_SOURCE_CHECKOUT/map-style/style-germany.json"
                  fi
                elif [[ "$url" == *"/local-basemap/style-germany-dark.json" ]]; then
                  if [[ "${TEST_PUBLIC_DARK_STYLE_BROKEN:-0}" == "1" ]]; then
                    printf '{"name":"broken dark style"}\\n'
                  else
                    cat "$WELTGEWEBE_SOURCE_CHECKOUT/map-style/style-germany-dark.json"
                  fi
                elif [[ "$url" == *"/local-basemap/basemap-germany.pmtiles" ]]; then
                  [[ "$range" == "0-126" ]] || exit 98
                  pmtiles="$WELTGEWEBE_SOURCE_CHECKOUT/build/basemap/basemap-germany.pmtiles"
                  size="$(stat -Lc %s "$pmtiles")"
                  if [[ "${TEST_PUBLIC_PMTILES_RANGE_BROKEN:-0}" == "1" ]]; then
                    {
                      printf 'HTTP/1.1 200 OK\\r\\n'
                      printf 'Content-Type: application/octet-stream\\r\\n'
                      printf 'Content-Length: 127\\r\\n'
                      printf '\\r\\n'
                    } > "$headers"
                  else
                    {
                      printf 'HTTP/1.1 206 Partial Content\\r\\n'
                      printf 'Content-Type: application/octet-stream\\r\\n'
                      printf 'Accept-Ranges: bytes\\r\\n'
                      printf 'Content-Range: bytes 0-126/%s\\r\\n' "$size"
                      printf 'Content-Length: 127\\r\\n'
                      printf '\\r\\n'
                    } > "$headers"
                  fi
                  head -c 127 "$pmtiles"
                else
                  printf '{"commit":"%s","version":"%s"}\\n' \
                    "$public_commit" "${public_commit:0:8}"
                fi
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)

        verifier = self.bin / "verify-public"
        verifier.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from datetime import datetime, timezone
                from pathlib import Path

                args = sys.argv[1:]
                commit = args[args.index("--expected-commit") + 1]
                output = Path(args[args.index("--output") + 1])
                marker = os.environ.get("TEST_DEPLOY_MARKER")
                deployed = marker is None or Path(marker).exists()
                observed_commit = commit if deployed else os.environ["PUBLIC_COMMIT"]
                passed = observed_commit == commit
                artifact_tree = {
                    "schema_version": 1,
                    "sha256": "a" * 64,
                    "file_count": 103,
                    "compile_revision": observed_commit,
                    "provenance": "unattested",
                    "error": None,
                }
                payload = {
                    "schema_version": int(
                        os.environ.get("TEST_PUBLIC_VERIFICATION_SCHEMA", "3")
                    ),
                    "expected_commit": commit,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "pass": passed,
                    "status": os.environ.get(
                        "TEST_PUBLIC_VERIFICATION_STATUS",
                        "consistency_pass_unattested" if passed else "failed",
                    ),
                    "pass_scope": os.environ.get(
                        "TEST_PUBLIC_PASS_SCOPE",
                        "identity_and_declared_artifact_consistency" if passed else "none",
                    ),
                    "identity_verified": passed,
                    "artifact_tree_declaration_verified": passed,
                    "attestation_verified": (
                        os.environ.get("TEST_PUBLIC_ATTESTATION_VERIFIED", "0") == "1"
                    ),
                    "provenance_status": "unattested",
                    "reasons": [] if passed else ["public commit differs"],
                    "limitations": [
                        "the public verifier does not reconstruct the deployed artifact tree",
                        "artifact provenance is explicitly unattested",
                    ] if passed else [],
                    "frontend": {
                        "url": "https://example.invalid/_app/version.json",
                        "status": 200,
                        "commit": observed_commit,
                        "version": observed_commit[:8],
                        "headers": {"cache-control": "no-store"},
                        "error": None,
                        "artifact_tree": artifact_tree,
                    },
                    "api": {
                        "url": "https://example.invalid/api/version",
                        "status": 200,
                        "commit": observed_commit,
                        "version": "0.1.0",
                        "headers": {
                            "x-weltgewebe-api-build": observed_commit,
                            "x-weltgewebe-build": observed_commit[:8],
                        },
                        "error": None,
                        "artifact_tree": None,
                    },
                }
                if os.environ.get("TEST_PUBLIC_EXTRA_FIELD"):
                    payload["untrusted_extension"] = "value"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
                raise SystemExit(0 if passed else 1)
                """
            ),
            encoding="utf-8",
        )
        verifier.chmod(0o755)

        blocking_verifier = self.bin / "blocking-verify-public"
        blocking_verifier.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                : > "$TEST_LOCK_OWNER_READY"
                while [[ ! -e "$TEST_LOCK_OWNER_RELEASE" ]]; do
                  sleep 0.02
                done
                exec "$TEST_VERIFY_PUBLIC" "$@"
                """
            ),
            encoding="utf-8",
        )
        blocking_verifier.chmod(0o755)

        forbidden_deploy = self.bin / "forbidden-deploy"
        forbidden_deploy.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
        forbidden_deploy.chmod(0o755)

        tempfail_deploy = self.bin / "tempfail-deploy"
        tempfail_deploy.write_text("#!/usr/bin/env bash\nexit 75\n", encoding="utf-8")
        tempfail_deploy.chmod(0o755)

        inherited_deploy = self.bin / "inherited-deploy"
        inherited_deploy.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                exec 9<> "$TEST_INHERITED_LOCK_TARGET"
                flock -n 9
                if [[ "${TEST_CLOSE_INHERITED_FD:-0}" == "1" ]]; then
                  exec 9>&-
                fi
                exec "$TEST_DEPLOY_SCRIPT" "$@"
                """
            ),
            encoding="utf-8",
        )
        inherited_deploy.chmod(0o755)

        exit_code_deploy = self.bin / "exit-code-deploy"
        exit_code_deploy.write_text(
            '#!/usr/bin/env bash\nexit "${TEST_DEPLOY_EXIT_CODE:?}"\n',
            encoding="utf-8",
        )
        exit_code_deploy.chmod(0o755)

        terminal_receipt_deploy = self.bin / "terminal-receipt-deploy"
        terminal_receipt_deploy.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                commit = args[args.index("--commit") + 1]
                receipt = (
                    Path(os.environ["WELTGEWEBE_DEPLOY_STATE_ROOT"])
                    / "receipts"
                    / f"{commit}.json"
                )
                receipt.parent.mkdir(parents=True, exist_ok=True)
                schema_version = int(os.environ.get("TEST_DEPLOY_RECEIPT_SCHEMA", "5"))
                payload = {
                    "schema_version": schema_version,
                    "commit": commit,
                    "result": os.environ["TEST_DEPLOY_RECEIPT_RESULT"],
                    "lock_domain": "weltgewebe-production-deployment-v1",
                    "lock_owner_entrypoint": "reconciler",
                    "lock_handoff": "inherited",
                    "deploy_invocation_id": os.environ[
                        "WELTGEWEBE_DEPLOY_INVOCATION_ID"
                    ],
                }
                if schema_version != 5:
                    payload.pop("deploy_invocation_id")
                receipt.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
                shape = os.environ.get("TEST_DEPLOY_RECEIPT_SHAPE", "regular")
                if shape == "mode":
                    receipt.chmod(0o666)
                elif shape == "owner":
                    os.chown(receipt, 65534, 65534)
                elif shape == "hardlink":
                    os.link(receipt, receipt.with_name(f"{receipt.name}.alias"))
                elif shape == "symlink":
                    receipt.unlink()
                    receipt.symlink_to(Path(os.environ["WELTGEWEBE_RUNTIME_ENV"]))
                elif shape == "oversize":
                    receipt.write_bytes(b"x" * 1048577)
                elif shape != "regular":
                    raise SystemExit(f"unknown receipt shape: {shape}")
                raise SystemExit(int(os.environ["TEST_DEPLOY_EXIT_CODE"]))
                """
            ),
            encoding="utf-8",
        )
        terminal_receipt_deploy.chmod(0o755)

        contention_receipt_deploy = self.bin / "contention-receipt-deploy"
        contention_receipt_deploy.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                commit = args[args.index("--commit") + 1]
                receipt = (
                    Path(os.environ["WELTGEWEBE_DEPLOY_STATE_ROOT"])
                    / "receipts"
                    / "last-contention.json"
                )
                receipt.parent.mkdir(parents=True, exist_ok=True)
                schema_version = int(os.environ.get("TEST_CONTENTION_SCHEMA", "2"))
                payload = {
                    "schema_version": schema_version,
                    "kind": "weltgewebe_production_lock_contention",
                    "requested_commit": commit,
                    "lock_domain": "weltgewebe-production-deployment-v1",
                    "entrypoint": "reconciler",
                    "result": "already_running",
                    "deploy_invocation_id": os.environ[
                        "WELTGEWEBE_DEPLOY_INVOCATION_ID"
                    ],
                }
                if schema_version != 2:
                    payload.pop("deploy_invocation_id")
                receipt.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
                shape = os.environ.get("TEST_CONTENTION_RECEIPT_SHAPE", "regular")
                if shape == "mode":
                    receipt.chmod(0o666)
                elif shape == "owner":
                    os.chown(receipt, 65534, 65534)
                elif shape == "hardlink":
                    os.link(receipt, receipt.with_name(f"{receipt.name}.alias"))
                elif shape == "symlink":
                    receipt.unlink()
                    receipt.symlink_to(Path(os.environ["WELTGEWEBE_RUNTIME_ENV"]))
                elif shape == "oversize":
                    receipt.write_bytes(b"x" * 1048577)
                elif shape != "regular":
                    raise SystemExit(f"unknown contention receipt shape: {shape}")
                raise SystemExit(75)
                """
            ),
            encoding="utf-8",
        )
        contention_receipt_deploy.chmod(0o755)

        lock_contention_deploy = self.bin / "lock-contention-deploy"
        lock_contention_deploy.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                exec 9<> "$WELTGEWEBE_DEPLOY_STATE_ROOT/production-deployment.lock"
                exec "$TEST_DEPLOY_SCRIPT" "$@"
                """
            ),
            encoding="utf-8",
        )
        lock_contention_deploy.chmod(0o755)

        successful_deploy = self.bin / "successful-deploy"
        successful_deploy.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            ': "${TEST_DEPLOY_MARKER:?}"\n'
            'touch "$TEST_DEPLOY_MARKER"\n',
            encoding="utf-8",
        )
        successful_deploy.chmod(0o755)

    def base_environment(self) -> dict[str, str]:
        inherited_path = os.environ.get("PATH", "/usr/bin:/bin")
        return {
            **GIT_MAINTENANCE_ENV,
            "PATH": f"{self.bin}:{inherited_path}",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "WELTGEWEBE_SOURCE_CHECKOUT": str(self.source),
            "WELTGEWEBE_RELEASE_ROOT": str(self.releases),
            "WELTGEWEBE_RUNTIME_ENV": str(self.runtime_env),
            "WELTGEWEBE_DEPLOY_STATE_ROOT": str(self.state),
            "WELTGEWEBE_ARCHIVE_VALIDATOR": str(VALIDATOR),
            "WELTGEWEBE_FRONTEND_VERSION_URL": (
                "https://example.invalid/_app/version.json"
            ),
            "WELTGEWEBE_FRONTEND_BASEMAP_IDENTITY_URL": (
                "https://example.invalid/_app/basemap-build.json"
            ),
            "WELTGEWEBE_FRONTEND_BASEMAP_LIGHT_STYLE_URL": (
                "https://example.invalid/local-basemap/style-germany.json"
            ),
            "WELTGEWEBE_FRONTEND_BASEMAP_DARK_STYLE_URL": (
                "https://example.invalid/local-basemap/style-germany-dark.json"
            ),
            "WELTGEWEBE_FRONTEND_BASEMAP_PMTILES_URL": (
                "https://example.invalid/local-basemap/basemap-germany.pmtiles"
            ),
            "WELTGEWEBE_API_VERSION_URL": "https://example.invalid/api/version",
            "WELTGEWEBE_SCHAUWERK_MANIFEST_URL": (
                "https://example.invalid/schaubild/manifest.json"
            ),
            "TEST_COMMIT": self.commit,
            "TEST_SCHAUWERK_MANIFEST": str(
                getattr(self, "schauwerk_manifest", self.root / "schauwerk-manifest.json")
            ),
            "TEST_REMOTE": str(self.remote),
            "TEST_WEB_ARTIFACT": str(self.artifact),
            "TEST_UP_LOG": str(self.root / "weltgewebe-up.log"),
            "TEST_UP_UMASK_LOG": str(self.root / "weltgewebe-up-umask.log"),
        }

    def deploy(
        self,
        *,
        advance: bool,
        advance_after_migration: bool = False,
        break_remote_after_migration: bool = False,
        fail_full: bool = False,
        umask: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        deploy_env = self.base_environment()
        deploy_env["ADVANCE_REMOTE_ON_DEPLOY"] = "1" if advance else "0"
        deploy_env["ADVANCE_REMOTE_ON_MIGRATION"] = (
            "1" if advance_after_migration else "0"
        )
        deploy_env["BREAK_REMOTE_ON_MIGRATION"] = (
            "1" if break_remote_after_migration else "0"
        )
        deploy_env["FAIL_FULL_DEPLOY"] = "1" if fail_full else "0"
        command = [
            "env",
            *[f"{key}={value}" for key, value in deploy_env.items()],
            str(DEPLOY_SCRIPT),
            "--commit",
            self.commit,
            "--web-artifact",
            str(self.artifact),
            "--web-sha256",
            self.artifact_sha,
        ]
        if umask is not None:
            command = [
                "bash",
                "-c",
                'umask "$1"; shift; exec "$@"',
                "bash",
                umask,
                *command,
            ]
        return run(self.privileged(command), check=False)

    def deploy_with_inherited_handoff(
        self,
        *,
        environment_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        lock_target = self.state / "production-deployment.lock"
        deploy_env = self.base_environment()
        deploy_env.update(
            {
                "WELTGEWEBE_PRODUCTION_LOCK_FD": "9",
                "WELTGEWEBE_PRODUCTION_LOCK_DOMAIN": (
                    "weltgewebe-production-deployment-v1"
                ),
                "WELTGEWEBE_PRODUCTION_LOCK_OWNER_ENTRYPOINT": "reconciler",
                "WELTGEWEBE_DEPLOY_INVOCATION_ID": "a" * 64,
                "TEST_INHERITED_LOCK_TARGET": str(lock_target),
                "TEST_DEPLOY_SCRIPT": str(DEPLOY_SCRIPT),
            }
        )
        if environment_overrides:
            deploy_env.update(environment_overrides)
        argv = self.privileged(
            [
                "env",
                *[f"{key}={value}" for key, value in deploy_env.items()],
                str(self.bin / "inherited-deploy"),
                "--commit",
                self.commit,
                "--web-artifact",
                str(self.artifact),
                "--web-sha256",
                self.artifact_sha,
            ]
        )
        return run(argv, check=False)

    def install_root_text(self, path: Path, content: str) -> None:
        source = (
            self.root
            / f"receipt-source-{len(list(self.root.glob('receipt-source-*')))}.json"
        )
        source.write_text(content, encoding="utf-8")
        run(
            self.privileged(
                [
                    "install",
                    "-d",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0700",
                    str(path.parent),
                ]
            )
        )
        run(
            self.privileged(
                [
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0600",
                    str(source),
                    str(path),
                ]
            )
        )

    def install_root_json(self, path: Path, payload: dict[str, object]) -> None:
        self.install_root_text(path, json.dumps(payload) + "\n")

    def reconcile_existing_public_commit(
        self, *, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        reconcile_env = self.base_environment()
        reconcile_env.update(
            {
                "WELTGEWEBE_BUILD_USER": "root",
                "WELTGEWEBE_LIVE_VERIFIER": str(self.bin / "verify-public"),
                "WELTGEWEBE_DEPLOY_HELPER": str(self.bin / "forbidden-deploy"),
                "WELTGEWEBE_MIN_FREE_KIB": "1",
            }
        )
        if extra_env:
            reconcile_env.update(extra_env)
        argv = self.privileged(
            [
                "env",
                *[f"{key}={value}" for key, value in reconcile_env.items()],
                str(RECONCILE_SCRIPT),
            ]
        )
        return run(argv, check=False)

    def reconcile_stale_public_commit(
        self,
        *,
        advance_after_migration: bool = False,
        advance_during_deploy: bool = False,
        deploy_helper: Path = DEPLOY_SCRIPT,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        reconcile_env = self.base_environment()
        reconcile_env.update(
            {
                "WELTGEWEBE_BUILD_USER": "root",
                "WELTGEWEBE_LIVE_VERIFIER": str(self.bin / "verify-public"),
                "WELTGEWEBE_DEPLOY_HELPER": str(deploy_helper),
                "WELTGEWEBE_MIN_FREE_KIB": "1",
                "PUBLIC_COMMIT": "0" * 40,
                "TEST_DEPLOY_MARKER": str(self.root / "deploy-complete"),
                "ADVANCE_REMOTE_ON_MIGRATION": (
                    "1" if advance_after_migration else "0"
                ),
                "ADVANCE_REMOTE_ON_DEPLOY": "1" if advance_during_deploy else "0",
            }
        )
        if extra_env:
            reconcile_env.update(extra_env)
        argv = self.privileged(
            [
                "env",
                *[f"{key}={value}" for key, value in reconcile_env.items()],
                str(RECONCILE_SCRIPT),
            ]
        )
        return run(argv, check=False)

    def test_reconciler_repairs_same_commit_regional_public_bundle(self) -> None:
        marker = self.root / "deploy-complete"
        result = self.reconcile_existing_public_commit(
            extra_env={
                "WELTGEWEBE_DEPLOY_HELPER": str(DEPLOY_SCRIPT),
                "PUBLIC_COMMIT": self.commit,
                "TEST_PUBLIC_BASEMAP_VARIANT": "regional",
                "TEST_DEPLOY_MARKER": str(marker),
            }
        )
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        self.assertIn("reason=basemap_identity_drift", result.stdout)
        self.assertIn("production_reconcile=verified", result.stdout)
        self.assertNotIn("production_reconcile=noop", result.stdout)

    def test_reconciler_keeps_same_commit_germany_public_bundle_as_noop(self) -> None:
        result = self.reconcile_existing_public_commit(
            extra_env={"PUBLIC_COMMIT": self.commit}
        )
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("production_reconcile=noop", result.stdout)
        self.assertIn("basemap_variant=germany", result.stdout)
        self.assertIn("schauwerk_release=verified", result.stdout)
        self.assertNotIn("reason=basemap_identity_drift", result.stdout)
        self.assertNotIn("reason=schauwerk_release_identity_drift", result.stdout)

    def test_reconciler_repairs_same_commit_with_stale_public_schauwerk_release(self) -> None:
        marker = self.root / "deploy-complete"
        result = self.reconcile_existing_public_commit(
            extra_env={
                "WELTGEWEBE_DEPLOY_HELPER": str(DEPLOY_SCRIPT),
                "PUBLIC_COMMIT": self.commit,
                "TEST_PUBLIC_SCHAUWERK_MANIFEST_BROKEN": "1",
                "TEST_DEPLOY_MARKER": str(marker),
            }
        )
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        self.assertIn("reason=schauwerk_release_identity_drift", result.stdout)
        self.assertIn("production_reconcile=verified", result.stdout)
        self.assertNotIn("production_reconcile=noop", result.stdout)

    def assert_reconciler_rejects_broken_public_germany_delivery(
        self, environment_flag: str
    ) -> None:
        result = self.reconcile_existing_public_commit(
            extra_env={"PUBLIC_COMMIT": self.commit, environment_flag: "1"}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=basemap_identity_drift", result.stdout)
        self.assertNotIn("production_reconcile=noop", result.stdout)

    def test_reconciler_rejects_same_commit_with_broken_public_germany_light_style(
        self,
    ) -> None:
        self.assert_reconciler_rejects_broken_public_germany_delivery(
            "TEST_PUBLIC_LIGHT_STYLE_BROKEN"
        )

    def test_reconciler_rejects_same_commit_with_broken_public_germany_dark_style(
        self,
    ) -> None:
        self.assert_reconciler_rejects_broken_public_germany_delivery(
            "TEST_PUBLIC_DARK_STYLE_BROKEN"
        )

    def test_reconciler_rejects_same_commit_without_public_germany_pmtiles_range(
        self,
    ) -> None:
        self.assert_reconciler_rejects_broken_public_germany_delivery(
            "TEST_PUBLIC_PMTILES_RANGE_BROKEN"
        )

    def test_reconciler_rejects_same_commit_public_bundle_with_missing_germany_pmtiles_alias(
        self,
    ) -> None:
        run(
            self.privileged(
                [
                    "rm",
                    "-f",
                    str(self.source / "build/basemap/basemap-germany.pmtiles"),
                ]
            )
        )
        result = self.reconcile_existing_public_commit(
            extra_env={"PUBLIC_COMMIT": self.commit}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("production_reconcile=noop", result.stdout)
        self.assertIn(
            "required nationwide Germany basemap alias is missing: basemap-germany.pmtiles",
            result.stderr,
        )

    def test_reconciler_rejects_same_commit_public_bundle_with_broken_germany_meta_alias(
        self,
    ) -> None:
        meta_alias = self.source / "build/basemap/basemap-germany.meta.json"
        run(self.privileged(["rm", "-f", str(meta_alias)]))
        run(
            self.privileged(
                [
                    "ln",
                    "-s",
                    "missing-germany.meta.json",
                    str(meta_alias),
                ]
            )
        )
        result = self.reconcile_existing_public_commit(
            extra_env={"PUBLIC_COMMIT": self.commit}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("production_reconcile=noop", result.stdout)
        self.assertIn(
            "required nationwide Germany basemap alias is broken: basemap-germany.meta.json",
            result.stderr,
        )

    def test_reconciler_rejects_a_regional_frontend_artifact(self) -> None:
        self.make_artifact(basemap_variant="regional")
        result = self.reconcile_stale_public_commit()
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("frontend basemap build identity mismatch", result.stderr)
        self.assertFalse((self.root / "deploy-complete").exists())

    def test_reconciler_exports_private_docker_config_to_build(self) -> None:
        result = self.reconcile_stale_public_commit()
        docker_config = self.state / "docker-config"
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(docker_config.stat().st_uid, 0)
        self.assertEqual(docker_config.stat().st_gid, 0)
        self.assertEqual(docker_config.stat().st_mode & 0o777, 0o700)
        self.restore_test_ownership()
        receipt = json.loads(
            (self.state / "reconcile-receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "verified")
        deployment_receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(
            deployment_receipt["lock_domain"], "weltgewebe-production-deployment-v1"
        )
        self.assertEqual(deployment_receipt["lock_owner_entrypoint"], "reconciler")
        self.assertEqual(deployment_receipt["lock_handoff"], "inherited")
        self.assertRegex(
            deployment_receipt["deploy_invocation_id"],
            r"^[0-9a-f]{64}$",
        )

    def test_reconciler_projects_migration_only_supersession_exactly(self) -> None:
        result = self.reconcile_stale_public_commit(advance_after_migration=True)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("production_reconcile=superseded_after_migration", result.stdout)
        deploy_receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        reconcile_receipt = json.loads(
            (self.state / "reconcile-receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(deploy_receipt["result"], "superseded_after_migration")
        self.assertEqual(reconcile_receipt["result"], "superseded_after_migration")
        self.assertIn("full deploy skipped", reconcile_receipt["detail"])
        deploy_calls = (self.root / "weltgewebe-up.log").read_text().splitlines()
        self.assertEqual(len(deploy_calls), 1)
        self.assertIn("--deploy-scope migration", deploy_calls[0])
        self.assertFalse((self.root / "deploy-complete").exists())

    def test_reconciler_projects_post_deploy_supersession_exactly(self) -> None:
        result = self.reconcile_stale_public_commit(advance_during_deploy=True)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("production_reconcile=superseded_after_deploy", result.stdout)
        deploy_receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        reconcile_receipt = json.loads(
            (self.state / "reconcile-receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(deploy_receipt["result"], "superseded_after_deploy")
        self.assertEqual(reconcile_receipt["result"], "superseded_after_deploy")
        self.assertIn("full deploy completed", reconcile_receipt["detail"])
        deploy_calls = (self.root / "weltgewebe-up.log").read_text().splitlines()
        self.assertEqual(len(deploy_calls), 2)
        self.assertTrue((self.root / "deploy-complete").exists())

    def test_reconciler_rejects_unbound_tempfail_without_supersession(self) -> None:
        result = self.reconcile_stale_public_commit(
            deploy_helper=self.bin / "tempfail-deploy"
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "unexplained temporary failure under inherited production lock",
            result.stderr,
        )
        receipt = json.loads(
            (self.state / "reconcile-receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "failed")
        self.assertFalse((self.state / "receipts" / f"{self.commit}.json").exists())

    def test_deploy_helper_rejects_each_invalid_inherited_handoff_branch(self) -> None:
        cases = (
            (
                "descriptor number",
                {"WELTGEWEBE_PRODUCTION_LOCK_FD": "8"},
                "inherited production lock descriptor is invalid",
            ),
            (
                "domain",
                {"WELTGEWEBE_PRODUCTION_LOCK_DOMAIN": "other-domain"},
                "inherited production lock domain is invalid",
            ),
            (
                "owner",
                {"WELTGEWEBE_PRODUCTION_LOCK_OWNER_ENTRYPOINT": "deploy-helper"},
                "inherited production lock owner is invalid",
            ),
            (
                "invocation identity",
                {"WELTGEWEBE_DEPLOY_INVOCATION_ID": ""},
                "inherited deploy invocation identity is invalid",
            ),
            (
                "closed descriptor",
                {"TEST_CLOSE_INHERITED_FD": "1"},
                "inherited production lock descriptor is not open",
            ),
            (
                "lock target",
                {
                    "TEST_INHERITED_LOCK_TARGET": str(
                        self.state / "wrong-production.lock"
                    )
                },
                "inherited production lock targets an unexpected file",
            ),
        )
        for label, overrides, expected_error in cases:
            with self.subTest(label=label):
                result = self.deploy_with_inherited_handoff(
                    environment_overrides=overrides
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def test_reconciler_rejects_stale_terminal_receipt_for_new_invocation(self) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            {
                "schema_version": 5,
                "commit": self.commit,
                "result": "superseded_after_migration",
                "lock_domain": "weltgewebe-production-deployment-v1",
                "lock_owner_entrypoint": "reconciler",
                "lock_handoff": "inherited",
                "deploy_invocation_id": "b" * 64,
            },
        )
        result = self.reconcile_stale_public_commit(
            deploy_helper=self.bin / "exit-code-deploy",
            extra_env={"TEST_DEPLOY_EXIT_CODE": "79"},
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "terminal deployment receipt does not match current invocation",
            result.stderr,
        )

    def test_reconciler_rejects_both_exit_result_mismatches(self) -> None:
        cases = (
            (79, "superseded_after_deploy"),
            (80, "superseded_after_migration"),
        )
        for exit_code, result_value in cases:
            with self.subTest(exit_code=exit_code, result=result_value):
                result = self.reconcile_stale_public_commit(
                    deploy_helper=self.bin / "terminal-receipt-deploy",
                    extra_env={
                        "TEST_DEPLOY_EXIT_CODE": str(exit_code),
                        "TEST_DEPLOY_RECEIPT_RESULT": result_value,
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("deploy helper exit/result mismatch", result.stderr)
        self.restore_test_ownership()

    def test_reconciler_identifies_child_tempfail_from_current_failed_receipt(
        self,
    ) -> None:
        contention = self.state / "receipts" / "last-contention.json"
        run(
            self.privileged(
                [
                    "install",
                    "-d",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0700",
                    str(contention.parent),
                ]
            )
        )
        run(self.privileged(["ln", "-s", str(self.runtime_env), str(contention)]))
        result = self.reconcile_stale_public_commit(
            extra_env={"TEMPFAIL_FULL_DEPLOY": "1"}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "child temporary failure after writing a failed receipt for the current invocation",
            result.stderr,
        )
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "failed")
        self.assertRegex(receipt["deploy_invocation_id"], r"^[0-9a-f]{64}$")

    def test_reconciler_ignores_stale_failed_receipt_for_tempfail(self) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            {
                "schema_version": 5,
                "commit": self.commit,
                "result": "failed",
                "lock_domain": "weltgewebe-production-deployment-v1",
                "lock_owner_entrypoint": "reconciler",
                "lock_handoff": "inherited",
                "deploy_invocation_id": "b" * 64,
            },
        )
        result = self.reconcile_stale_public_commit(
            deploy_helper=self.bin / "exit-code-deploy",
            extra_env={"TEST_DEPLOY_EXIT_CODE": "75"},
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "unexplained temporary failure under inherited production lock",
            result.stderr,
        )
        self.assertNotIn("child temporary failure", result.stderr)

    def test_reconciler_rejects_each_unsafe_tempfail_receipt_shape(self) -> None:
        for shape in ("mode", "owner", "hardlink", "symlink", "oversize"):
            with self.subTest(shape=shape):
                self.remove_deploy_test_receipts()
                result = self.reconcile_stale_public_commit(
                    deploy_helper=self.bin / "terminal-receipt-deploy",
                    extra_env={
                        "TEST_DEPLOY_EXIT_CODE": "75",
                        "TEST_DEPLOY_RECEIPT_RESULT": "failed",
                        "TEST_DEPLOY_RECEIPT_SHAPE": shape,
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "deploy helper returned temporary failure with unsafe receipt evidence",
                    result.stderr,
                )
        self.restore_test_ownership()

    def test_reconciler_identifies_actual_inherited_lock_contention(self) -> None:
        result = self.reconcile_stale_public_commit(
            deploy_helper=self.bin / "lock-contention-deploy",
            extra_env={"TEST_DEPLOY_SCRIPT": str(DEPLOY_SCRIPT)},
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "production lock contention during inherited handoff",
            result.stderr,
        )
        receipt = json.loads(
            (self.state / "receipts" / "last-contention.json").read_text()
        )
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["requested_commit"], self.commit)
        self.assertRegex(receipt["deploy_invocation_id"], r"^[0-9a-f]{64}$")

    def remove_deploy_test_receipts(self) -> None:
        receipt_root = self.state / "receipts"
        run(
            self.privileged(
                [
                    "rm",
                    "-f",
                    str(receipt_root / f"{self.commit}.json"),
                    str(receipt_root / f"{self.commit}.json.alias"),
                    str(receipt_root / "last-contention.json"),
                    str(receipt_root / "last-contention.json.alias"),
                ]
            )
        )

    def test_reconciler_rejects_unsafe_terminal_receipt_shapes(self) -> None:
        for shape in ("mode", "owner", "hardlink", "symlink", "oversize"):
            with self.subTest(shape=shape):
                self.remove_deploy_test_receipts()
                result = self.reconcile_stale_public_commit(
                    deploy_helper=self.bin / "terminal-receipt-deploy",
                    extra_env={
                        "TEST_DEPLOY_EXIT_CODE": "79",
                        "TEST_DEPLOY_RECEIPT_RESULT": "superseded_after_migration",
                        "TEST_DEPLOY_RECEIPT_SHAPE": shape,
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("terminal deployment receipt is unsafe", result.stderr)
        self.restore_test_ownership()

    def test_reconciler_rejects_schema4_terminal_receipt(self) -> None:
        result = self.reconcile_stale_public_commit(
            deploy_helper=self.bin / "terminal-receipt-deploy",
            extra_env={
                "TEST_DEPLOY_EXIT_CODE": "79",
                "TEST_DEPLOY_RECEIPT_RESULT": "superseded_after_migration",
                "TEST_DEPLOY_RECEIPT_SCHEMA": "4",
            },
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "terminal deployment receipt schema is not current", result.stderr
        )

    def test_reconciler_rejects_unsafe_contention_receipt_shapes(self) -> None:
        for shape in ("mode", "owner", "hardlink", "symlink", "oversize"):
            with self.subTest(shape=shape):
                self.remove_deploy_test_receipts()
                result = self.reconcile_stale_public_commit(
                    deploy_helper=self.bin / "contention-receipt-deploy",
                    extra_env={"TEST_CONTENTION_RECEIPT_SHAPE": shape},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe receipt evidence", result.stderr)
        self.restore_test_ownership()

    def test_reconciler_rejects_schema1_contention_receipt(self) -> None:
        result = self.reconcile_stale_public_commit(
            deploy_helper=self.bin / "contention-receipt-deploy",
            extra_env={"TEST_CONTENTION_SCHEMA": "1"},
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "unexplained temporary failure under inherited production lock",
            result.stderr,
        )

    def start_blocking_reconciler(self) -> tuple[subprocess.Popen[str], Path]:
        ready = self.root / "lock-owner-ready"
        release = self.root / "lock-owner-release"
        reconcile_env = self.base_environment()
        reconcile_env.update(
            {
                "WELTGEWEBE_BUILD_USER": "root",
                "WELTGEWEBE_LIVE_VERIFIER": str(self.bin / "blocking-verify-public"),
                "WELTGEWEBE_DEPLOY_HELPER": str(self.bin / "forbidden-deploy"),
                "WELTGEWEBE_MIN_FREE_KIB": "1",
                "TEST_LOCK_OWNER_READY": str(ready),
                "TEST_LOCK_OWNER_RELEASE": str(release),
                "TEST_VERIFY_PUBLIC": str(self.bin / "verify-public"),
            }
        )
        argv = self.privileged(
            [
                "env",
                *[f"{key}={value}" for key, value in reconcile_env.items()],
                str(RECONCILE_SCRIPT),
            ]
        )
        process = subprocess.Popen(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(250):
            if ready.exists():
                return process, release
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f"blocking reconciler exited early ({process.returncode})\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            __import__("time").sleep(0.02)
        process.terminate()
        process.wait(timeout=5)
        raise AssertionError("blocking reconciler did not acquire the production lock")

    def finish_blocking_reconciler(
        self, process: subprocess.Popen[str], release: Path
    ) -> tuple[str, str]:
        release.write_text("release\n", encoding="utf-8")
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"blocking reconciler did not finish\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        self.assertEqual(process.returncode, 0, stderr)
        return stdout, stderr

    def test_timer_and_installer_reconcile_starts_share_one_owner(self) -> None:
        owner, release = self.start_blocking_reconciler()
        try:
            contender = self.reconcile_existing_public_commit()
            self.assertEqual(contender.returncode, 0, contender.stderr)
            self.assertIn("production_reconcile=already_running", contender.stdout)
        finally:
            self.finish_blocking_reconciler(owner, release)
        self.restore_test_ownership()
        receipt = json.loads(
            (self.state / "reconcile-receipts" / "last-contention.json").read_text()
        )
        self.assertEqual(receipt["kind"], "weltgewebe_production_lock_contention")
        self.assertEqual(receipt["entrypoint"], "reconciler")
        self.assertEqual(receipt["result"], "already_running")
        self.assertEqual(receipt["lock_domain"], "weltgewebe-production-deployment-v1")
        self.assertFalse((self.root / "weltgewebe-up.log").exists())

    def test_reconciler_owner_rejects_direct_helper_without_deploy_effects(
        self,
    ) -> None:
        owner, release = self.start_blocking_reconciler()
        try:
            contender = self.deploy(advance=False)
            self.assertEqual(contender.returncode, 75, contender.stderr)
            self.assertIn("production_deployment=already_running", contender.stderr)
        finally:
            self.finish_blocking_reconciler(owner, release)
        self.restore_test_ownership()
        receipt = json.loads(
            (self.state / "receipts" / "last-contention.json").read_text()
        )
        self.assertEqual(receipt["kind"], "weltgewebe_production_lock_contention")
        self.assertEqual(receipt["entrypoint"], "deploy-helper")
        self.assertEqual(receipt["requested_commit"], self.commit)
        self.assertEqual(receipt["result"], "already_running")
        self.assertEqual(receipt["lock_domain"], "weltgewebe-production-deployment-v1")
        self.assertFalse((self.root / "weltgewebe-up.log").exists())

    def test_stale_lock_file_without_owner_does_not_block_deploy(self) -> None:
        lock_path = self.state / "production-deployment.lock"
        run(self.privileged(["touch", str(lock_path)]))
        run(self.privileged(["chmod", "0600", str(lock_path)]))
        result = self.deploy(advance=False)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "verified")
        self.assertEqual(receipt["lock_handoff"], "direct")

    def test_success_marks_exact_commit_current(self) -> None:
        result = self.deploy(advance=False)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy_calls = (
            (self.root / "weltgewebe-up.log").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(len(deploy_calls), 2)
        self.assertIn("--deploy-scope migration", deploy_calls[0])
        self.assertNotIn("--with-caddy", deploy_calls[0])
        self.assertIn("--with-caddy", deploy_calls[1])
        self.assertNotIn("--deploy-scope migration", deploy_calls[1])
        receipt_path = self.state / "receipts" / f"{self.commit}.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["result"], "verified")
        self.assertEqual(receipt["schema_version"], 5)
        self.assertEqual(receipt["lock_domain"], "weltgewebe-production-deployment-v1")
        self.assertEqual(receipt["lock_owner_entrypoint"], "deploy-helper")
        self.assertEqual(receipt["lock_handoff"], "direct")
        self.assertIsNone(receipt["deploy_invocation_id"])
        self.assertIsInstance(receipt["migration_completed_at"], str)
        self.assertEqual(receipt["observed_main_after_deploy"], self.commit)
        current = (self.state / "current.json").resolve()
        self.assertEqual(current, receipt_path)

    def test_direct_recovery_receipt_is_mode_0600_under_permissive_umask(self) -> None:
        result = self.deploy(advance=False, umask="000")
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.state / "receipts" / f"{self.commit}.json"
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    def schema4_verified_receipt(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 4,
            "environment": "production",
            "commit": self.commit,
            "web_artifact_sha256": "a" * 64,
            "started_at": "2026-07-27T00:00:00+00:00",
            "completed_at": "2026-07-27T00:01:00+00:00",
            "api_commit": self.commit,
            "frontend_commit": self.commit,
            "observed_main_after_deploy": self.commit,
            "migration_completed_at": "2026-07-27T00:00:30+00:00",
            "lock_domain": "weltgewebe-production-deployment-v1",
            "lock_owner_entrypoint": "deploy-helper",
            "lock_handoff": "direct",
            "result": "verified",
        }
        payload.update(overrides)
        return payload

    def assert_observed_recovery_timestamps(self, receipt: dict[str, object]) -> None:
        verification = json.loads(
            (
                self.state / "reconcile-receipts" / f"observed-{self.commit}.json"
            ).read_text()
        )
        self.assertIsNone(receipt["started_at"])
        self.assertEqual(receipt["completed_at"], verification["verified_at"])
        self.assertIsNone(receipt["migration_completed_at"])

    def test_public_noop_migrates_trusted_direct_schema4_verified_receipt(
        self,
    ) -> None:
        original = self.schema4_verified_receipt()
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json", original
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["schema_version"], 5)
        self.assertEqual(receipt["result"], "verified")
        self.assertIsNone(receipt["deploy_invocation_id"])
        for key, value in original.items():
            if key != "schema_version":
                self.assertEqual(receipt[key], value)

    def test_public_noop_does_not_promote_inherited_schema4_receipt(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(
                lock_owner_entrypoint="reconciler", lock_handoff="inherited"
            ),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["schema_version"], 6)
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertIsNone(receipt["deploy_invocation_id"])
        self.assertIsNone(receipt["web_artifact_sha256"])

    def test_public_noop_does_not_promote_inconsistent_schema4_receipt(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(api_commit="f" * 40),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertIsNone(receipt["web_artifact_sha256"])

    def test_public_noop_does_not_promote_incomplete_schema4_receipt(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(completed_at=""),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assert_observed_recovery_timestamps(receipt)

    def test_public_noop_does_not_promote_schema4_with_invalid_timestamps(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(started_at="not-a-time"),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assert_observed_recovery_timestamps(receipt)

    def test_public_noop_does_not_promote_schema4_with_overflowing_timestamp(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(started_at="0001-01-01T00:00:00+23:59"),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assert_observed_recovery_timestamps(receipt)

    def test_public_noop_does_not_promote_schema4_with_inconsistent_timestamps(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(
                migration_completed_at="2026-07-27T00:02:00+00:00"
            ),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assert_observed_recovery_timestamps(receipt)

    def test_public_noop_does_not_promote_schema4_with_unknown_metadata(
        self,
    ) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            self.schema4_verified_receipt(untrusted_extension="value"),
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertNotIn("untrusted_extension", receipt)

    def test_public_noop_does_not_promote_schema4_with_duplicate_keys(
        self,
    ) -> None:
        serialized = json.dumps(self.schema4_verified_receipt())
        conflicting_commit = "f" * 40
        serialized = serialized.replace(
            f'"commit": "{self.commit}"',
            f'"commit": "{conflicting_commit}", "commit": "{self.commit}"',
            1,
        )
        self.install_root_text(
            self.state / "receipts" / f"{self.commit}.json", serialized + "\n"
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assert_observed_recovery_timestamps(receipt)

    def schema5_verified_receipt(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 5,
            "environment": "production",
            "commit": self.commit,
            "web_artifact_sha256": "a" * 64,
            "started_at": "2026-07-27T00:00:00+00:00",
            "completed_at": "2026-07-27T00:01:00+00:00",
            "api_commit": self.commit,
            "frontend_commit": self.commit,
            "observed_main_after_deploy": self.commit,
            "migration_completed_at": "2026-07-27T00:00:30+00:00",
            "lock_domain": "weltgewebe-production-deployment-v1",
            "lock_owner_entrypoint": "deploy-helper",
            "lock_handoff": "direct",
            "result": "verified",
            "deploy_invocation_id": None,
        }
        payload.update(overrides)
        return payload

    def assert_schema5_receipt_rewritten_as_observed(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        self.install_root_json(self.state / "receipts" / f"{self.commit}.json", payload)
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["schema_version"], 6)
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertEqual(receipt["lock_domain"], "weltgewebe-production-deployment-v1")
        self.assertFalse(receipt["attestation_verified"])
        return receipt

    def test_public_noop_preserves_valid_direct_schema5_verified_receipt(self) -> None:
        original = self.schema5_verified_receipt()
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json", original
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt, original)

    def test_public_noop_preserves_valid_inherited_schema5_verified_receipt(
        self,
    ) -> None:
        original = self.schema5_verified_receipt(
            lock_owner_entrypoint="reconciler",
            lock_handoff="inherited",
            deploy_invocation_id="b" * 64,
        )
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json", original
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt, original)

    def test_public_noop_rewrites_schema5_verified_with_unknown_field(self) -> None:
        receipt = self.assert_schema5_receipt_rewritten_as_observed(
            self.schema5_verified_receipt(untrusted_extension="value")
        )
        self.assertNotIn("untrusted_extension", receipt)

    def test_public_noop_rewrites_schema5_verified_with_naive_timestamp(self) -> None:
        self.assert_schema5_receipt_rewritten_as_observed(
            self.schema5_verified_receipt(started_at="2026-07-27T00:00:00")
        )

    def test_public_noop_rewrites_schema5_verified_with_invalid_timestamp(self) -> None:
        self.assert_schema5_receipt_rewritten_as_observed(
            self.schema5_verified_receipt(completed_at="not-a-time")
        )

    def test_public_noop_rewrites_schema5_verified_with_overflowing_timestamp(
        self,
    ) -> None:
        self.assert_schema5_receipt_rewritten_as_observed(
            self.schema5_verified_receipt(started_at="0001-01-01T00:00:00+23:59")
        )

    def test_public_noop_rewrites_schema5_verified_with_wrong_time_order(self) -> None:
        self.assert_schema5_receipt_rewritten_as_observed(
            self.schema5_verified_receipt(
                migration_completed_at="2026-07-27T00:02:00+00:00"
            )
        )

    def test_public_noop_rewrites_legacy_schema5_observed_receipt_as_limited(
        self,
    ) -> None:
        original = self.schema5_verified_receipt(
            web_artifact_sha256=None,
            started_at=None,
            migration_completed_at=None,
            lock_owner_entrypoint="reconciler",
            lock_handoff="public-observation",
            result="verified_observed",
            evidence_boundary="Exact public readback only.",
        )
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json", original
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["schema_version"], 6)
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertFalse(receipt["attestation_verified"])

    def test_public_noop_rewrites_schema5_observed_with_unknown_field(self) -> None:
        payload = self.schema5_verified_receipt(
            web_artifact_sha256=None,
            started_at=None,
            migration_completed_at=None,
            lock_owner_entrypoint="reconciler",
            lock_handoff="public-observation",
            result="verified_observed",
            evidence_boundary="Exact public readback only.",
            untrusted_extension="value",
        )
        receipt = self.assert_schema5_receipt_rewritten_as_observed(payload)
        self.assertNotIn("untrusted_extension", receipt)

    def test_public_noop_rewrites_invalid_schema5_verified_receipt(self) -> None:
        self.install_root_json(
            self.state / "receipts" / f"{self.commit}.json",
            {
                "schema_version": 5,
                "environment": "production",
                "commit": self.commit,
                "web_artifact_sha256": "a" * 64,
                "started_at": "2026-07-27T00:00:00+00:00",
                "completed_at": "2026-07-27T00:01:00+00:00",
                "api_commit": self.commit,
                "frontend_commit": self.commit,
                "observed_main_after_deploy": self.commit,
                "migration_completed_at": "2026-07-27T00:00:30+00:00",
                "lock_domain": "unexpected-domain",
                "lock_owner_entrypoint": "deploy-helper",
                "lock_handoff": "direct",
                "result": "verified",
                "deploy_invocation_id": None,
            },
        )
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["schema_version"], 6)
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertEqual(receipt["lock_domain"], "weltgewebe-production-deployment-v1")

    def test_main_advancing_after_migration_is_superseded_before_full_deploy(
        self,
    ) -> None:
        result = self.deploy(advance=False, advance_after_migration=True)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 79, result.stderr)
        deploy_calls = (
            (self.root / "weltgewebe-up.log").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(len(deploy_calls), 1)
        self.assertIn("--deploy-scope migration", deploy_calls[0])
        receipt_path = self.state / "receipts" / f"{self.commit}.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["result"], "superseded_after_migration")
        self.assertIsInstance(receipt["migration_completed_at"], str)
        self.assertIsNone(receipt["api_commit"])
        self.assertIsNone(receipt["frontend_commit"])
        self.assertNotEqual(receipt["observed_main_after_deploy"], self.commit)
        self.assertFalse((self.state / "current.json").exists())

    def test_full_deploy_failure_after_migration_records_completed_phase(self) -> None:
        result = self.deploy(advance=False, fail_full=True)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 42, result.stderr)
        deploy_calls = (
            (self.root / "weltgewebe-up.log").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(len(deploy_calls), 2)
        self.assertIn("--deploy-scope migration", deploy_calls[0])
        self.assertIn("--with-caddy", deploy_calls[1])
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "failed")
        self.assertIsInstance(receipt["migration_completed_at"], str)
        self.assertIsNone(receipt["api_commit"])
        self.assertIsNone(receipt["frontend_commit"])
        self.assertFalse((self.state / "current.json").exists())

    def test_main_fetch_failure_after_migration_records_completed_phase(self) -> None:
        result = self.deploy(advance=False, break_remote_after_migration=True)
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.returncode, 75, result.stderr)
        deploy_calls = (
            (self.root / "weltgewebe-up.log").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(len(deploy_calls), 1)
        self.assertIn("--deploy-scope migration", deploy_calls[0])
        receipt = json.loads(
            (self.state / "receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(receipt["result"], "failed")
        self.assertIsInstance(receipt["migration_completed_at"], str)
        self.assertIsNone(receipt["api_commit"])
        self.assertIsNone(receipt["frontend_commit"])
        self.assertFalse((self.state / "current.json").exists())

    def test_main_advancing_during_deploy_is_not_marked_current(self) -> None:
        result = self.deploy(advance=True)
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 80, result.stderr)
        receipt_path = self.state / "receipts" / f"{self.commit}.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["result"], "superseded_after_deploy")
        self.assertNotEqual(receipt["observed_main_after_deploy"], self.commit)
        self.assertFalse((self.state / "current.json").exists())

    def test_public_noop_rejects_generic_pass_without_limited_status(self) -> None:
        result = self.reconcile_existing_public_commit(
            extra_env={"TEST_PUBLIC_VERIFICATION_STATUS": "verified"}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an exact limited pass", result.stderr)
        self.assertFalse((self.state / "current.json").exists())

    def test_public_noop_rejects_attestation_claim_without_verifier_support(
        self,
    ) -> None:
        result = self.reconcile_existing_public_commit(
            extra_env={"TEST_PUBLIC_ATTESTATION_VERIFIED": "1"}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an exact limited pass", result.stderr)
        self.assertFalse((self.state / "current.json").exists())

    def test_public_noop_rejects_unknown_verification_receipt_fields(self) -> None:
        result = self.reconcile_existing_public_commit(
            extra_env={"TEST_PUBLIC_EXTRA_FIELD": "1"}
        )
        self.restore_test_ownership()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("field matrix is invalid", result.stderr)
        self.assertFalse((self.state / "current.json").exists())

    def test_public_noop_repairs_missing_deployment_receipt(self) -> None:
        result = self.reconcile_existing_public_commit()
        self.restore_test_ownership()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt_path = self.state / "receipts" / f"{self.commit}.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["schema_version"], 6)
        self.assertEqual(receipt["result"], "consistent_observed_unattested")
        self.assertEqual(receipt["lock_domain"], "weltgewebe-production-deployment-v1")
        self.assertEqual(receipt["lock_owner_entrypoint"], "reconciler")
        self.assertEqual(receipt["lock_handoff"], "public-observation")
        self.assertIsNone(receipt["deploy_invocation_id"])
        self.assertIsNone(receipt["migration_completed_at"])
        self.assertIsNone(receipt["web_artifact_sha256"])
        self.assertIn("build provenance is unattested", receipt["evidence_boundary"])
        self.assertEqual(receipt["public_verification_schema_version"], 3)
        self.assertEqual(
            receipt["public_verification_status"], "consistency_pass_unattested"
        )
        self.assertEqual(
            receipt["public_pass_scope"],
            "identity_and_declared_artifact_consistency",
        )
        self.assertTrue(receipt["identity_verified"])
        self.assertTrue(receipt["artifact_tree_declaration_verified"])
        self.assertFalse(receipt["attestation_verified"])
        self.assertEqual(receipt["artifact_tree_sha256"], "a" * 64)
        self.assertEqual(receipt["artifact_tree_compile_revision"], self.commit)
        self.assertEqual(receipt["artifact_tree_provenance"], "unattested")
        self.assertEqual((self.state / "current.json").resolve(), receipt_path)
        reconcile_receipt = json.loads(
            (self.state / "reconcile-receipts" / f"{self.commit}.json").read_text()
        )
        self.assertEqual(reconcile_receipt["result"], "consistent_observed_unattested")


if __name__ == "__main__":
    unittest.main()
