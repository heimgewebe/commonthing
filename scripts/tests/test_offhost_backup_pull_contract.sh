#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." > /dev/null 2>&1 && pwd)"
INSTALLER="$REPO_ROOT/scripts/ops/install-postgres-offhost-pull-user-units.sh"
READER_SOURCE="$REPO_ROOT/scripts/ops/postgres-backup-read.sh"
GATE="$REPO_ROOT/scripts/ops/restic-plaintext-gate.sh"
SERVICE_TEMPLATE="$REPO_ROOT/scripts/ops/systemd/weltgewebe-postgres-offhost-pull.service"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
remote="$root/remote"
dest="$root/dest"
bin="$root/bin"
mkdir -p "$remote/proofs" "$bin"
chmod 700 "$remote" "$remote/proofs"
# The production reader fixes its directory and owner; the fixture copy points
# them at the fake remote, everything else is the shipped script.
reader="$bin/postgres-backup-read"
sed -e "s|^readonly BACKUP_DIR=.*|readonly BACKUP_DIR=$remote|" \
  -e "s|^readonly BACKUP_OWNER_UID=.*|readonly BACKUP_OWNER_UID=$(id -u)|" \
  "$READER_SOURCE" > "$reader"
chmod 755 "$reader"
grep -qx "readonly BACKUP_DIR=$remote" "$reader"
grep -qx "readonly BACKUP_OWNER_UID=$(id -u)" "$reader"
base=weltgewebe-postgres-20260712T040000Z
plain="$base.sql.gz"
file="$base.sql.gz.age"
age_manifest="$base.age.sha256.manifest"
manifest="$base.sha256.manifest"
proof="$base.restore-proof"
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
# The client may only ever run the reader through non-interactive sudo.
[[ "$cmd" == "sudo -n '"* ]] || {
  printf 'fake ssh: command does not start with sudo -n: %s\n' "$cmd" >&2
  exit 99
}
if [[ "${SIMULATE_SUDO_DENIED:-0}" == 1 ]]; then
  printf 'sudo: a password is required\n' >&2
  exit 1
fi
cmd="${cmd#sudo -n }"
printf '%s\n' "$cmd" >> "$SSH_LOG"
if [[ "${SIMULATE_PROOF_SSH_FAILURE:-0}" == 1 && "$cmd" == *' proof-state '* ]]; then
  printf 'ssh: connection reset\n' >&2
  exit 255
fi
# Simulates another writer (e.g. a legacy pull) dropping plaintext while this
# pull is still transferring.
if [[ -n "${INJECT_PLAINTEXT_DURING_CAT:-}" && "$cmd" == *' read '* ]]; then
  printf 'late legacy dump\n' > "$INJECT_PLAINTEXT_DURING_CAT"
fi
exec bash -c "$cmd"
FAKE
chmod +x "$bin/ssh"
export SSH_LOG="$root/ssh.log"
# Prefixes occur inside SSH's remote shell command; reject shell quoting tokens
# before the first SSH invocation, rather than relying on a later filename check.
if BACKUP_PREFIX="unsafe'prefix" PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_READER=1 \
  REMOTE_HOST=test-host REMOTE_BACKUP_READER="$reader" DEST_DIR="$root/unsafe-prefix" \
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
  PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_READER=1 REMOTE_HOST="${2-}" REMOTE_BACKUP_READER="$reader" DEST_DIR="$destination" \
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

# A sudo rule that does not grant the reader must surface as such, not as an
# empty backup listing.
if SIMULATE_SUDO_DENIED=1 pull "$root/denied-reader" test-host > /dev/null 2> "$root/denied-reader.stderr"; then
  echo 'denied remote reader must fail' >&2
  exit 1
fi
grep -qF 'sudo: a password is required' "$root/denied-reader.stderr"
grep -qF 'could not list remote backups' "$root/denied-reader.stderr"
if grep -qF 'remote host returned no complete encrypted backup set' "$root/denied-reader.stderr"; then
  echo 'remote reader denial was hidden as an empty listing' >&2
  exit 1
