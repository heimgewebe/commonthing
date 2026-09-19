from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]


class ProductionReconcilerContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_privileged_inline_python_uses_isolated_import_path(self) -> None:
        scripts = (
            self.read("scripts/ops/deploy-exact-commit-vps.sh"),
            self.read("scripts/ops/reconcile-production-main-vps.sh"),
        )
        for script in scripts:
            self.assertNotIn('export PYTHONPATH="$SCRIPT_DIR"', script)
            self.assertIn(
                'WELTGEWEBE_OPS_SCRIPT_DIR="$SCRIPT_DIR" python3 -I - "$@"',
                script,
            )
            self.assertNotIn("python3 - ", script)
            self.assertNotIn("python3 -c ", script)
            self.assertEqual(
                script.count("from weltgewebe_secure_receipt_io import"),
                script.count(
                    'sys.path.insert(0, os.environ["WELTGEWEBE_OPS_SCRIPT_DIR"])'
                ),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attacker = root / "attacker"
            trusted = root / "trusted"
            attacker.mkdir()
            trusted.mkdir()
            marker = root / "attacker-ran"
            (attacker / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('site')\n",
                encoding="utf-8",
            )
            (attacker / "weltgewebe_secure_receipt_io.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('module')\nSOURCE = 'attacker'\n",
                encoding="utf-8",
            )
            (trusted / "weltgewebe_secure_receipt_io.py").write_text(
                "SOURCE = 'trusted'\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["WELTGEWEBE_OPS_SCRIPT_DIR"] = str(trusted)
            result = subprocess.run(
                [sys.executable, "-I", "-"],
                cwd=attacker,
                env=environment,
                input=(
                    "import os\n"
                    "import sys\n"
                    "sys.path.insert(0, os.environ['WELTGEWEBE_OPS_SCRIPT_DIR'])\n"
                    "from weltgewebe_secure_receipt_io import SOURCE\n"
                    "print(SOURCE)\n"
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "trusted")
            self.assertFalse(marker.exists())

    def test_deploy_helper_rechecks_main_after_public_readback(self) -> None:
        script = self.read("scripts/ops/deploy-exact-commit-vps.sh")
        self.assertGreaterEqual(script.count("fetch_main"), 3)
        deploy = script.index('"$release_dir/scripts/weltgewebe-up"')
        public_readback = script.index('api_body_file="$(mktemp', deploy)
        post_check = script.index('post_deploy_main="$(fetch_main)"', public_readback)
        verified_link = script.index('ln -sfn "receipts/$COMMIT.json"', post_check)
        self.assertLess(deploy, public_readback)
        self.assertLess(public_readback, post_check)
        self.assertLess(post_check, verified_link)
        self.assertIn("superseded_after_deploy", script)
        self.assertIn("readonly EX_TEMPFAIL=75", script)
        self.assertIn("readonly EXIT_SUPERSEDED_AFTER_MIGRATION=79", script)
        self.assertIn("readonly EXIT_SUPERSEDED_AFTER_DEPLOY=80", script)
        self.assertIn('exit "$EXIT_SUPERSEDED_AFTER_DEPLOY"', script)
        self.assertNotIn("curl -fsSI", script)
        self.assertIn("--max-filesize 1048576", script)
        self.assertIn("write_bounded_response", script)
        self.assertIn('api_body_file="$(mktemp', script)
        self.assertIn("awk -F':'", script)

    def test_reconciler_requires_germany_artifacts_before_frontend_build(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        guard = script.index("# Nationwide Germany is the production sovereign contract.")
        docker_build = script.index("docker run --rm", guard)
        self.assertLess(guard, docker_build)
        for marker in (
            "basemap-germany.pmtiles",
            "basemap-germany.meta.json",
            "map-style/style-germany.json",
            "map-style/style-germany-dark.json",
            "--env PUBLIC_BASEMAP_VARIANT=germany",
        ):
            self.assertIn(marker, script)
        self.assertIn("nationwide Germany basemap alias escapes canonical data root", script)
        self.assertIn("nationwide Germany basemap target is group- or world-writable", script)
        self.assertIn("build/_app/basemap-build.json", script)
        self.assertIn("frontend basemap build identity mismatch", script)
        self.assertIn("WELTGEWEBE_FRONTEND_BASEMAP_IDENTITY_URL", script)
        self.assertIn("WELTGEWEBE_FRONTEND_BASEMAP_LIGHT_STYLE_URL", script)
        self.assertIn("WELTGEWEBE_FRONTEND_BASEMAP_DARK_STYLE_URL", script)
        self.assertIn("WELTGEWEBE_FRONTEND_BASEMAP_PMTILES_URL", script)
        self.assertIn("verify_public_germany_basemap_delivery", script)
        self.assertEqual(script.count("verify_public_germany_basemap_delivery"), 3)
        self.assertIn(
            "<< 'PY_PUBLIC_BASEMAP_IDENTITY' || return 1", script
        )
        self.assertIn("reason=basemap_identity_drift", script)
        self.assertIn(
            "public nationwide Germany basemap delivery mismatch after deploy", script
        )
        self.assertIn("/local-basemap/style-germany.json", script)
        self.assertIn("/local-basemap/style-germany-dark.json", script)
        self.assertIn("/local-basemap/basemap-germany.pmtiles", script)
        self.assertIn("--range 0-126", script)
        self.assertIn("public Germany PMTiles range response is not HTTP 206", script)

    def test_reconciler_binds_noop_and_postdeploy_to_schaubild_runtime_lock(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        self.assertIn("WELTGEWEBE_SCHAUWERK_MANIFEST_URL", script)
        self.assertIn("infra/schauwerk-editor/release-lock.json", script)
        self.assertIn("weltgewebe-schauwerk-runtime-lock.v1", script)
        self.assertIn("ghcr.io/heimgewebe/schauwerk-schaubild", script)
        self.assertIn("verify_public_schauwerk_runtime", script)
        self.assertEqual(script.count("verify_public_schauwerk_runtime"), 3)
        self.assertIn("reason=schaubild_runtime_image_identity_drift", script)
        self.assertIn(
            "public Schaubild runtime does not match the reviewed OCI digest after deploy",
            script,
        )
        self.assertIn("schauwerk-standalone-editor-manifest.v2", script)
        self.assertIn("schauwerk-native-diagram-v1", script)
        no_op = script.index(
            'if [[ "$basemap_identity_matches" == "1" && "$schauwerk_identity_matches" == "1" ]]'
        )
        repair = script.index("repair_observed_deployment_state", no_op)
        self.assertLess(no_op, repair)

    def test_deploy_helper_runs_bounded_migrations_before_full_deploy(self) -> None:
        script = self.read("scripts/ops/deploy-exact-commit-vps.sh")
        migration = script.index("run_release_deploy migration")
        post_migration_main = script.index('remote_main="$(fetch_main)"', migration)
        full = script.index("run_release_deploy full", post_migration_main)
        public_readback = script.index('api_headers="$(mktemp', full)
        self.assertLess(migration, post_migration_main)
        self.assertLess(post_migration_main, full)
        self.assertLess(full, public_readback)
        self.assertIn('write_deploy_receipt "superseded_after_migration"', script)
        self.assertIn('"$completed_at" "" "" "$remote_main"', script)
        self.assertIn("production_deployment=superseded_after_migration", script)
        self.assertIn("migration_completed_at", script)
        self.assertIn("production_deployment_phase=migration state=starting", script)
        self.assertIn("production_deployment_phase=migration state=completed", script)
        self.assertIn("production_deployment_phase=full state=starting", script)
        self.assertIn('exit "$EXIT_SUPERSEDED_AFTER_MIGRATION"', script)
        self.assertIn("--deploy-scope migration", script)
        self.assertIn('"+refs/heads/main:refs/remotes/origin/main" || return 1', script)
        self.assertIn("rev-parse refs/remotes/origin/main || return 1", script)

    def test_deploy_helper_preserves_legacy_checkout_and_binds_main(self) -> None:
        script = self.read("scripts/ops/deploy-exact-commit-vps.sh")
        self.assertIn("+refs/heads/main:refs/remotes/origin/main", script)
        self.assertIn('worktree add --detach "$release_dir" "$COMMIT"', script)
        for forbidden in ("git reset", "git clean", "git switch", "git pull"):
            self.assertNotIn(forbidden, script)
        self.assertIn(
            'PRODUCTION_LOCK_FILE="${STATE_ROOT}/production-deployment.lock"',
            script,
        )
        self.assertIn("WELTGEWEBE_PRODUCTION_LOCK_FD", script)
        self.assertIn("WELTGEWEBE_DEPLOY_INVOCATION_ID", script)
        self.assertIn("inherited deploy invocation identity is invalid", script)
        self.assertIn('lock_handoff="inherited"', script)
        self.assertIn("production_deployment=already_running", script)
        receipt_io = self.read("scripts/ops/weltgewebe_secure_receipt_io.py")
        self.assertIn("from weltgewebe_secure_receipt_io import", script)
        self.assertIn("write_secure_json", script)
        for receipt_guard in (
            "os.O_EXCL",
            "O_NOFOLLOW",
            "os.fchmod(file_fd, mode)",
            "metadata.st_nlink != 1",
            "os.replace(",
        ):
            self.assertIn(receipt_guard, receipt_io)
        self.assertIn(
            '"$release_dir/scripts/weltgewebe-up" "${arguments[@]}" 9>&-', script
        )
        self.assertNotIn("${STATE_ROOT}/deploy.lock", script)
        self.assertIn("ARCHIVE_VALIDATOR", script)
        self.assertIn("validate_release_tree", script)
        self.assertIn("release contains unexpected state", script)
        self.assertIn("release directory is not root-owned", script)
        self.assertIn(
            'find "$release_dir/apps/web/build" -type f -exec chmod 0644', script
        )
        self.assertIn('ln -s "$basemap_real" "$release_dir/build/basemap"', script)
        self.assertIn("basemap link escapes the canonical data root", script)
        self.assertIn("-type l -print0", script)
        self.assertIn(
            'install -d -o root -g root -m 0700 "$STATE_ROOT/receipts"', script
        )

    def test_deploy_helper_requires_immutable_root_artifact(self) -> None:
        script = self.read("scripts/ops/deploy-exact-commit-vps.sh")
        self.assertIn("web artifact escaped the root-owned artifact directory", script)
        self.assertIn("web artifact is not root-owned", script)
        self.assertIn("web artifact is group- or world-writable", script)
        self.assertIn("web artifact has unexpected hard links", script)
        self.assertIn("web artifact changed during validation", script)
        self.assertGreaterEqual(script.count('sha256sum "$artifact_real"'), 2)
        self.assertIn('write_deploy_receipt \\\n      "failed"', script)

    def run_release_cleanup(self, release: Path) -> subprocess.CompletedProcess[str]:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        start = script.index("path_contains_mount() {")
        end = script.index("\nprune_releases() {", start)
        cleanup_functions = script[start:end]
        command = f"""set -Eeuo pipefail
SCRIPT_DIR={str(ROOT / 'scripts/ops')!r}
run_ops_python() {{
  WELTGEWEBE_OPS_SCRIPT_DIR="$SCRIPT_DIR" python3 -I - "$@"
}}
{cleanup_functions}
cleanup_release_runtime_paths "$1"
"""
        return subprocess.run(
            ["bash", "-c", command, "release-cleanup-test", str(release)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_deploy_web_cleanup(self, release: Path) -> subprocess.CompletedProcess[str]:
        script = self.read("scripts/ops/deploy-exact-commit-vps.sh")
        start = script.index("path_contains_mount() {")
        end = script.index("\nwrite_deploy_receipt() {", start)
        cleanup_functions = script[start:end]
        command = f"""set -Eeuo pipefail
SCRIPT_DIR={str(ROOT / 'scripts/ops')!r}
run_ops_python() {{
  WELTGEWEBE_OPS_SCRIPT_DIR="$SCRIPT_DIR" python3 -I - "$@"
}}
fail() {{
  echo "ERROR: $*" >&2
  exit 1
}}
release_dir="$1"
release_real="$(realpath -e -- "$release_dir")"
{cleanup_functions}
remove_release_web_build
"""
        return subprocess.run(
            ["bash", "-c", command, "deploy-web-cleanup-test", str(release)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_reconciler_preserves_explicit_terminal_success_states(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        start = script.index("is_terminal_success_state() {")
        end = script.index("\ntrap cleanup EXIT", start)
        cleanup_functions = script[start:end]
        terminal_states = (
            "consistent_observed_unattested",
            "deferred",
            "verified",
            "verified_observed",
            "superseded_after_observe",
            "superseded_after_migration",
            "superseded_after_deploy",
            "superseded_after_verify",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = f"""set +e
temporary_artifact=""
temporary_source=""
source_archive=""
target_commit=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
state_result="$1"
OUTPUT="$2"
write_state() {{
  printf '%s\n' "$1" > "$OUTPUT"
}}
{cleanup_functions}
false
cleanup
"""
            for state in terminal_states:
                output = root / f"{state}.txt"
                result = subprocess.run(
                    ["bash", "-c", command, "state-test", state, str(output)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, state)
                self.assertFalse(output.exists(), state)

            output = root / "building.txt"
            result = subprocess.run(
                ["bash", "-c", command, "state-test", "building", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "failed\n")

        self.assertIn('! is_terminal_success_state "$state_result"', script)
        self.assertNotIn('^\(verified|superseded\)', script)

    def test_deploy_helper_web_cleanup_is_boundary_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ("6" * 40)
            web = release / "apps/web"
            external = root / "external-web"
            web.mkdir(parents=True)
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_text("persistent", encoding="utf-8")
            (web / "build").symlink_to(external, target_is_directory=True)

            result = self.run_deploy_web_cleanup(release)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((web / "build").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "persistent")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ("7" * 40)
            apps = release / "apps"
            external = root / "external-web"
            external_build = external / "build"
            apps.mkdir(parents=True)
            external_build.mkdir(parents=True)
            sentinel = external_build / "sentinel"
            sentinel.write_text("persistent", encoding="utf-8")
            (apps / "web").symlink_to(external, target_is_directory=True)

            result = self.run_deploy_web_cleanup(release)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cleanup parent is unsafe", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "persistent")

        script = self.read("scripts/ops/deploy-exact-commit-vps.sh")
        self.assertIn('path_contains_mount "$web_build_real" "$release_real"', script)
        self.assertIn('rm -rf --one-file-system -- "$web_build_real"', script)
        self.assertNotIn('rm -rf -- "$release_dir/apps/web/build"', script)

    def test_reconciler_prunes_legacy_basemap_directory_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ("a" * 40)
            legacy_basemap = release / "build/basemap"
            web_build = release / "apps/web/build"
            persistent_basemap = root / "persistent-basemap"
            legacy_basemap.mkdir(parents=True)
            web_build.mkdir(parents=True)
            persistent_basemap.mkdir()
            (legacy_basemap / "legacy.pmtiles").write_text("legacy", encoding="utf-8")
            (web_build / "index.html").write_text("generated", encoding="utf-8")
            sentinel = persistent_basemap / "canonical.pmtiles"
            sentinel.write_text("persistent", encoding="utf-8")
            (legacy_basemap / "external-link").symlink_to(sentinel)

            result = self.run_release_cleanup(release)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(legacy_basemap.exists())
            self.assertFalse(web_build.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "persistent")

    def test_reconciler_unlinks_basemap_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ("b" * 40)
            build = release / "build"
            persistent_basemap = root / "persistent-basemap"
            build.mkdir(parents=True)
            persistent_basemap.mkdir()
            sentinel = persistent_basemap / "canonical.pmtiles"
            sentinel.write_text("persistent", encoding="utf-8")
            (build / "basemap").symlink_to(persistent_basemap, target_is_directory=True)

            result = self.run_release_cleanup(release)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((build / "basemap").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "persistent")

    def test_reconciler_unlinks_web_build_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ("f" * 40)
            web = release / "apps/web"
            external = root / "external-web"
            web.mkdir(parents=True)
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_text("persistent", encoding="utf-8")
            (web / "build").symlink_to(external, target_is_directory=True)

            result = self.run_release_cleanup(release)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((web / "build").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "persistent")

    def test_reconciler_refuses_intermediate_web_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ("1" * 40)
            apps = release / "apps"
            external = root / "external-web"
            external_build = external / "build"
            apps.mkdir(parents=True)
            external_build.mkdir(parents=True)
            sentinel = external_build / "sentinel"
            sentinel.write_text("persistent", encoding="utf-8")
            (apps / "web").symlink_to(external, target_is_directory=True)

            result = self.run_release_cleanup(release)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release cleanup parent is unsafe", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "persistent")

    def test_reconciler_refuses_unprotected_legacy_basemap_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / ("c" * 40)
            legacy_basemap = release / "build/basemap"
            legacy_basemap.mkdir(parents=True)
            unsafe = legacy_basemap / "mutable.pmtiles"
            unsafe.write_text("mutable", encoding="utf-8")
            unsafe.chmod(0o666)

            result = self.run_release_cleanup(release)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not exclusively owned and protected", result.stderr)
            self.assertTrue(unsafe.exists())

    def test_reconciler_retains_release_when_guarded_cleanup_refuses(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        start = script.index("path_contains_mount() {")
        end = script.index('\n[[ "$EUID" -eq 0 ]]', start)
        cleanup_functions = script[start:end]
        command = f"""set -Eeuo pipefail
SCRIPT_DIR={str(ROOT / 'scripts/ops')!r}
STATE_ROOT="$1/state"
RELEASE_ROOT="$1/releases"
SOURCE_CHECKOUT="$1/source"
mkdir -p "$STATE_ROOT" "$RELEASE_ROOT" "$SOURCE_CHECKOUT"
protected_release="$RELEASE_ROOT/0000000000000000000000000000000000000000"
mkdir -p "$protected_release"
ln -s "$protected_release" "$STATE_ROOT/current-release"
run_ops_python() {{
  WELTGEWEBE_OPS_SCRIPT_DIR="$SCRIPT_DIR" python3 -I - "$@"
}}
stat() {{
  printf '0\n'
}}
git() {{
  if [[ "$#" -ge 4 && "$1" == "-C" && "$3" == "rev-parse" && "$4" == "HEAD" ]]; then
    printf '%s\n' "${{2##*/}}"
  fi
  return 0
}}
rm() {{
  if [[ "${{FAIL_WEB_RM:-0}}" == "1" && "$*" == *"/apps/web/build"* ]]; then
    return 1
  fi
  command rm "$@"
}}
{cleanup_functions}
prune_releases
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "releases" / ("d" * 40)
            unsafe = release / "build/basemap/mutable.pmtiles"
            unsafe.parent.mkdir(parents=True)
            unsafe.write_text("mutable", encoding="utf-8")
            unsafe.chmod(0o666)
            old = 1_600_000_000
            os.utime(release, (old, old))

            result = subprocess.run(
                ["bash", "-c", command, "release-prune-test", str(root)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(unsafe.exists())
            self.assertIn(
                "retaining release after guarded cleanup refusal", result.stderr
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "releases" / ("e" * 40)
            generated = release / "apps/web/build/index.html"
            generated.parent.mkdir(parents=True)
            generated.write_text("generated", encoding="utf-8")
            old = 1_600_000_000
            os.utime(release, (old, old))
            environment = dict(os.environ)
            environment["FAIL_WEB_RM"] = "1"

            result = subprocess.run(
                ["bash", "-c", command, "release-prune-test", str(root)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(generated.exists())
            self.assertIn("could not remove release web build", result.stderr)
            self.assertIn(
                "retaining release after guarded cleanup refusal", result.stderr
            )

    def test_mount_intersection_detects_ancestor_and_descendant_mounts(self) -> None:
        for script_path in (
            "scripts/ops/reconcile-production-main-vps.sh",
            "scripts/ops/deploy-exact-commit-vps.sh",
        ):
            script = self.read(script_path)
            function_start = script.index("path_contains_mount() {")
            source_start = script.index("import os\n", function_start)
            source_end = script.index("\nPY\n}", source_start)
            source = script[source_start:source_end]

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                boundary = root / "release"
                target = boundary / "apps/web/build"
                target.mkdir(parents=True)

                def probe(*mount_paths: Path) -> int:
                    mountinfo = root / "mountinfo"
                    mountinfo.write_text(
                        "".join(
                            f"36 25 0:32 / {path} rw - ext4 /dev/root rw\n"
                            for path in mount_paths
                        ),
                        encoding="utf-8",
                    )
                    return subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-",
                            str(target),
                            str(boundary),
                            str(mountinfo),
                        ],
                        input=source,
                        text=True,
                        capture_output=True,
                        check=False,
                    ).returncode

                self.assertEqual(probe(boundary / "apps/web"), 0, script_path)
                self.assertEqual(probe(target / "cache"), 0, script_path)
                self.assertEqual(probe(boundary), 0, script_path)
                self.assertEqual(
                    probe(Path("/"), root / "outside"), 1, script_path
                )

    def test_reconciler_release_cleanup_is_mount_and_boundary_guarded(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        self.assertIn('/proc/self/mountinfo', script)
        self.assertIn('mount_within_boundary', script)
        self.assertIn('target_within_mount', script)
        self.assertIn('legacy release basemap contains a mount', script)
        self.assertIn('legacy release basemap escaped release root', script)
        self.assertIn('rm -rf --one-file-system -- "$basemap_real"', script)
        self.assertNotIn('rm -f -- "$release_dir/build/basemap"', script)

    def test_reconciler_build_cache_cleanup_is_backend_capability_bounded(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        active_block = script.index("reason=active_build_cache")
        min_free_prune = script.index(
            'docker builder prune --force --min-free-space "$BUILD_MIN_FREE_BYTES"'
        )
        incompatibility = script.index(
            "buildkit v0.17.0+ is required for max-used-space and "
            "min-free-space filters"
        )
        fallback_prune = script.index(
            "docker builder prune --force --filter until=168h"
        )
        post_cleanup = script.index("production_disk_preflight=post_cleanup")

        self.assertLess(active_block, min_free_prune)
        self.assertLess(min_free_prune, incompatibility)
        self.assertLess(incompatibility, fallback_prune)
        self.assertLess(min_free_prune, post_cleanup)
        self.assertLess(fallback_prune, post_cleanup)
        allowed_prune_shapes = (
            re.compile(
                r'^if min_free_space_output="\$\(docker builder prune --force '
                r'--min-free-space "\$BUILD_MIN_FREE_BYTES" 2>&1\)"; then$'
            ),
            re.compile(
                r"^if docker builder prune --force --filter until=168h "
                r"> /dev/null; then$"
            ),
        )
        prune_lines = [
            line.strip()
            for line in script.splitlines()
            if "docker builder prune" in line
        ]
        matched_shapes = []
        for line in prune_lines:
            matches = [
                index
                for index, pattern in enumerate(allowed_prune_shapes)
                if pattern.fullmatch(line)
            ]
            self.assertEqual(
                len(matches), 1, f"unexpected Docker builder prune shape: {line}"
            )
            matched_shapes.append(matches[0])
        self.assertCountEqual(matched_shapes, range(len(allowed_prune_shapes)))
        self.assertIn("docker system df --format '{{json .}}'", script)
        for forbidden_probe in (
            r"\bdocker\s+buildx(?:\s|$)",
            r"\bdocker\s+(?:--version|version|info)(?:\s|$)",
            r"\bdocker\s+builder(?:\s+prune)?\s+--help(?:\s|$)",
        ):
            self.assertNotRegex(script, forbidden_probe)
        for forbidden in (
            "docker builder prune --all",
            "docker system prune",
            "docker image prune",
            "docker container prune",
            "docker volume prune",
            "docker image rm",
            "docker rmi",
        ):
            self.assertNotIn(forbidden, script)

    def test_reconciler_has_bounded_storage_and_state_transitions(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        for state in (
            "observed",
            "building",
            "artifact_validated",
            "deferred",
            "superseded_after_observe",
            "superseded_after_deploy",
            "superseded_after_verify",
            "verified_observed",
            "consistent_observed_unattested",
            "verified",
            "failed",
        ):
            self.assertIn(f'"{state}"', script)
        self.assertIn("MIN_FREE_KIB", script)
        self.assertIn("prune_artifacts", script)
        self.assertIn("prune_releases", script)
        self.assertIn("current release marker is unavailable", script)
        self.assertIn("retaining release after Git status failure", script)
        self.assertIn('realpath -e -- "$RELEASE_ROOT"', script)
        self.assertIn(
            '"+refs/heads/main:refs/remotes/origin/main" || return 1', script
        )
        self.assertIn(
            "rev-parse refs/remotes/origin/main || return 1", script
        )
        self.assertIn("observed release is group- or world-writable", script)
        self.assertIn('rm -f -- "$source_archive"', script)
        self.assertIn('worktree remove "$release_dir"', script)
        self.assertNotIn("worktree remove --force", script)
        self.assertIn("web_artifacts[@]:20", script)
        self.assertIn("repair_observed_deployment_state", script)
        self.assertIn('"migration_completed_at": None', script)
        self.assertIn("build provenance is unattested", script)
        self.assertIn('"schema_version": 6', script)
        self.assertIn('"attestation_verified": verification.get', script)
        self.assertIn('ln -sfn "receipts/$target_commit.json"', script)
        self.assertIn('chmod 0600 "$artifact"', script)
        self.assertIn('DOCKER_CONFIG="$STATE_ROOT/docker-config"', script)
        self.assertIn("export DOCKER_CONFIG", script)
        self.assertIn('"$DEPLOY_RECEIPT_ROOT" "$DOCKER_CONFIG"', script)
        self.assertIn(
            'PRODUCTION_LOCK_FILE="$STATE_ROOT/production-deployment.lock"',
            script,
        )
        self.assertIn("production_reconcile=already_running", script)
        self.assertIn("readonly EX_TEMPFAIL=75", script)
        self.assertIn("readonly EXIT_SUPERSEDED_AFTER_MIGRATION=79", script)
        self.assertIn("readonly EXIT_SUPERSEDED_AFTER_DEPLOY=80", script)
        self.assertIn('case "$deploy_rc" in', script)
        self.assertIn("new_deploy_invocation_id", script)
        self.assertIn('WELTGEWEBE_DEPLOY_INVOCATION_ID="$deploy_invocation_id"', script)
        self.assertIn("does not match current invocation", script)
        self.assertIn("read_deploy_tempfail_diagnostic", script)
        receipt_io = self.read("scripts/ops/weltgewebe_secure_receipt_io.py")
        self.assertIn("from weltgewebe_secure_receipt_io import", script)
        self.assertIn("read_secure_json", script)
        self.assertIn("write_secure_json", script)
        for receipt_guard in (
            "O_NOFOLLOW",
            "os.fstat(file_fd)",
            "metadata.st_nlink != 1",
            "os.read(file_fd",
            "DEFAULT_MAX_BYTES = 1024 * 1024",
        ):
            self.assertIn(receipt_guard, receipt_io)
        self.assertIn("production lock contention during inherited handoff", script)
        self.assertIn("child temporary failure", script)
        self.assertIn("unexpected superseded reason", script)
        self.assertIn(
            'WELTGEWEBE_PRODUCTION_LOCK_OWNER_ENTRYPOINT="reconciler"',
            script,
        )
        self.assertNotIn("$STATE_ROOT/reconcile.lock", script)

    def test_reconciler_build_cannot_write_release_or_source(self) -> None:
        script = self.read("scripts/ops/reconcile-production-main-vps.sh")
        self.assertIn(
            "docker.io/library/node@sha256:8898f8ed3c0126667837b678979b4ed83306c856a1227c8bf5f5f77740c25cd6",
            script,
        )
        for expected in (
            "docker run --rm",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--read-only",
            "--pids-limit 512",
            "--memory 2g",
            "--cpus 2",
            "--tmpfs /workspace:rw,exec,nosuid,nodev,size=3g,mode=1777",
            "dst=/source.tar,readonly",
            'GIT_COMMIT_SHA="$target_commit"',
        ):
            self.assertIn(expected, script)
        for forbidden in (
            "/var/run/docker.sock",
            "SSH_AUTH_SOCK",
            "--privileged",
            "dst=/opt/weltgewebe",
            "dst=/opt/weltgewebe-releases",
        ):
            self.assertNotIn(forbidden, script)

    def test_production_reconciler_is_in_critical_impl_registry(self) -> None:
        registry = self.read("audit/impl-registry.yaml")
        start = registry.index("  - id: impl.workflow.production-main-reconcile\n")
        next_entry = registry.find("\n  - id:", start + 1)
        entry = registry[start : next_entry if next_entry != -1 else len(registry)]
        for expected in (
            "path: scripts/ops/reconcile-production-main-vps.sh",
            "criticality: high",
            "evidence_level: ci",
            "docs/deploy/merge-to-live.md",
            "docs/deploy/schauwerk-editor-frontdoor.md",
            "scripts/ci/tests/test_production_reconciler_contract.py",
            "scripts/ci/tests/test_deploy_exact_commit_integration.py",
            ".github/workflows/production-live-contract.yml",
        ):
            self.assertIn(expected, entry)

    def test_secure_receipt_helper_is_in_critical_impl_registry(self) -> None:
        registry = self.read("audit/impl-registry.yaml")
        self.assertIn("id: impl.guard.secure-receipt-io", registry)
        self.assertIn("path: scripts/ops/weltgewebe_secure_receipt_io.py", registry)
        self.assertIn("scripts/ci/tests/test_secure_receipt_io.py", registry)
        self.assertIn(".github/workflows/production-live-contract.yml", registry)

    def test_public_verifier_writes_receipts_through_safe_descriptors(self) -> None:
        script = self.read("scripts/ops/verify_public_release_commit.py")
        receipt_io = self.read("scripts/ops/weltgewebe_secure_receipt_io.py")
        self.assertIn(
            "from weltgewebe_secure_receipt_io import write_secure_json", script
        )
        self.assertIn("write_secure_json(", script)
        for guard in (
            "os.O_EXCL",
            "O_NOFOLLOW",
            "os.fchmod(file_fd, mode)",
            "os.fstat(file_fd)",
            "metadata.st_nlink != 1",
            "os.replace(",
        ):
            self.assertIn(guard, receipt_io)
        self.assertNotIn('temporary.open("w"', script)

    def test_systemd_timer_uses_completion_relative_cadence(self) -> None:
        service = self.read(
            "infra/systemd/system/weltgewebe-production-reconcile.service"
        )
        timer = self.read("infra/systemd/system/weltgewebe-production-reconcile.timer")
        self.assertIn(
            "EnvironmentFile=-/etc/weltgewebe/production-reconciler.env", service
        )
        self.assertIn("TimeoutStartSec=7200", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("OnUnitInactiveSec=2min", timer)
        self.assertNotIn("OnUnitActiveSec", timer)
        self.assertNotIn("Persistent=true", timer)
        self.assertIn(
            "ExecStart=/usr/local/libexec/weltgewebe-reconcile-production-main",
            service,
        )
        self.assertIn("Unit=weltgewebe-production-reconcile.service", timer)

    def test_installer_is_exact_commit_bound_and_parameterizes_build_user(self) -> None:
        script = self.read("scripts/ops/install-production-reconciler.sh")
        self.assertIn("--commit", script)
        self.assertIn('remote_main" == "$COMMIT', script)
        self.assertIn('git -C "$REPO_DIR" show "$COMMIT:$source_path"', script)
        self.assertIn("production-reconciler.env", script)
        self.assertIn("installed-contract.sha256", script)
        self.assertIn("/var/lib/weltgewebe-main-reconciler/docker-config", script)
        self.assertNotIn("Environment=WELTGEWEBE_BUILD_USER=alex", script)
        self.assertIn("systemctl start weltgewebe-production-reconcile.service", script)

    def test_installer_deferred_update_is_atomic_and_non_recursive(self) -> None:
        script = self.read("scripts/ops/install-production-reconciler.sh")
        self.assertIn("--defer-reconcile", script)
        self.assertIn(
            "deferred installation requires an existing enabled and active production reconcile timer",
            script,
        )
        self.assertIn("atomic_install()", script)
        self.assertIn("mv -fT --", script)
        self.assertIn("require_matching_sha256", script)
        for installed_path in (
            "/usr/local/libexec/weltgewebe-deploy-exact-commit",
            "/usr/local/libexec/weltgewebe-reconcile-production-main",
            "/usr/local/libexec/weltgewebe-validate-web-deploy-archive",
            "/usr/local/libexec/weltgewebe-verify-public-release",
            "/usr/local/libexec/weltgewebe_secure_receipt_io.py",
            "/etc/systemd/system/weltgewebe-production-reconcile.service",
            "/etc/systemd/system/weltgewebe-production-reconcile.timer",
        ):
            self.assertIn(installed_path, script)
        helper_install = script.index(
            'atomic_install "$staging/weltgewebe_secure_receipt_io.py"'
        )
        deploy_install = script.index(
            'atomic_install "$staging/weltgewebe-deploy-exact-commit"'
        )
        reconcile_install = script.index(
            'atomic_install "$staging/weltgewebe-reconcile-production-main"'
        )
        verifier_install = script.index(
            'atomic_install "$staging/weltgewebe-verify-public-release"'
        )
        self.assertLess(helper_install, deploy_install)
        self.assertLess(helper_install, reconcile_install)
        self.assertLess(helper_install, verifier_install)
        staged_verify = script.index("systemd-analyze verify")
        self.assertIn(
            '"$staging/weltgewebe-production-reconcile.service"',
            script[staged_verify:],
        )
        first_binary_install = script.index(
            'atomic_install "$staging/weltgewebe-deploy-exact-commit"'
        )
        first_unit_install = script.index(
            'atomic_install "$staging/weltgewebe-production-reconcile.service"'
        )
        self.assertLess(first_binary_install, staged_verify)
        self.assertLess(staged_verify, first_unit_install)
        deferred = script.index(
            "if ((DEFER_RECONCILE == 1)); then",
            script.index("systemctl daemon-reload"),
        )
        normal_start = script.index("else", deferred)
        deferred_branch = script[deferred:normal_start]
        self.assertIn("systemctl is-enabled --quiet", deferred_branch)
        self.assertIn("systemctl is-active --quiet", deferred_branch)
        self.assertNotIn("systemctl start", deferred_branch)
        self.assertIn("mode=$install_mode", script)

    def test_release_activation_is_exact_path_bound_and_precedes_docker(self) -> None:
        activator = self.read(
            "scripts/ops/activate-production-reconciler-from-release.sh"
        )
        up = self.read("scripts/weltgewebe-up")
        self.assertIn("production reconciler activation must run as root", activator)
        self.assertIn('"$release_dir_real" == "$release_root_real/$COMMIT"', activator)
        self.assertIn("require_root_safe_directory", activator)
        self.assertIn("require_root_safe_regular_file", activator)
        self.assertIn(
            'require_root_safe_directory "$release_root_real" "resolved release root"',
            activator,
        )
        self.assertIn(
            'require_root_safe_directory "$release_dir_real" "resolved release directory"',
            activator,
        )
        self.assertIn("rev-parse --verify 'HEAD^{commit}'", activator)
        self.assertIn("release directory is not a valid Git repository", activator)
        self.assertIn('release_head" == "$COMMIT', activator)
        self.assertIn("--defer-reconcile", activator)
        self.assertIn(
            'if [[ "$DEPLOY_TARGET" == "vps" && "$PLAN_ONLY" == "0" ]]; then',
            up,
        )
        self.assertIn('release_root="${release_root%/}"', up)
        self.assertIn('release_dir="${REPO_DIR%/}"', up)
        self.assertIn('release_commit="$(basename -- "$release_dir")"', up)
        self.assertIn('activation_owner_uid="$(id -u)"', up)
        self.assertIn('stat --format=%u -- "$reconciler_activator"', up)
        self.assertIn("((8#$activation_mode & 022))", up)
        activation = up.index(">> Production reconciler contract activation:")
        bake = up.index("# --- Bake Configuration ---")
        docker_config = up.index('docker compose "${BASE_ARGS[@]}" config', bake)
        self.assertLess(activation, bake)
        self.assertLess(bake, docker_config)

    def test_vps_caddy_binding_is_public_and_fail_closed(self) -> None:
        up = self.read("scripts/weltgewebe-up")
        self.assertIn("resolve_vps_public_bind.py", up)
        repo_resolution = up.index('REPO_DIR="$FOUND_REPO"')
        resolver = up.index('PUBLIC_BIND_RESOLVER="$REPO_DIR/scripts/ops/resolve_vps_public_bind.py"')
        self.assertLess(repo_resolution, resolver)
        self.assertIn('$PUBLIC_BIND_RESOLVER --family ipv4', up)
        self.assertIn('$PUBLIC_BIND_RESOLVER --family ipv6', up)
        self.assertIn('export CADDY_BIND="$VPS_PUBLIC_CADDY_IPV4"', up)
        self.assertIn('export CADDY_IPV6_BIND="${VPS_PUBLIC_CADDY_IPV6:-127.0.0.1}"', up)
        conflict = up.index('if echo "$ERR_CONTENT" | grep -Eq')
        vps_guard = up.index('if [[ "$DEPLOY_TARGET" == "vps" ]]; then', conflict)
        fallback = up.index('CMD_RETRY=("${CMD_BASE[@]}" "--scale" "caddy=0")', conflict)
        self.assertLess(vps_guard, fallback)
        self.assertIn(
            "refusing to continue without public Caddy",
            up[vps_guard:fallback],
        )

    def test_full_vps_deploy_refreshes_caddy_bind_mounts_after_web_replacement(self) -> None:
        up = self.read("scripts/weltgewebe-up")
        full = up.index('if [[ "$DEPLOY_SCOPE" == "full" ]]; then')
        compose = up.index('CMD_BASE=("docker" "compose"', full)
        refresh_guard = up.index('if [[ "$DEPLOY_TARGET" == "vps" ]]; then', compose)
        refresh = up.index('CADDY_REFRESH_CMD=(', refresh_guard)
        self.assertLess(compose, refresh_guard)
        self.assertLess(refresh_guard, refresh)
        refresh_slice = up[refresh : up.index('  else\n    CMD=("${CMD_BASE[@]}"', refresh)]
        self.assertIn('"--no-deps" "--force-recreate" "caddy"', refresh_slice)
        self.assertIn("Refreshing VPS Caddy bind mounts", refresh_slice)
        self.assertIn("VPS Caddy bind-mount refresh failed.", refresh_slice)
        self.assertNotIn('"db"', refresh_slice)
        self.assertNotIn('"nats"', refresh_slice)

    def test_weltgewebe_up_normalizes_release_trailing_slashes(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weltgewebe-release-activation-"
        ) as tmp:
            root = Path(tmp)
            commit = "a" * 40
            release_root = root / "releases"
            release_dir = release_root / commit
            (release_dir / "infra" / "compose").mkdir(parents=True)
            (release_dir / "scripts" / "ops").mkdir(parents=True)
            shutil.copy2(
                ROOT / "scripts" / "ops" / "resolve_vps_public_bind.py",
                release_dir / "scripts" / "ops" / "resolve_vps_public_bind.py",
            )
            for activation_dir in (
                release_root,
                release_dir,
                release_dir / "scripts",
                release_dir / "scripts" / "ops",
            ):
                activation_dir.chmod(0o755)
            (release_dir / "infra" / "compose" / "compose.prod.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            runtime_env = root / "runtime.env"
            runtime_env.write_text("TEST_ONLY=1\n", encoding="utf-8")
            activation_log = root / "activation.log"
            activator = (
                release_dir
                / "scripts"
                / "ops"
                / "activate-production-reconciler-from-release.sh"
            )
            activator.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                f"printf '%s\\n' \"$*\" > {activation_log!s}\n",
                encoding="utf-8",
            )
            activator.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "DEPLOY_TARGET": "vps",
                    "CADDY_BIND": "203.0.113.10",
                    "CADDY_IPV6_BIND": "[2001:db8::10]",
                    "ENV_FILE": str(runtime_env),
                    "REPO_DIR": f"{release_dir}/",
                    "WELTGEWEBE_DEPLOY_LOCK_FILE": str(root / "deploy.lock"),
                    "WELTGEWEBE_RELEASE_ROOT": f"{release_root}/",
                    "WELTGEWEBE_STATE_DIR": str(root / "state"),
                }
            )
            completed = subprocess.run(
                [str(ROOT / "scripts" / "weltgewebe-up"), "--no-pull"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(activation_log.is_file())
            activation_args = activation_log.read_text(encoding="utf-8").strip()
            self.assertIn(f"--release-dir {release_dir}", activation_args)
            self.assertIn(f"--commit {commit}", activation_args)

            activation_log.unlink()
            activator.chmod(0o775)
            unsafe = subprocess.run(
                [str(ROOT / "scripts" / "weltgewebe-up"), "--no-pull"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(unsafe.returncode, 0)
            self.assertFalse(activation_log.exists())
            self.assertIn(
                "production reconciler activator has unsafe ownership or mode",
                unsafe.stderr,
            )

    def test_release_activator_rejects_unsafe_and_non_git_releases(self) -> None:
        sudo_probe = subprocess.run(
            ["sudo", "-n", "true"],
            check=False,
            capture_output=True,
            text=True,
        )
        if sudo_probe.returncode != 0:
            self.skipTest("passwordless sudo is unavailable for root-safety tests")

        base = Path(tempfile.mkdtemp(prefix="weltgewebe-activator-root-"))
        commit = "b" * 40
        script = (
            ROOT / "scripts" / "ops" / "activate-production-reconciler-from-release.sh"
        )
        build_user = os.environ.get("USER", "runner")
        try:
            unsafe_root = base / "unsafe-releases"
            unsafe_dir = unsafe_root / commit
            (unsafe_dir / "scripts" / "ops").mkdir(parents=True)
            unsafe = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "env",
                    f"WELTGEWEBE_RELEASE_ROOT={unsafe_root}",
                    str(script),
                    "--release-dir",
                    str(unsafe_dir),
                    "--commit",
                    commit,
                    "--build-user",
                    build_user,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unsafe.returncode, 0)
            self.assertIn("release root is not root-owned", unsafe.stderr)

            safe_root = base / "safe-releases"
            safe_dir = safe_root / commit
            installer = (
                safe_dir / "scripts" / "ops" / "install-production-reconciler.sh"
            )
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "install",
                    "-d",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0755",
                    str(installer.parent),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0755",
                    "/bin/true",
                    str(installer),
                ],
                check=True,
            )
            invalid_git = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "env",
                    f"WELTGEWEBE_RELEASE_ROOT={safe_root}",
                    str(script),
                    "--release-dir",
                    str(safe_dir),
                    "--commit",
                    commit,
                    "--build-user",
                    build_user,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(invalid_git.returncode, 0)
            self.assertIn(
                "release directory is not a valid Git repository", invalid_git.stderr
            )

            release_root_link = base / "release-root-link"
            release_root_link.symlink_to(safe_root, target_is_directory=True)
            symlinked = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "env",
                    f"WELTGEWEBE_RELEASE_ROOT={release_root_link}",
                    str(script),
                    "--release-dir",
                    str(safe_dir),
                    "--commit",
                    commit,
                    "--build-user",
                    build_user,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(symlinked.returncode, 0)
            self.assertIn("release root is missing or unsafe", symlinked.stderr)
        finally:
            subprocess.run(
                ["sudo", "-n", "rm", "-rf", "--", str(base)],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_staged_unit_verification_allows_missing_initial_env_file(self) -> None:
        installer = self.read("scripts/ops/install-production-reconciler.sh")
        service = self.read(
            "infra/systemd/system/weltgewebe-production-reconcile.service"
        )
        self.assertIn(
            "EnvironmentFile=-/etc/weltgewebe/production-reconciler.env", service
        )
        staged_verify = installer.index("systemd-analyze verify")
        env_install = installer.index(
            'atomic_install "$staging/production-reconciler.env"'
        )
        self.assertLess(staged_verify, env_install)

    def test_release_activator_rejects_non_root_execution(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("non-root guard requires a non-root test process")
        completed = subprocess.run(
            [
                str(
                    ROOT
                    / "scripts"
                    / "ops"
                    / "activate-production-reconciler-from-release.sh"
                ),
                "--release-dir",
                "/tmp/not-a-production-release",
                "--commit",
                "0" * 40,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must run as root", completed.stderr)

    def test_github_contract_serializes_main_observers(self) -> None:
        workflow = self.read(".github/workflows/production-live-contract.yml")
        self.assertIn("production-live-main", workflow)
        self.assertIn(
            "github.event_name == 'pull_request' || github.event_name == 'push'",
            workflow,
        )
        self.assertIn("ref: main", workflow)
        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertIn("wait_seconds=1200", workflow)
        self.assertNotIn("production-live-contract-${{ github.event_name }}", workflow)
        self.assertIn("+refs/heads/main:refs/remotes/origin/main", workflow)
        self.assertIn('BASE_SHA="$(git rev-parse refs/remotes/origin/main)"', workflow)
        self.assertIn('git diff --binary "$BASE_SHA...$HEAD_SHA"', workflow)
        self.assertIn("review-diff-manifest.txt", workflow)
        self.assertIn("review-diff-pr-", workflow)
        self.assertIn(
            '"scripts/ops/activate-production-reconciler-from-release.sh"', workflow
        )
        self.assertIn('"scripts/weltgewebe-up"', workflow)
        self.assertIn(
            "bash -n scripts/ops/activate-production-reconciler-from-release.sh",
            workflow,
        )
        self.assertIn("bash -n scripts/weltgewebe-up", workflow)


if __name__ == "__main__":
    unittest.main()
