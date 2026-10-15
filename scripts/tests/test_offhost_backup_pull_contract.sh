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
recipients=${4:-$recipient}
plaintext_file=$plain
plaintext_sha256=$3
MANIFEST
}

# The pull side pins the accepted recipients independently of the producer.
pinned="$root/pinned-recipients.txt"
printf '# offline identity\n%s\n' "$recipient" > "$pinned"
foreign_recipient="age1$(printf 'z%.0s' $(seq 1 58))"

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
# Simulates another writer (e.g. a legacy pull) dropping plaintext while this
# pull is still transferring.
if [[ -n "${INJECT_PLAINTEXT_DURING_CAT:-}" && "$cmd" == *'cat -- '* ]]; then
  printf 'late legacy dump\n' > "$INJECT_PLAINTEXT_DURING_CAT"
fi
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
    PULL_AGE_RECIPIENTS_FILE="${PULL_AGE_RECIPIENTS_FILE-$pinned}" scripts/ops/pull-production-postgres-backup.sh
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

# Simulate a denied sudo-find through the remote shell's PATH.
cat > "$bin/find" << 'FAKE_FIND'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${SIMULATE_SUDO_FIND_DENIED:-0}" == 1 ]]; then
  printf 'sudo: remote find denied\n' >&2
  exit 37
fi
exec /usr/bin/find "$@"
FAKE_FIND
chmod +x "$bin/find"
if SIMULATE_SUDO_FIND_DENIED=1 pull "$root/denied-find" test-host > /dev/null 2> "$root/denied-find.stderr"; then
  echo 'denied remote find must fail' >&2
  exit 1
fi
grep -qF 'sudo: remote find denied' "$root/denied-find.stderr"
if grep -qF 'remote host returned no complete encrypted backup set' "$root/denied-find.stderr"; then
  echo 'remote find denial was hidden by remote pipeline' >&2
  exit 1
fi

# Legacy off-host plaintext must stop before an age success receipt.
legacy_dest="$root/legacy"
mkdir -p "$legacy_dest"
printf 'old private pg dump\n' > "$legacy_dest/weltgewebe-postgres-legacy.sql.gz"
if pull "$legacy_dest" test-host > /dev/null 2> "$root/legacy.stderr"; then
  echo 'legacy plaintext alongside successful encrypted pull is forbidden' >&2
  exit 1
fi
grep -qF 'legacy plaintext PostgreSQL backups' "$root/legacy.stderr"
[[ -f "$legacy_dest/weltgewebe-postgres-legacy.sql.gz" ]]
[[ ! -e "$legacy_dest/latest-pull.receipt" ]]

# The legacy gate must not depend on the active BACKUP_PREFIX.
other_prefix_dest="$root/legacy-other-prefix"
mkdir -p "$other_prefix_dest"
printf 'old private pg dump\n' > "$other_prefix_dest/earlier-prefix-20250101T040000Z.sql.gz"
if pull "$other_prefix_dest" test-host > /dev/null 2> "$root/legacy-other-prefix.stderr"; then
  echo 'legacy plaintext under an earlier prefix must block the age receipt' >&2
  exit 1
fi
grep -qF 'legacy plaintext PostgreSQL backups' "$root/legacy-other-prefix.stderr"
[[ ! -e "$other_prefix_dest/latest-pull.receipt" ]]

# Plaintext left in an abandoned work directory of an interrupted legacy pull
# is below the Restic source too and must block the age receipt.
abandoned_dest="$root/legacy-abandoned-pull"
mkdir -p "$abandoned_dest/.pull.Ab12Cd"
printf 'old private pg dump\n' > "$abandoned_dest/.pull.Ab12Cd/weltgewebe-postgres-legacy.sql.gz"
if pull "$abandoned_dest" test-host > /dev/null 2> "$root/legacy-abandoned.stderr"; then
  echo 'legacy plaintext in an abandoned pull directory must block the age receipt' >&2
  exit 1
fi
grep -qF 'legacy plaintext PostgreSQL backups' "$root/legacy-abandoned.stderr"
[[ ! -e "$abandoned_dest/latest-pull.receipt" ]]

