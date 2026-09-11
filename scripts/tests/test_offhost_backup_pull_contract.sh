#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." > /dev/null 2>&1 && pwd)"
INSTALLER="$REPO_ROOT/scripts/ops/install-postgres-offhost-pull-user-units.sh"
SERVICE_TEMPLATE="$REPO_ROOT/scripts/ops/systemd/weltgewebe-postgres-offhost-pull.service"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
remote="$root/remote"
dest="$root/dest"
bin="$root/bin"
mkdir -p "$remote/proofs" "$bin"
file=weltgewebe-postgres-20260712T040000Z.sql.gz
printf 'test dump\n' | gzip -c > "$remote/$file"
sha="$(sha256sum "$remote/$file" | awk '{print $1}')"
cat > "$remote/${file%.sql.gz}.sha256.manifest" << MANIFEST
contract=weltgewebe-postgres-backup-v1
file=$file
sha256=$sha
MANIFEST
cat > "$remote/proofs/${file%.sql.gz}.restore-proof" << PROOF
contract=weltgewebe-postgres-restore-proof-v1
result=ok
PROOF
cat > "$bin/ssh" << 'FAKE'
#!/usr/bin/env bash
set -euo pipefail
cmd="${!#}"
cmd="${cmd//sudo -n /}"
exec bash -c "$cmd"
FAKE
chmod +x "$bin/ssh"
PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_DIR=1 REMOTE_HOST='' REMOTE_BACKUP_DIR="$remote" DEST_DIR="$dest" \
  scripts/ops/pull-production-postgres-backup.sh
[[ -f "$dest/$file" ]]
[[ -f "$dest/${file%.sql.gz}.sha256.manifest" ]]
[[ -f "$dest/${file%.sql.gz}.restore-proof" ]]
grep -qx 'result=ok' "$dest/latest-pull.receipt"
grep -qx 'remote_host=commonserver' "$dest/latest-pull.receipt"
[[ "$(stat -c %a "$dest/$file")" == 600 ]]

# A manifest mismatch must fail and must not overwrite the verified copy.
printf 'sha256=%064d\n' 0 > "$remote/${file%.sql.gz}.sha256.manifest"
if PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_DIR=1 REMOTE_HOST=test-host REMOTE_BACKUP_DIR="$remote" DEST_DIR="$root/bad" \
  scripts/ops/pull-production-postgres-backup.sh > /dev/null 2>&1; then
  echo 'expected sha mismatch to fail' >&2
  exit 1
fi
# The user service is generated from a clean commit placeholder and must avoid
# ProtectKernelModules, which is unsupported by the real user manager on heim-pc.
grep -q '@WELTGEWEBE_COMMIT@' "$SERVICE_TEMPLATE"
if grep -q '^ProtectKernelModules=' "$SERVICE_TEMPLATE"; then
  echo 'user service must not request ProtectKernelModules' >&2
  exit 1
fi
install_home="$root/install-home"
fixture_repo="$root/install-repo"
mkdir -p "$install_home" "$fixture_repo/scripts/ops/systemd"
cp "$REPO_ROOT/scripts/ops/pull-production-postgres-backup.sh" "$fixture_repo/scripts/ops/"
cp "$SERVICE_TEMPLATE" "$fixture_repo/scripts/ops/systemd/"
cp "$REPO_ROOT/scripts/ops/systemd/weltgewebe-postgres-offhost-pull.timer" "$fixture_repo/scripts/ops/systemd/"
git -C "$fixture_repo" init -q
git -C "$fixture_repo" config user.name contract-test
git -C "$fixture_repo" config user.email contract-test@example.invalid
git -C "$fixture_repo" add scripts
git -C "$fixture_repo" commit -qm 'test: bind off-host installer fixture'
commit="$(git -C "$fixture_repo" rev-parse HEAD)"
HOME="$install_home" TARGET_HOME="$install_home" REPO_DIR="$fixture_repo" \
  EXPECTED_COMMIT="$commit" SYSTEMCTL_BIN=/bin/true \
  "$INSTALLER" --enable-now > /dev/null
installed_pull="$install_home/.local/libexec/weltgewebe/$commit/pull-production-postgres-backup.sh"
installed_service="$install_home/.config/systemd/user/weltgewebe-postgres-offhost-pull.service"
receipt="$install_home/.local/libexec/weltgewebe/$commit/install.receipt"
[[ -x "$installed_pull" ]]
cmp -s "$fixture_repo/scripts/ops/pull-production-postgres-backup.sh" "$installed_pull"
grep -qxF "ExecStart=%h/.local/libexec/weltgewebe/$commit/pull-production-postgres-backup.sh" "$installed_service"
if grep -q '@WELTGEWEBE_COMMIT@' "$installed_service"; then
  echo 'generated service retained the commit placeholder' >&2
  exit 1
fi
if grep -q '^ProtectKernelModules=' "$installed_service"; then
  echo 'generated user service retained ProtectKernelModules' >&2
  exit 1
fi
grep -qxF 'contract=weltgewebe-postgres-offhost-user-install-v1' "$receipt"
grep -Eq '^installed_at=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' "$receipt"
grep -qxF "git_commit=$commit" "$receipt"
[[ "$(stat -c %a "$receipt")" == 600 ]]
[[ "$(stat -c %a "$install_home/merges/weltgewebe-production-backups")" == 700 ]]
# Reinstalling the same commit is idempotent.
HOME="$install_home" TARGET_HOME="$install_home" REPO_DIR="$fixture_repo" \
  EXPECTED_COMMIT="$commit" SYSTEMCTL_BIN=/bin/true \
  "$INSTALLER" > /dev/null

printf 'test_offhost_backup_pull_contract: OK\n'
