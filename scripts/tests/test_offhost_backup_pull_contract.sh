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
base=weltgewebe-postgres-20260712T040000Z
plain="$base.sql.gz"
file="$base.sql.gz.age"
age_manifest="$base.age.sha256.manifest"
manifest="$base.sha256.manifest"
marker='private-home-anchor-fixture person@example.invalid'

# Real age when available; otherwise an age-shaped stub without cryptography
# (the pull side never needs age, only the producer and the offline restore do).
REAL_AGE=0
if [[ "${WELTGEWEBE_TEST_FORCE_AGE_STUB:-0}" != 1 ]] && command -v age > /dev/null 2>&1 && command -v age-keygen > /dev/null 2>&1; then
  REAL_AGE=1
  age-keygen -o "$root/offline-identity.txt" 2> /dev/null
  recipient="$(age-keygen -y "$root/offline-identity.txt")"
else
  [[ "${WELTGEWEBE_TEST_REQUIRE_REAL_AGE:-0}" != 1 ]] || {
    echo 'real age/age-keygen required but not found' >&2
    exit 1
  }
  recipient="age1$(printf 'q%.0s' $(seq 1 58))"
fi
encrypt_fixture() {
  if ((REAL_AGE == 1)); then
    age -r "$recipient" -o "$2" "$1"
  else
    {
      printf 'age-encryption.org/v1\n-> X25519 stub\n--- stub\n'
      base64 < "$1"
    } > "$2"
  fi
}
write_age_manifest() {
  cat > "$remote/$age_manifest" << MANIFEST
contract=weltgewebe-postgres-backup-age-v1
created_at=2026-07-12T04:00:00Z
file=$file
sha256=$1
bytes=$2
format=age-encryption.org/v1
recipients=$recipient
plaintext_file=$plain
plaintext_sha256=$3
MANIFEST
}

printf '%s\n' "$marker" | gzip -c > "$remote/$plain"
plain_sha="$(sha256sum "$remote/$plain" | awk '{print $1}')"
cat > "$remote/$manifest" << MANIFEST
contract=weltgewebe-postgres-backup-v1
file=$plain
sha256=$plain_sha
MANIFEST
encrypt_fixture "$remote/$plain" "$remote/$file"
age_sha="$(sha256sum "$remote/$file" | awk '{print $1}')"
age_bytes="$(wc -c < "$remote/$file" | tr -d '[:space:]')"
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha"
# A newer host-local plaintext dump without ciphertext must never be selected.
printf 'newer plaintext\n' | gzip -c > "$remote/weltgewebe-postgres-20260713T040000Z.sql.gz"
# A newer ciphertext whose off-host manifest (the completion marker, published
# last) is missing is an interrupted run and must not be selected either.
printf 'age-encryption.org/v1\ninterrupted\n' > "$remote/weltgewebe-postgres-20260713T040000Z.sql.gz.age"
cat > "$remote/proofs/$base.restore-proof" << PROOF
contract=weltgewebe-postgres-restore-proof-v1
result=ok
PROOF
cat > "$bin/ssh" << 'FAKE'
#!/usr/bin/env bash
set -euo pipefail
cmd="${!#}"
cmd="${cmd//sudo -n /}"
printf '%s\n' "$cmd" >> "$SSH_LOG"
exec bash -c "$cmd"
FAKE
chmod +x "$bin/ssh"
export SSH_LOG="$root/ssh.log"
# Prefixes occur inside SSH's remote shell command; reject shell quoting tokens
# before the first SSH invocation, rather than relying on a later filename check.
if BACKUP_PREFIX="unsafe'prefix" PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_DIR=1 \
  REMOTE_HOST=test-host REMOTE_BACKUP_DIR="$remote" DEST_DIR="$root/unsafe-prefix" \
  scripts/ops/pull-production-postgres-backup.sh > /dev/null 2> "$root/unsafe-prefix.stderr"; then
  echo 'untrusted BACKUP_PREFIX must fail closed' >&2
  exit 1
fi
grep -qF 'BACKUP_PREFIX contains unsupported characters' "$root/unsafe-prefix.stderr" || {
  echo 'untrusted prefix was not rejected before SSH' >&2
  exit 1
}
[[ ! -s "$SSH_LOG" ]] || {
  echo 'unsafe prefix reached SSH command' >&2
  exit 1
}
pull() {
  local destination="$1"
  PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_DIR=1 REMOTE_HOST="${2-}" REMOTE_BACKUP_DIR="$remote" DEST_DIR="$destination" \
    scripts/ops/pull-production-postgres-backup.sh
}
expect_pull_failure() {
  local destination="$1" message="$2"
  if pull "$destination" test-host > /dev/null 2> "$root/pull.stderr"; then
    echo "expected pull to fail: $message" >&2
    exit 1
  fi
  grep -qF "$message" "$root/pull.stderr" || {
    echo "unexpected pull failure output (wanted: $message):" >&2
    cat "$root/pull.stderr" >&2
    exit 1
  }
  if compgen -G "$destination/weltgewebe-postgres-*" > /dev/null; then
    echo "failed pull must not promote any artefact: $message" >&2
    exit 1
  fi
}