# Explicit migration staging must permit a ciphertext-only first transfer while
# legacy recovery points remain untouched, without publishing an age receipt.
stage_dest="$root/staged-age"
PULL_MODE=migration-stage MIGRATION_STAGE_DIR="$stage_dest" pull "$legacy_dest" test-host
[[ -f "$stage_dest/$file" ]]
[[ -f "$stage_dest/$age_manifest" ]]
[[ -f "$stage_dest/$manifest" ]]
[[ -f "$legacy_dest/weltgewebe-postgres-legacy.sql.gz" ]]
[[ ! -e "$stage_dest/latest-pull.receipt" ]]
[[ ! -e "$legacy_dest/latest-pull.receipt" ]]
[[ ! -e "$legacy_dest/$file" ]]
[[ "$(stat -c %a "$stage_dest")" == 700 ]]
if PULL_MODE=migration-stage MIGRATION_STAGE_DIR="$legacy_dest/unsafe-staging" \
  pull "$legacy_dest" test-host > /dev/null 2> "$root/unsafe-staging.stderr"; then
  echo 'staging inside legacy destination must fail closed' >&2
  exit 1
fi
grep -qF 'outside the normal backup destination' "$root/unsafe-staging.stderr"
if PULL_MODE=migration-stage pull "$legacy_dest" test-host > /dev/null 2> "$root/missing-staging.stderr"; then
  echo 'migration-stage must require an explicit destination' >&2
  exit 1
fi
grep -qF 'MIGRATION_STAGE_DIR is required' "$root/missing-staging.stderr"

# Reusing a staging location is forbidden: migration data cannot be overwritten.
if PULL_MODE=migration-stage MIGRATION_STAGE_DIR="$stage_dest" \
  pull "$legacy_dest" test-host > /dev/null 2> "$root/duplicate-stage.stderr"; then
  echo 'existing staging candidate must be create-only' >&2
  exit 1
fi
grep -qF 'migration staging artefact already exists' "$root/duplicate-stage.stderr"
if PULL_MODE=unsafe pull "$legacy_dest" test-host > /dev/null 2> "$root/unsafe-mode.stderr"; then
  echo 'unknown pull mode must fail closed' >&2
  exit 1
fi
grep -qF 'PULL_MODE must be normal or migration-stage' "$root/unsafe-mode.stderr"
if ((REAL_AGE == 1)); then
  mkdir -m 700 "$root/stage-restore"
  age -d -i "$root/offline-identity.txt" -o "$root/stage-restore/$plain" "$stage_dest/$file"
  [[ "$(sha256sum "$root/stage-restore/$plain" | awk '{print $1}')" == "$plain_sha" ]]
  gzip -t "$root/stage-restore/$plain"
  [[ "$(gunzip -c "$root/stage-restore/$plain")" == "$marker" ]]
fi

# The actual nightly Restic job sources ~/artifacts/merges (not ~/merges).
# Staging inside that backup source is forbidden even if legacy destination differs.
mkdir -p "$root/controlled-home/artifacts/merges"
if HOME="$root/controlled-home" PULL_MODE=migration-stage \
  MIGRATION_STAGE_DIR="$root/controlled-home/artifacts/merges/candidate" \
  pull "$legacy_dest" test-host > /dev/null 2> "$root/restic-source.stderr"; then
  echo 'staging inside nightly Restic artifact merges source must fail' >&2
  exit 1
fi
grep -qF 'Restic sources' "$root/restic-source.stderr"
[[ ! -e "$root/controlled-home/artifacts/merges/candidate/$file" ]]

# Retention fixture: expired local artefacts, including legacy plaintext copies.
mkdir -p "$dest"
for suffix in .sql.gz.age .sha256.manifest .age.sha256.manifest .restore-proof; do
  touch -d '10 days ago' "$dest/weltgewebe-postgres-20260101T000000Z${suffix}"
