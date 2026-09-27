#!/usr/bin/env bash
# shellcheck disable=SC2012
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
SCRIPT_SOURCE="$REPO_ROOT/scripts/weltgewebe-up"
REAL_GIT="$(command -v git)"

# Synthetic VPS fixtures must not depend on the host network or resolver file.
export CADDY_BIND=203.0.113.10
export CADDY_IPV6_BIND='[2001:db8::10]'

WORKDIR_ROOT="$(mktemp -d)"
EDGE_CA_FIXTURE="$WORKDIR_ROOT/edge-ca.crt"
printf 'test-ca\n' > "$EDGE_CA_FIXTURE"
cleanup() {
  rm -rf "$WORKDIR_ROOT"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  if ! grep -Fq "$needle" <<< "$haystack"; then
    fail "expected output to contain: $needle"
  fi
}

new_repo() {
  local name="$1"
  local root="$WORKDIR_ROOT/$name"
  local origin="$root/origin.git"
  local repo="$root/repo"

  mkdir -p "$root"
  git init --bare "$origin" > /dev/null
  git -C "$origin" symbolic-ref HEAD refs/heads/main
  git clone "$origin" "$repo" > /dev/null 2>&1

  (
    cd "$repo"
    git config user.name "Weltgewebe Test"
    git config user.email "tests@weltgewebe.local"

    mkdir -p infra/compose scripts apps/web/build/_app
    cp "$SCRIPT_SOURCE" scripts/weltgewebe-up
    chmod +x scripts/weltgewebe-up

    cat > .env << 'EOF'
WEB_UPSTREAM_URL=https://example.com
WEB_UPSTREAM_HOST=example.com
EOF

    cat > infra/compose/compose.prod.yml << 'EOF'
services:
  api:
    image: busybox
EOF

    cat > infra/compose/compose.prod.override.yml << 'EOF'
services:
  api:
    image: busybox
EOF

    cat > infra/compose/compose.vps.override.yml << 'EOF'
services:
  api:
    image: busybox
  caddy:
    image: busybox
EOF

    cat > apps/web/build/index.html << 'EOF'
<!doctype html><html><body><script src="/_app/immutable/test.js"></script></body></html>
EOF

    cat > apps/web/build/_app/version.json << 'EOF'
{"version":"test-build"}
EOF

    git add .
    git commit -m "test: initial main" > /dev/null
    git branch -M main
    git -c core.hooksPath=/dev/null push -u origin HEAD:refs/heads/main > /dev/null

    git checkout -b feat/x > /dev/null
    echo "feature" > feature.txt
    git add feature.txt
    git commit -m "feat: x" > /dev/null
    git -c core.hooksPath=/dev/null push -u origin feat/x > /dev/null
  )

  echo "$repo"
}

advance_remote_branch() {
  local origin="$1"
  local branch="$2"
  local tmp_clone
  tmp_clone="$(mktemp -d "$WORKDIR_ROOT/tmp-remote-XXXXXX")"
  git clone "$origin" "$tmp_clone" > /dev/null 2>&1
  (
    cd "$tmp_clone"
    git config user.name "Weltgewebe Test"
    git config user.email "tests@weltgewebe.local"
    git checkout "$branch" > /dev/null 2>&1 || git checkout -b "$branch" "origin/$branch" > /dev/null
    printf '%s\n' "remote-${RANDOM}" >> remote.txt
    git add remote.txt
    git commit -m "chore: advance $branch" > /dev/null
    git -c core.hooksPath=/dev/null push origin "$branch" > /dev/null
  )
  rm -rf "$tmp_clone"
}