fi

# The backup directory is fixed in the reader; neither a leftover directory
# override nor another reader path is accepted outside the test switch.
if REMOTE_BACKUP_DIR="$remote" pull "$root/dir-override" test-host > /dev/null 2> "$root/dir-override.stderr"; then
  echo 'REMOTE_BACKUP_DIR must be rejected' >&2
  exit 1
fi
grep -qF 'REMOTE_BACKUP_DIR is no longer supported' "$root/dir-override.stderr"
if PATH="$bin:$PATH" REMOTE_HOST=test-host REMOTE_BACKUP_READER="$reader" DEST_DIR="$root/reader-override" \
  PULL_AGE_RECIPIENTS_FILE="$pinned" scripts/ops/pull-production-postgres-backup.sh > /dev/null 2> "$root/reader-override.stderr"; then
  echo 'a non-default reader path must be rejected' >&2
  exit 1
fi
grep -qF 'REMOTE_BACKUP_READER must be /usr/local/libexec/weltgewebe/postgres-backup-read' "$root/reader-override.stderr"

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

# A symlinked destination must not let GNU find -P skip existing legacy
# plaintext and emit a false age-only success receipt at its real target.
symlink_real="$root/legacy-symlink-real"
symlink_dest="$root/legacy-symlink-alias"
mkdir -p "$symlink_real"
printf 'old private pg dump\n' > "$symlink_real/earlier-prefix-private.sql.gz"
ln -s "$symlink_real" "$symlink_dest"
if pull "$symlink_dest" test-host > /dev/null 2> "$root/legacy-symlink.stderr"; then
  echo 'symlinked legacy plaintext must block the encrypted pull receipt' >&2
  exit 1
fi
[[ ! -e "$symlink_real/latest-pull.receipt" ]] || {
  echo 'symlinked legacy plaintext produced a false age-only receipt' >&2
  exit 1
}
[[ -f "$symlink_real/earlier-prefix-private.sql.gz" ]]

# A deliberately configured symlink to a clean destination remains usable;
# retention must operate on the same real directory as the pull receipt.
clean_real="$root/clean-symlink-real"
clean_alias="$root/clean-symlink-alias"
mkdir -p "$clean_real"
ln -s "$clean_real" "$clean_alias"
old_cipher="$clean_real/weltgewebe-postgres-20200101T000000Z.sql.gz.age"
printf 'old encrypted fixture\n' > "$old_cipher"
touch -d '10 days ago' "$old_cipher"
LOCAL_RETENTION_DAYS=1 pull "$clean_alias" test-host > /dev/null
[[ -f "$clean_real/$file" && -f "$clean_real/latest-pull.receipt" ]]
[[ ! -e "$old_cipher" ]] || {
  echo 'symlinked encrypted destination skipped age retention' >&2
  exit 1
}

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
# Pulls with different destinations still serialize on a shared staging dir.
locked_stage="$root/locked-stage"
mkdir -m 700 "$locked_stage"
exec 6> "$locked_stage/.stage.lock"
flock -n 6
if PULL_MODE=migration-stage MIGRATION_STAGE_DIR="$locked_stage" \
  pull "$root/other-legacy-dest" test-host > /dev/null 2> "$root/locked-stage.stderr"; then
  echo 'a held staging lock must block a second migration staging' >&2
  exit 1