done
PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_DIR=1 REMOTE_HOST='' REMOTE_BACKUP_DIR="$remote" DEST_DIR="$dest" \
  PULL_AGE_RECIPIENTS_FILE="$pinned" LOCAL_RETENTION_DAYS=1 scripts/ops/pull-production-postgres-backup.sh
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
grep -qx "recipients=$recipient" "$dest/latest-pull.receipt"
# Each completed pull keeps its own immutable receipt; a later pull adds one
# and leaves the earlier one byte-identical.
mapfile -t history < <(find "$dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#history[@]} == 1))
cmp -s "${history[0]}" "$dest/latest-pull.receipt"
first_receipt="${history[0]}"
cp "$first_receipt" "$root/first-receipt"
pull "$dest" test-host > /dev/null
mapfile -t history < <(find "$dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#history[@]} == 2))
cmp -s "$root/first-receipt" "$first_receipt"
grep -qx 'remote_host=test-host' "$dest/latest-pull.receipt"
[[ "$(stat -c %a "$dest/$file")" == 600 ]]
if compgen -G "$dest/weltgewebe-postgres-20260101T000000Z*" > /dev/null; then
  echo 'local retention must expire ciphertext, manifests and proofs' >&2
  exit 1
fi

# Plaintext never lands off-host and never crosses the (fake) SSH link.
if compgen -G "$dest/*.sql.gz" > /dev/null; then
  echo 'plaintext dump landed in the off-host destination' >&2
  exit 1
fi
for pulled in "$dest"/*; do
  [[ -f "$pulled" ]] || continue
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

# A corrupt optional weekly proof must fail before any encrypted backup files
# become visible in the destination (completion receipt is not enough).
printf 'contract=weltgewebe-postgres-restore-proof-v1\nresult=failed\n' > "$remote/proofs/$base.restore-proof"
expect_pull_failure "$root/bad-proof" 'remote restore proof is not successful'
printf 'contract=weltgewebe-postgres-restore-proof-v1\nresult=ok\n' > "$remote/proofs/$base.restore-proof"

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

# A backup encrypted to a recipient that is not pinned on the pull side fails
# closed: the offline identity could not decrypt it, or a foreign key could.
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha" "$foreign_recipient"
expect_pull_failure "$root/foreign-recipient" 'not pinned'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha" "$recipient,$foreign_recipient"
expect_pull_failure "$root/extra-recipient" 'not pinned'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha"
PULL_AGE_RECIPIENTS_FILE="$root/missing-pinned.txt" expect_pull_failure "$root/no-pin" 'pinned age recipients file is missing'

# Recipient declarations must be unambiguous: exactly one field, canonical
# list, no empty or duplicate member.
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha"
printf 'recipients=%s\n' "$foreign_recipient" >> "$remote/$age_manifest"
expect_pull_failure "$root/dup-field" 'declare age recipients exactly once'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha" "$recipient,"
expect_pull_failure "$root/trailing-comma" 'not a canonical comma-separated age1 list'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha" ",$recipient"
expect_pull_failure "$root/leading-comma" 'not a canonical comma-separated age1 list'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha" "$recipient,$recipient"
expect_pull_failure "$root/dup-member" 'lists an age recipient twice'
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha"

# Plaintext that appears after the first scan still blocks the receipt.
late_dest="$root/late-plaintext"
mkdir -p "$late_dest"
if INJECT_PLAINTEXT_DURING_CAT="$late_dest/weltgewebe-postgres-late.sql.gz" \
  pull "$late_dest" test-host > /dev/null 2> "$root/late.stderr"; then
  echo 'plaintext created during the pull must block the receipt' >&2
  exit 1
fi
grep -qF 'legacy plaintext PostgreSQL backups' "$root/late.stderr"
[[ ! -e "$late_dest/latest-pull.receipt" && ! -e "$late_dest/receipts" ]]

# Concurrent pulls into one destination are serialized fail-closed.
locked_dest="$root/locked"
mkdir -p "$locked_dest"
exec 8> "$locked_dest/.pull.lock"
flock -n 8
expect_pull_failure "$locked_dest" 'another pull holds'
exec 8>&-

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