setup_mocks() {
  local repo="$1"
  local mock_root
  mock_root="$(mktemp -d "$WORKDIR_ROOT/mocks-XXXXXX")"
  local mock_bin="$mock_root/mock-bin"
  local wrapper_bin="$mock_root/wrapper-bin"

  mkdir -p "$mock_bin" "$wrapper_bin"

  cat > "$mock_bin/docker" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
ARGS="$*"
if [[ "$1" == "ps" ]]; then
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  if [[ "$ARGS" == *"Aliases"* ]]; then
    echo "weltgewebe-api"
    exit 0
  fi
  if [[ "$ARGS" == *".State.Health"* && "$ARGS" == *"yes"* && "$ARGS" == *"no"* ]]; then
    echo "yes"
    exit 0
  fi
  if [[ "$ARGS" == *".State.Health.Status"* ]]; then
    echo "healthy"
    exit 0
  fi
  if [[ "$ARGS" == *".State.Health.Log"* ]]; then
    echo '[{"ExitCode":0,"Output":"ok"}]'
    exit 0
  fi
  echo "{}"
  exit 0
fi
if [[ "$1" == "compose" ]]; then
  if [[ "$ARGS" == *" config --services"* ]]; then
    printf '%s\n' api db nats caddy
    exit 0
  fi
  if [[ "$ARGS" == *" config --format json"* ]]; then
    if [[ "${MOCK_FAIL_CONFIG_GUARD:-0}" == "1" ]]; then
      echo "{"
    else
      echo '{"services":{"api":{"ports":[],"networks":{"default":{"aliases":["weltgewebe-api"]}}},"db":{},"nats":{},"caddy":{}}}'
    fi
    exit 0
  fi
  if [[ "$ARGS" == *" config"* ]]; then
    echo "services: {}"
    exit 0
  fi
  if [[ "$ARGS" == *" ps -q api"* ]]; then
    echo "api_container_id"
    exit 0
  fi
  if [[ "$ARGS" == *" ps -q db"* ]]; then
    echo "db_container_id"
    exit 0
  fi
  if [[ "$ARGS" == *" ps -q nats"* ]]; then
    echo "nats_container_id"
    exit 0
  fi
  if [[ "$ARGS" == *" ps -q caddy"* ]]; then
    echo "caddy_container_id"
    exit 0
  fi
  if [[ "$ARGS" == *" ps --format"* ]]; then
    echo "weltgewebe-api-1 running healthy"
    exit 0
  fi
  if [[ "$ARGS" == *" port api"* ]]; then
    echo "0.0.0.0:18080"
    exit 0
  fi
  if [[ "$ARGS" == *" exec -T api wget -qO- http://localhost:8080/health/ready"* ]]; then
    echo '{"status":"ok"}'
    exit 0
  fi
  if [[ "$ARGS" == *" up -d"* ]]; then
    if [[ "${MOCK_UP_FAIL:-0}" == "1" ]]; then
      exit 1
    fi
    exit 0
  fi
  exit 0
fi
exit 0
EOF

  cat > "$mock_bin/curl" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
ARGS="$*"
if [[ "$ARGS" == *"-w"* ]]; then
  echo "200"
  exit 0
fi
if [[ "$ARGS" == *"-I"* || "$ARGS" == *"-SI"* ]]; then
  printf 'HTTP/2 200\r\ncache-control: no-store\r\nx-weltgewebe-build: test-build\r\n\r\n'
  exit 0
fi
if [[ "$ARGS" == *"/_app/version.json"* ]]; then
  echo '{"version":"test-build","build_id":"test-build"}'
  exit 0
fi
echo '{"status":"ok"}'
exit 0
EOF

  cat > "$mock_bin/pnpm" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$mock_bin/getent" << 'EOF'
#!/usr/bin/env bash
echo "127.0.0.1 localhost"
exit 0
EOF

  cat > "$wrapper_bin/git" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${GIT_LOG_FILE:-}" ]]; then
  printf '%s\n' "$*" >> "$GIT_LOG_FILE"
fi
exec "$REAL_GIT" "$@"
EOF

  chmod +x "$mock_bin/docker" "$mock_bin/curl" "$mock_bin/pnpm" "$mock_bin/getent" "$wrapper_bin/git"

  echo "$mock_bin|$wrapper_bin"
}