fi
grep -qF 'another migration staging holds' "$root/locked-stage.stderr"
exec 6>&-
[[ ! -e "$locked_stage/$file" ]]
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
PATH="$bin:$PATH" ALLOW_TEST_REMOTE_BACKUP_READER=1 REMOTE_HOST='' REMOTE_BACKUP_READER="$reader" DEST_DIR="$dest" \
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
# Byte-identical normal pulls are idempotent: do not replace immutable
# core artefacts, an existing proof, or any historical completion receipt.
mapfile -t history < <(find "$dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#history[@]} == 1))
cmp -s "${history[0]}" "$dest/latest-pull.receipt"
first_receipt="${history[0]}"
cp "$first_receipt" "$root/first-receipt"
cp "$dest/$file" "$root/first-ciphertext"
cp "$dest/$age_manifest" "$root/first-age-manifest"
cp "$dest/$manifest" "$root/first-metadata"
cp "$dest/$proof" "$root/first-proof"
pull "$dest" commonserver > /dev/null
for immutable in "$file" "$age_manifest" "$manifest" "$proof"; do
  [[ "$(stat -c %h "$dest/$immutable")" == 1 ]] || {
    echo "idempotent pull created an extra immutable link: $immutable" >&2
    exit 1
  }
done
mapfile -t history < <(find "$dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#history[@]} == 1))
cmp -s "$root/first-receipt" "$first_receipt"
cmp -s "$root/first-receipt" "$dest/latest-pull.receipt"

# A crash after publishing immutable history but before updating the mutable
# latest pointer must be recoverable by a byte-identical no-op rerun.
# Neither case creates a duplicate immutable completion receipt.
rm "$dest/latest-pull.receipt"
pull "$dest" commonserver > /dev/null
cmp -s "$root/first-receipt" "$dest/latest-pull.receipt" || {
  echo 'missing latest receipt was not repaired on idempotent replay' >&2
  exit 1
}
printf 'stale latest pointer from a previous completed backup\n' > "$dest/latest-pull.receipt"
pull "$dest" commonserver > /dev/null
cmp -s "$root/first-receipt" "$dest/latest-pull.receipt" || {
  echo 'stale latest receipt was not repaired on idempotent replay' >&2
  exit 1
}
mapfile -t history < <(find "$dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#history[@]} == 1))
cmp -s "$root/first-receipt" "${history[0]}"

# An equally named backup with a DIFFERENT, correctly manifest-bound
# ciphertext may not rewrite the historical ciphertext, metadata or proof.
# With real age this encrypts the same plaintext using a fresh ephemeral key.
cp "$remote/$file" "$root/remote-original-ciphertext"
encrypt_fixture "$remote/$plain" "$remote/$file"
if ((REAL_AGE == 0)); then
  # The stub has no entropy; force a changed age-shaped fixture.
  printf '\n' >> "$remote/$file"
fi
new_age_sha="$(sha256sum "$remote/$file" | awk '{print $1}')"
new_age_bytes="$(wc -c < "$remote/$file" | tr -d '[:space:]')"
write_age_manifest "$new_age_sha" "$new_age_bytes" "$plain_sha"
if pull "$dest" test-host > /dev/null 2> "$root/overwrite-normal.stderr"; then
  echo 'normal pull replaced a historically sealed encrypted backup' >&2
  exit 1
fi
grep -qF 'normal pull artefact content differs' "$root/overwrite-normal.stderr"
for binding in "$file" "$age_manifest" "$manifest" "$proof"; do
  case "$binding" in
    "$file") original="$root/first-ciphertext" ;;
    "$age_manifest") original="$root/first-age-manifest" ;;
    "$manifest") original="$root/first-metadata" ;;
    "$proof") original="$root/first-proof" ;;
  esac
  cmp -s "$original" "$dest/$binding" || {
    echo "historical off-host artefact was replaced: $binding" >&2
    exit 1
  }