# Retention fixture: expired local artefacts, including legacy plaintext copies.
mkdir -p "$dest"
for suffix in .sql.gz .sql.gz.age .sha256.manifest .age.sha256.manifest .restore-proof; do
  touch -d '10 days ago' "$dest/weltgewebe-postgres-20260101T000000Z${suffix}"
done
PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_DIR=1 REMOTE_HOST='' REMOTE_BACKUP_DIR="$remote" DEST_DIR="$dest" \
  LOCAL_RETENTION_DAYS=1 scripts/ops/pull-production-postgres-backup.sh
[[ -f "$dest/$file" ]]
[[ -f "$dest/$age_manifest" ]]
[[ -f "$dest/$manifest" ]]
[[ -f "$dest/$base.restore-proof" ]]
grep -qx 'result=ok' "$dest/latest-pull.receipt"
grep -qx 'remote_host=commonserver' "$dest/latest-pull.receipt"
grep -qx 'contract=weltgewebe-offhost-backup-pull-v2' "$dest/latest-pull.receipt"
grep -qx 'encryption=age' "$dest/latest-pull.receipt"
grep -qx "sha256=$age_sha" "$dest/latest-pull.receipt"
grep -qx "plaintext_sha256=$plain_sha" "$dest/latest-pull.receipt"
[[ "$(stat -c %a "$dest/$file")" == 600 ]]
if compgen -G "$dest/weltgewebe-postgres-20260101T000000Z*" > /dev/null; then
  echo 'local retention must expire ciphertext, manifests, proofs and legacy plaintext' >&2
  exit 1
fi

# Plaintext never lands off-host and never crosses the (fake) SSH link.
if compgen -G "$dest/*.sql.gz" > /dev/null; then
  echo 'plaintext dump landed in the off-host destination' >&2
  exit 1
fi
for pulled in "$dest"/*; do
  if gzip -t "$pulled" 2> /dev/null; then
    echo "pulled file is a plaintext gzip stream: $pulled" >&2
    exit 1
  fi
  if grep -qF "$marker" "$pulled"; then
    echo "pulled file contains plaintext dump content: $pulled" >&2
    exit 1
  fi
done
if grep -E "cat -- '[^']*\.sql\.gz'" "$SSH_LOG"; then
  echo 'pull requested a plaintext dump over SSH' >&2
  exit 1
fi
grep -qF "cat -- '$remote/$file'" "$SSH_LOG"

if ((REAL_AGE == 1)); then
  # Offline restore path: decrypt the pulled copy with the offline identity and
  # verify it against the pulled plaintext metadata manifest.
  restore="$root/offline-restore"
  mkdir -m 700 "$restore"
  age -d -i "$root/offline-identity.txt" -o "$restore/$plain" "$dest/$file"
  cmp -s "$restore/$plain" "$remote/$plain"
  [[ "$(sha256sum "$restore/$plain" | awk '{print $1}')" == "$(awk -F= '$1=="sha256" {print $2}' "$dest/$manifest")" ]]
  [[ "$(gunzip -c "$restore/$plain")" == "$marker" ]]
fi

# A ciphertext sha256 mismatch must fail and must not promote anything.
write_age_manifest "$(printf '%064d' 0)" "$age_bytes" "$plain_sha"
expect_pull_failure "$root/bad-sha" 'ciphertext sha256 mismatch'

# A plaintext file disguised as the encrypted artefact (matching sha) is rejected.
cp "$remote/$file" "$root/real.age"
cp "$remote/$plain" "$remote/$file"
write_age_manifest "$(sha256sum "$remote/$file" | awk '{print $1}')" "$(wc -c < "$remote/$file" | tr -d '[:space:]')" "$plain_sha"
expect_pull_failure "$root/bad-format" 'is not an age-encrypted file'
cp "$root/real.age" "$remote/$file"

# Manifests that disagree about the plaintext are rejected.
write_age_manifest "$age_sha" "$age_bytes" "$(printf '%064d' 1)"
expect_pull_failure "$root/bad-plain" 'manifests disagree'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha"

# A host without an encrypted artefact (e.g. no recipient configured) fails closed.
rm -f "$remote/$file" "$remote/$age_manifest"
expect_pull_failure "$root/no-age" 'no complete encrypted backup set'
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

if ((REAL_AGE == 1)); then
  printf 'test_offhost_backup_pull_contract: OK (real age round trip)\n'
else
  printf 'test_offhost_backup_pull_contract: OK (age stub; round trip skipped)\n'
fi