run_up() {
  local repo="$1"
  local git_log_file="$2"
  shift 2

  local bins
  bins="$(setup_mocks "$repo")"
  local mock_bin="${bins%%|*}"
  local wrapper_bin="${bins##*|}"

  : > "$git_log_file"

  (
    cd "$repo"
    PATH="$wrapper_bin:$mock_bin:$PATH" \
      REAL_GIT="$REAL_GIT" \
      GIT_LOG_FILE="$git_log_file" \
      REPO_DIR="$repo" \
      ENV_FILE="$repo/.env" \
      EDGE_CA="$EDGE_CA_FIXTURE" \
      DEPLOY_FRONTEND_MODE=off \
      WELTGEWEBE_DEPLOY_SCOPE="${WELTGEWEBE_DEPLOY_SCOPE:-api}" \
      WELTGEWEBE_STATE_DIR="$repo/.ops" \
      bash scripts/weltgewebe-up "$@"
  )
}

# 1) Default behavior: start on feature branch, switch to/pull main
repo_default="$(new_repo default-main)"
(
  cd "$repo_default"
  git checkout feat/x > /dev/null
)
if ! out_default="$(run_up "$repo_default" "$WORKDIR_ROOT/default-main.git.log" 2>&1)"; then
  echo "$out_default" >&2
  fail "default deploy should succeed"
fi
(
  cd "$repo_default"
  current_branch="$(git symbolic-ref --short HEAD)"
  [[ "$current_branch" == "main" ]] || fail "default deploy must end on main, got $current_branch"
)
assert_contains "$out_default" "Deploy branch: main"
assert_contains "$out_default" "Branch switch: yes"
assert_contains "$(cat "$WORKDIR_ROOT/default-main.git.log")" "pull --ff-only origin main"

echo "PASS: default deploy switches to main"

# 2) Explicit branch: --branch feat/x pulls feat/x
repo_explicit="$(new_repo explicit-branch)"
out_explicit="$(run_up "$repo_explicit" "$WORKDIR_ROOT/explicit-branch.git.log" --branch feat/x 2>&1)" || fail "explicit branch deploy should succeed"
(
  cd "$repo_explicit"
  current_branch="$(git symbolic-ref --short HEAD)"
  [[ "$current_branch" == "feat/x" ]] || fail "--branch feat/x should end on feat/x, got $current_branch"
)
assert_contains "$out_explicit" "Deploy branch: feat/x"
assert_contains "$(cat "$WORKDIR_ROOT/explicit-branch.git.log")" "pull --ff-only origin feat/x"

echo "PASS: explicit --branch works"

# 3) Explicit current branch: pulls attached current branch
repo_current="$(new_repo current-branch)"
(
  cd "$repo_current"
  git checkout feat/x > /dev/null
)
out_current="$(run_up "$repo_current" "$WORKDIR_ROOT/current-branch.git.log" --current-branch 2>&1)" || fail "--current-branch deploy should succeed"
assert_contains "$out_current" "Git Branch Mode: current-branch (explicit)"
assert_contains "$out_current" "Deploy branch: feat/x"
assert_contains "$out_current" "Branch switch: no"

echo "PASS: --current-branch works"

# 4) Invalid branch name is rejected early
repo_invalid_branch="$(new_repo invalid-branch)"
set +e
out_invalid_branch="$(run_up "$repo_invalid_branch" "$WORKDIR_ROOT/invalid-branch.git.log" --branch "origin/main" 2>&1)"
rc_invalid_branch=$?
set -e
[[ "$rc_invalid_branch" -ne 0 ]] || fail "invalid --branch value should fail"
assert_contains "$out_invalid_branch" "ERROR: Invalid deploy branch name: origin/main"
if grep -Fq "switch" "$WORKDIR_ROOT/invalid-branch.git.log"; then
  fail "invalid --branch must fail before any branch switch"
fi