done
mapfile -t history < <(find "$dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#history[@]} == 1))
cmp -s "$root/first-receipt" "$dest/latest-pull.receipt"
# Restore producer fixtures for subsequent offline-restore and negative tests.
cp "$root/remote-original-ciphertext" "$remote/$file"
write_age_manifest "$age_sha" "$age_bytes" "$plain_sha"
grep -qx 'remote_host=commonserver' "$dest/latest-pull.receipt"
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
# Every remote command is one reader operation; none names a file directly.
if grep -vE "^'$reader' (latest '[A-Za-z0-9._-]+'|read '[A-Za-z0-9._-]+' '(ciphertext|age-manifest|manifest|proof)'|proof-state '[A-Za-z0-9._-]+')\$" "$SSH_LOG"; then
  echo 'pull sent a remote command outside the reader interface' >&2
  exit 1
fi
if grep -F '.sql.gz' "$SSH_LOG"; then
  echo 'pull named a dump file over SSH' >&2
  exit 1
fi
grep -qxF "'$reader' read '$base' 'ciphertext'" "$SSH_LOG"

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

# A proof probe that fails for transport or reader reasons is not "no proof":
# the pull must fail instead of publishing a receipt without the evidence.
SIMULATE_PROOF_SSH_FAILURE=1 expect_pull_failure "$root/proof-ssh" 'could not probe the remote restore proof'
mv "$remote/proofs/$base.restore-proof" "$root/held-proof"
ln -s "$root/held-proof" "$remote/proofs/$base.restore-proof"
expect_pull_failure "$root/proof-symlink" 'could not probe the remote restore proof'
rm "$remote/proofs/$base.restore-proof"
mv "$root/held-proof" "$remote/proofs/$base.restore-proof"
# A definitely absent weekly proof is valid for a new daily backup.
# When the producer publishes it later, the identical immutable core is
# accepted and only the proof is added create-only, preserving old receipts.
mv "$remote/proofs/$base.restore-proof" "$root/held-proof"
no_proof_dest="$root/no-proof"
pull "$no_proof_dest" test-host > /dev/null
grep -qx 'result=ok' "$no_proof_dest/latest-pull.receipt"
[[ ! -e "$no_proof_dest/$base.restore-proof" ]]
mapfile -t no_proof_history < <(find "$no_proof_dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#no_proof_history[@]} == 1))
cp "${no_proof_history[0]}" "$root/no-proof-first-receipt"
for core in "$file" "$manifest" "$age_manifest"; do
  cp "$no_proof_dest/$core" "$root/early-$core"
done
mv "$root/held-proof" "$remote/proofs/$base.restore-proof"
pull "$no_proof_dest" test-host > /dev/null
cmp -s "$remote/proofs/$base.restore-proof" "$no_proof_dest/$proof"
cmp -s "$root/no-proof-first-receipt" "${no_proof_history[0]}"
for core in "$file" "$manifest" "$age_manifest"; do
  cmp -s "$root/early-$core" "$no_proof_dest/$core" || {
    echo "late proof replaced immutable core: $core" >&2
    exit 1
  }
done
mapfile -t no_proof_history < <(find "$no_proof_dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#no_proof_history[@]} == 2))
cp "$no_proof_dest/$proof" "$root/late-proof-saved"
pull "$no_proof_dest" test-host > /dev/null
mapfile -t no_proof_history < <(find "$no_proof_dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#no_proof_history[@]} == 2))
cmp -s "$root/late-proof-saved" "$no_proof_dest/$proof"

# With two immutable receipts, recovery must restore the newest receipt
# (the one reflecting late-proof publication), not the original daily pull.
mapfile -t no_proof_history < <(find "$no_proof_dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#no_proof_history[@]} == 2))
latest_history="$(printf '%s\n' "${no_proof_history[@]}" | LC_ALL=C sort | tail -n 1)"
rm "$no_proof_dest/latest-pull.receipt"
pull "$no_proof_dest" test-host > /dev/null
cmp -s "$latest_history" "$no_proof_dest/latest-pull.receipt" || {
  echo 'late-proof history was not restored as latest receipt' >&2
  exit 1
}
mapfile -t no_proof_history < <(find "$no_proof_dest/receipts" -type f -name "$base.pulled-*.receipt")
((${#no_proof_history[@]} == 2))

# A symlink must not be followed or silently accepted for the mutable pointer.
rm "$no_proof_dest/latest-pull.receipt"
ln -s "$root/no-proof-first-receipt" "$no_proof_dest/latest-pull.receipt"
if pull "$no_proof_dest" test-host > /dev/null 2> "$root/unsafe-latest.stderr"; then
  echo 'symlinked latest receipt was accepted' >&2
  exit 1
fi
grep -qF 'normal pull unsafe latest receipt pointer' "$root/unsafe-latest.stderr"
rm "$no_proof_dest/latest-pull.receipt"
pull "$no_proof_dest" test-host > /dev/null
cmp -s "$latest_history" "$no_proof_dest/latest-pull.receipt"

# A changed remote proof must never replace the historical local proof.
printf 'contract=weltgewebe-postgres-restore-proof-v1\nresult=ok\nnote=changed\n' > "$remote/proofs/$base.restore-proof"
if pull "$no_proof_dest" test-host > /dev/null 2> "$root/proof-changed.stderr"; then
  echo 'a changed restore proof replaced immutable history' >&2
  exit 1
fi
grep -qF 'normal pull artefact content differs' "$root/proof-changed.stderr"
cmp -s "$root/late-proof-saved" "$no_proof_dest/$proof"
printf 'contract=weltgewebe-postgres-restore-proof-v1\nresult=ok\n' > "$remote/proofs/$base.restore-proof"

# Partial core sets and pre-existing symlinks are not an idempotent pull.
partial_dest="$root/partial-core"
mkdir -p "$partial_dest"
cp "$remote/$file" "$partial_dest/$file"
if pull "$partial_dest" test-host > /dev/null 2> "$root/partial-core.stderr"; then
  echo 'partial core set was silently accepted' >&2
  exit 1
fi
grep -qF 'normal pull incomplete core artefact set' "$root/partial-core.stderr"
[[ ! -e "$partial_dest/$manifest" && ! -e "$partial_dest/latest-pull.receipt" ]]
unsafe_dest="$root/unsafe-proof"
mkdir -p "$unsafe_dest"
ln -s "$remote/proofs/$base.restore-proof" "$unsafe_dest/$proof"
if pull "$unsafe_dest" test-host > /dev/null 2> "$root/unsafe-proof.stderr"; then
  echo 'symlinked local proof was accepted' >&2
  exit 1
fi
grep -qF 'normal pull unsafe existing artefact' "$root/unsafe-proof.stderr"
[[ ! -e "$unsafe_dest/$file" && ! -e "$unsafe_dest/latest-pull.receipt" ]]
unsafe_core="$root/unsafe-core"
mkdir -p "$unsafe_core"
ln -s "$remote/$file" "$unsafe_core/$file"
if pull "$unsafe_core" test-host > /dev/null 2> "$root/unsafe-core.stderr"; then
  echo 'symlinked local ciphertext was accepted' >&2
  exit 1
fi
grep -qF 'normal pull unsafe existing artefact' "$root/unsafe-core.stderr"
[[ ! -e "$unsafe_core/$manifest" && ! -e "$unsafe_core/latest-pull.receipt" ]]

# A complete old core without an immutable receipt is not an authenticated
# recovery point. A symlink or forged receipt cannot make it one.
for scenario in absent symlink forged receipt-only; do
  history_dest="$root/history-$scenario"
  mkdir -p "$history_dest/receipts"
  if [[ "$scenario" != receipt-only ]]; then
    for core in "$file" "$manifest" "$age_manifest"; do
      cp "$remote/$core" "$history_dest/$core"
    done
  fi
  history_path="$history_dest/receipts/$base.pulled-20260712T040000.000000000Z.receipt"
  case "$scenario" in
    absent) expected_error='normal pull existing core has no historical completion receipt' ;;
    symlink)
      ln -s "$root/first-receipt" "$history_path"
      expected_error='normal pull unsafe existing history receipt'
      ;;
    forged)
      printf 'contract=weltgewebe-offhost-backup-pull-v2\nresult=ok\n' > "$history_path"
      expected_error='normal pull existing history receipt differs'
      ;;
    receipt-only)
      cp "$root/first-receipt" "$history_path"
      expected_error='normal pull historical receipt exists without core artefacts'
      ;;
  esac
  if pull "$history_dest" commonserver > /dev/null 2> "$root/history-$scenario.stderr"; then
    echo "inconsistent historical backup was accepted: $scenario" >&2
    exit 1
  fi
  grep -qF "$expected_error" "$root/history-$scenario.stderr"
  [[ ! -e "$history_dest/latest-pull.receipt" ]]
done

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
# The reader is the only privileged interface, so its argument and file-type
# boundary is tested directly: a sudoers rule naming it permits any arguments.
make_reader() {
  sed -e "s|^readonly BACKUP_DIR=.*|readonly BACKUP_DIR=$1|" \
    -e "s|^readonly BACKUP_OWNER_UID=.*|readonly BACKUP_OWNER_UID=$2|" \
    "$READER_SOURCE" > "$3"
  chmod 755 "$3"
}
rdir="$root/reader-remote"
mkdir -p "$rdir/proofs"
chmod 700 "$rdir" "$rdir/proofs"
rbase=weltgewebe-postgres-20260801T040000Z
printf '%s\n' "$marker" | gzip -c > "$rdir/$rbase.sql.gz"
printf 'age-encryption.org/v1\ncipher\n' > "$rdir/$rbase.sql.gz.age"
printf 'contract=weltgewebe-postgres-backup-age-v1\n' > "$rdir/$rbase.age.sha256.manifest"
printf 'contract=weltgewebe-postgres-backup-v1\n' > "$rdir/$rbase.sha256.manifest"
printf 'result=ok\n' > "$rdir/proofs/$rbase.restore-proof"
rreader="$root/reader-bin"
make_reader "$rdir" "$(id -u)" "$rreader"
expect_reader_failure() {
  local message="$1"
  shift
  if timeout 10 "$rreader" "$@" > "$root/reader.stdout" 2> "$root/reader.stderr"; then
    echo "reader accepted: $* ($message)" >&2
    exit 1
  fi
  grep -qF "postgres-backup-read: $message" "$root/reader.stderr" || {
    echo "unexpected reader failure for: $* (wanted: $message)" >&2
    cat "$root/reader.stderr" >&2
    exit 1
  }
  if grep -qF "$marker" "$root/reader.stdout"; then
    echo "reader leaked plaintext for: $*" >&2
    exit 1
  fi
}
[[ "$("$rreader" latest weltgewebe-postgres)" == "$rbase.age.sha256.manifest" ]]
# The caller's environment must not choose code that runs as root: no PATH
# lookup for the interpreter, no BASH_ENV, no imported functions.
[[ "$(head -n1 "$READER_SOURCE")" == '#!/bin/bash -p' ]]
mkdir -p "$root/evil-bin"
printf '#!/bin/sh\necho evil-bash-ran\n' > "$root/evil-bin/bash"
printf 'echo evil-bash-env-ran\n' > "$root/evil-bash-env"
chmod 755 "$root/evil-bin/bash"
evil_out="$(env PATH="$root/evil-bin:$PATH" BASH_ENV="$root/evil-bash-env" \
  'BASH_FUNC_find%%=() { echo evil-function-ran; }' \
  "$rreader" latest weltgewebe-postgres 2>&1)"
[[ "$evil_out" == "$rbase.age.sha256.manifest" ]] || {
  printf 'reader honoured caller-controlled code: %s\n' "$evil_out" >&2
  exit 1
}
cmp -s <("$rreader" read "$rbase" ciphertext) "$rdir/$rbase.sql.gz.age"
cmp -s <("$rreader" read "$rbase" age-manifest) "$rdir/$rbase.age.sha256.manifest"
cmp -s <("$rreader" read "$rbase" manifest) "$rdir/$rbase.sha256.manifest"
cmp -s <("$rreader" read "$rbase" proof) "$rdir/proofs/$rbase.restore-proof"
[[ "$("$rreader" proof-state "$rbase")" == present ]]
[[ "$("$rreader" proof-state weltgewebe-postgres-20990101T000000Z)" == absent ]]
expect_reader_failure 'usage:'
expect_reader_failure 'unsupported operation' cat "$rdir/$rbase.sql.gz"
expect_reader_failure 'unsupported operation' find "$rdir"
expect_reader_failure 'latest takes exactly one PREFIX' latest
expect_reader_failure 'latest takes exactly one PREFIX' latest weltgewebe-postgres extra
expect_reader_failure 'invalid prefix' latest '*'
expect_reader_failure 'invalid prefix' latest '-name'
expect_reader_failure 'invalid prefix' latest '../postgres'
expect_reader_failure 'read takes exactly BASE and KIND' read "$rbase"
expect_reader_failure 'read takes exactly BASE and KIND' read "$rbase" ciphertext extra
expect_reader_failure 'unsupported kind' read "$rbase" plaintext
expect_reader_failure 'unsupported kind' read "$rbase" ''
expect_reader_failure 'invalid backup name' read "../$rbase" ciphertext
expect_reader_failure 'invalid backup name' read "proofs/$rbase" proof
expect_reader_failure 'invalid backup name' read "$rbase x" ciphertext
expect_reader_failure 'invalid backup name' read "-$rbase" ciphertext
expect_reader_failure 'invalid backup name' read "$rbase..x" ciphertext
expect_reader_failure 'invalid backup name' read '' ciphertext
expect_reader_failure 'invalid backup name' proof-state "../$rbase"
expect_reader_failure 'proof-state takes exactly one BASE' proof-state
# Appending a fixed suffix means the plaintext dump has no name at all.
expect_reader_failure 'not a regular file' read "$rbase.sql.gz" manifest
# Symlinks and special files are refused, never followed or read.
ln -s "$rdir/$rbase.sql.gz" "$rdir/linked.sql.gz.age"
expect_reader_failure 'not a regular file' read linked ciphertext
mkfifo "$rdir/fifo.sha256.manifest"
expect_reader_failure 'not a regular file' read fifo manifest
ln -s "$rdir/$rbase.sql.gz" "$rdir/proofs/linked.restore-proof"
expect_reader_failure 'restore proof is not a regular file' proof-state linked
expect_reader_failure 'not a regular file' read linked proof
# A missing proofs directory means no proof; an untrusted one is refused.
mv "$rdir/proofs" "$root/held-proofs"
[[ "$("$rreader" proof-state "$rbase")" == absent ]]
mv "$root/held-proofs" "$rdir/proofs"
chmod 770 "$rdir/proofs"
expect_reader_failure 'group or world writable' proof-state "$rbase"
expect_reader_failure 'group or world writable' read "$rbase" proof
chmod 700 "$rdir/proofs"
# The type check above cannot be raced deterministically here; the open itself
# must also refuse a symlink swapped in after the check.
# shellcheck disable=SC2016 # literal source text, not an expansion
grep -qF 'dd if="$path" iflag=nofollow' "$READER_SOURCE"
# The backup directory itself must be a real, owner-only directory.
chmod 750 "$rdir"
"$rreader" latest weltgewebe-postgres > /dev/null
chmod 720 "$rdir"
expect_reader_failure 'group or world writable' latest weltgewebe-postgres
chmod 700 "$rdir"
ln -s "$rdir" "$root/reader-remote-alias"
make_reader "$root/reader-remote-alias" "$(id -u)" "$rreader"
expect_reader_failure 'not a directory' latest weltgewebe-postgres
make_reader "$rdir" "$(($(id -u) + 1))" "$rreader"
expect_reader_failure 'unexpected owner' read "$rbase" ciphertext
make_reader "$rdir" "$(id -u)" "$rreader"
"$rreader" latest weltgewebe-postgres > /dev/null

# The Restic snapshot gate refuses a snapshot while plaintext dumps lie below
# any source, and fails closed when a source cannot be scanned.
gate_home="$root/gate-home"
mkdir -p "$gate_home/repos/project" "$gate_home/artifacts/merges" "$gate_home/fundus" "$gate_home/elsewhere"
printf 'not a dump\n' > "$gate_home/repos/project/notes.sql.gz.age"
gate_sources=("$gate_home/repos" "$gate_home/artifacts/merges" "$gate_home/fundus")
"$GATE" "${gate_sources[@]}" > "$root/gate.stdout"
grep -qF 'no plaintext PostgreSQL dumps below 3 source(s)' "$root/gate.stdout"
expect_gate_failure() {
  local message="$1"
  shift
  if "$GATE" "$@" > /dev/null 2> "$root/gate.stderr"; then
    echo "Restic gate accepted: $* ($message)" >&2
    exit 1
  fi
  grep -qF "restic-plaintext-gate: $message" "$root/gate.stderr" || {
    echo "unexpected Restic gate failure for: $* (wanted: $message)" >&2
    cat "$root/gate.stderr" >&2
    exit 1
  }
}
expect_gate_failure 'usage:'
expect_gate_failure 'source must be an absolute path' repos
expect_gate_failure 'source does not exist' "$gate_home/missing"
mkdir -p "$gate_home/artifacts/merges/weltgewebe-production-backups/.pull.Ab12Cd"
printf 'old private pg dump\n' > "$gate_home/artifacts/merges/weltgewebe-production-backups/.pull.Ab12Cd/weltgewebe-postgres-legacy.sql.gz"
expect_gate_failure "plaintext PostgreSQL dump below snapshot source $gate_home/artifacts/merges" "${gate_sources[@]}"
rm -r "$gate_home/artifacts/merges/weltgewebe-production-backups"
"$GATE" "${gate_sources[@]}" > /dev/null
# A dump reached through a symlinked source root, or itself a symlink, counts.
printf 'old private pg dump\n' > "$gate_home/elsewhere/weltgewebe-postgres-legacy.sql.gz"
ln -s "$gate_home/elsewhere" "$gate_home/fundus-alias"
expect_gate_failure 'plaintext PostgreSQL dump below snapshot source' "$gate_home/fundus-alias"
ln -s "$gate_home/elsewhere/weltgewebe-postgres-legacy.sql.gz" "$gate_home/fundus/linked.sql.gz"
expect_gate_failure 'plaintext PostgreSQL dump below snapshot source' "${gate_sources[@]}"
rm "$gate_home/fundus/linked.sql.gz"
# An unreadable subtree is a partial scan, never a pass (root reads everything).
if (($(id -u) != 0)); then
  mkdir -m 000 "$gate_home/repos/locked"
  expect_gate_failure 'could not scan source completely' "${gate_sources[@]}"
  chmod 700 "$gate_home/repos/locked"
fi
"$GATE" "${gate_sources[@]}" > /dev/null

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
cp "$REPO_ROOT/scripts/ops/pull-production-postgres-backup.sh" "$GATE" "$fixture_repo/scripts/ops/"
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
installed_gate="$install_home/.local/libexec/weltgewebe/$commit/restic-plaintext-gate.sh"
[[ -x "$installed_gate" ]]
cmp -s "$GATE" "$installed_gate"
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
grep -qxF "restic_gate_sha256=$(sha256sum "$GATE" | awk '{print $1}')" "$receipt"
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