echo "PASS: invalid --branch is rejected"

# 4b) Invalid remote-qualified branch is rejected even if REMOTE differs
repo_invalid_branch_upstream="$(new_repo invalid-branch-upstream)"
(
  cd "$repo_invalid_branch_upstream"
  git remote add upstream "$WORKDIR_ROOT/invalid-branch-upstream/origin.git"
)
set +e
out_invalid_branch_upstream="$(REMOTE=upstream run_up "$repo_invalid_branch_upstream" "$WORKDIR_ROOT/invalid-branch-upstream.git.log" --branch "origin/main" 2>&1)"
rc_invalid_branch_upstream=$?
set -e
[[ "$rc_invalid_branch_upstream" -ne 0 ]] || fail "origin/main must fail even when REMOTE=upstream"
assert_contains "$out_invalid_branch_upstream" "ERROR: Invalid deploy branch name: origin/main"
if grep -Fq "switch" "$WORKDIR_ROOT/invalid-branch-upstream.git.log"; then
  fail "invalid remote-qualified branch must fail before any branch switch"
fi

echo "PASS: origin/main rejected with REMOTE=upstream"

# 4c) refs/* branch path is rejected
set +e
out_invalid_refs="$(run_up "$repo_invalid_branch" "$WORKDIR_ROOT/invalid-refs.git.log" --branch "refs/heads/main" 2>&1)"
rc_invalid_refs=$?
set -e
[[ "$rc_invalid_refs" -ne 0 ]] || fail "refs/heads/main should fail"
assert_contains "$out_invalid_refs" "ERROR: Invalid deploy branch name: refs/heads/main"

echo "PASS: refs/* branch path is rejected"

# 5) Dirty worktree: abort before branch switch
repo_dirty="$(new_repo dirty-worktree)"
(
  cd "$repo_dirty"
  git checkout feat/x > /dev/null
  echo "dirty" > dirty.txt
)
set +e
out_dirty="$(run_up "$repo_dirty" "$WORKDIR_ROOT/dirty-worktree.git.log" 2>&1)"
rc_dirty=$?
set -e
[[ "$rc_dirty" -ne 0 ]] || fail "dirty worktree run should fail"
assert_contains "$out_dirty" "Dirty worktree detected"
(
  cd "$repo_dirty"
  current_branch="$(git symbolic-ref --short HEAD)"
  [[ "$current_branch" == "feat/x" ]] || fail "dirty worktree should not switch branch"
)

echo "PASS: dirty worktree blocks deploy"

# 6) Detached HEAD + clean: switch to default main
repo_detached="$(new_repo detached-head)"
(
  cd "$repo_detached"
  git checkout feat/x > /dev/null
  git checkout --detach HEAD > /dev/null
)
out_detached="$(run_up "$repo_detached" "$WORKDIR_ROOT/detached-head.git.log" 2>&1)" || fail "detached-head deploy should succeed"
(
  cd "$repo_detached"
  current_branch="$(git symbolic-ref --short HEAD)"
  [[ "$current_branch" == "main" ]] || fail "detached-head default should end on main"
)
assert_contains "$out_detached" "Branch before deploy: DETACHED"
assert_contains "$out_detached" "Current branch: main"
assert_contains "$out_detached" "Deploy branch: main"

echo "PASS: detached head resolves via deploy-branch contract"

# 7) --no-pull: no git sync/branch operations (fetch/switch/pull)
repo_no_pull="$(new_repo no-pull)"
(
  cd "$repo_no_pull"
  git checkout feat/x > /dev/null
)
out_no_pull="$(run_up "$repo_no_pull" "$WORKDIR_ROOT/no-pull.git.log" --no-pull 2>&1)" || fail "--no-pull run should succeed"
assert_contains "$out_no_pull" "Git mode: no-pull"
assert_contains "$out_no_pull" "Deploy mode: existing checkout (--no-pull; branch contract not applied)"
assert_contains "$out_no_pull" "Branch switch: no"
if grep -Eq '(^| )fetch( |$)|(^| )switch( |$)|(^| )pull( |$)' "$WORKDIR_ROOT/no-pull.git.log"; then
  fail "--no-pull must not execute fetch/switch/pull"
fi
(
  cd "$repo_no_pull"
  current_branch="$(git symbolic-ref --short HEAD)"
  [[ "$current_branch" == "feat/x" ]] || fail "--no-pull should keep current branch"
)

echo "PASS: --no-pull skips git sync/branch operations"

# 7b) --no-pull and --current-branch are mutually exclusive
repo_no_pull_current="$(new_repo no-pull-current-branch)"
set +e
out_no_pull_current="$(run_up "$repo_no_pull_current" "$WORKDIR_ROOT/no-pull-current-branch.git.log" --no-pull --current-branch 2>&1)"
rc_no_pull_current=$?
set -e
[[ "$rc_no_pull_current" -ne 0 ]] || fail "--no-pull --current-branch should fail"
assert_contains "$out_no_pull_current" "ERROR: --current-branch cannot be combined with --no-pull."
if grep -Eq '(^| )fetch( |$)|(^| )switch( |$)|(^| )pull( |$)' "$WORKDIR_ROOT/no-pull-current-branch.git.log"; then
  fail "--no-pull --current-branch must not execute fetch/switch/pull"
fi

echo "PASS: --no-pull rejects --current-branch"

# 8) Non-fast-forward pull fails hard
repo_nonff="$(new_repo non-ff)"
origin_nonff="$WORKDIR_ROOT/non-ff/origin.git"
(
  cd "$repo_nonff"
  git checkout main > /dev/null
  echo "local-only" > local-diverge.txt
  git add local-diverge.txt
  git commit -m "local diverge" > /dev/null
)
advance_remote_branch "$origin_nonff" main
(
  cd "$repo_nonff"
  git checkout feat/x > /dev/null
)
set +e
out_nonff="$(run_up "$repo_nonff" "$WORKDIR_ROOT/non-ff.git.log" 2>&1)"
rc_nonff=$?
set -e
[[ "$rc_nonff" -ne 0 ]] || fail "non-fast-forward case must fail"
assert_contains "$out_nonff" "Git pull failed (fast-forward only)"

echo "PASS: non-fast-forward pull aborts"

# 9) Legacy heimserver failure bundle contains deploy-branch metadata fields
repo_bundle="$(new_repo failure-bundle)"
(
  cd "$repo_bundle"
  git checkout feat/x > /dev/null
)
set +e
out_bundle="$(DEPLOY_TARGET=heimserver WELTGEWEBE_DEPLOY_SCOPE=full MOCK_FAIL_CONFIG_GUARD=1 run_up "$repo_bundle" "$WORKDIR_ROOT/failure-bundle.git.log" 2>&1)"
rc_bundle=$?
set -e
[[ "$rc_bundle" -ne 0 ]] || fail "failure-bundle case must fail"
assert_contains "$out_bundle" "ERROR: Deploy failed."

latest_bundle="$(ls -1dt "$repo_bundle"/.ops/failures/* 2> /dev/null | head -n1 || true)"
[[ -n "$latest_bundle" ]] || fail "expected a failure bundle directory"

bundle_state="$(cat "$latest_bundle/git_state.txt")"
assert_contains "$bundle_state" "deploy branch:"
assert_contains "$bundle_state" "current branch before:"
assert_contains "$bundle_state" "current branch after:"
assert_contains "$bundle_state" "head before:"
assert_contains "$bundle_state" "head after:"
assert_contains "$bundle_state" "remote deploy branch:"
assert_contains "$bundle_state" "branch switch attempted:"
assert_contains "$bundle_state" "branch switch result:"

echo "PASS: legacy heimserver failure bundle contains deploy branch metadata"

echo "test_weltgewebe_up_git_branch: OK"
