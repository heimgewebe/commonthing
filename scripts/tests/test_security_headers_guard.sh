#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
GUARD="$REPO_ROOT/scripts/guard/security-headers-guard.sh"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
mkdir -p "$TEMP_DIR/policies" "$TEMP_DIR/infra/caddy" "$TEMP_DIR/apps/web"
mkdir -p "$TEMP_DIR/apps/api/src/routes"

POLICY_SOURCE="$REPO_ROOT/policies/security.yml"
reset_policy() {
  cp "$POLICY_SOURCE" "$TEMP_DIR/policies/security.yml"
}
reset_policy

# The guard derives the magic-link style hash from this source, so the fixture
# carries the real file rather than a stand-in that could drift from it.
AUTH_SOURCE="$REPO_ROOT/apps/api/src/routes/auth.rs"
reset_auth_source() {
  cp "$AUTH_SOURCE" "$TEMP_DIR/apps/api/src/routes/auth.rs"
}
reset_auth_source

magic_policy() {
  uv run --project "$REPO_ROOT/tools/py" --locked python - \
    "$TEMP_DIR/apps/api/src/routes/auth.rs" << 'PY'
import base64
import hashlib
import re
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
matches = re.findall(r'const\s+MAGIC_LINK_CONFIRM_STYLE:\s*&str\s*=\s*"([^"\\]*)"\s*;', source)
if len(matches) != 1:
    raise SystemExit(f"expected one MAGIC_LINK_CONFIRM_STYLE literal, found {len(matches)}")
digest = base64.b64encode(hashlib.sha256(matches[0].encode("utf-8")).digest()).decode("ascii")
print(
    f"default-src 'none'; style-src 'sha256-{digest}'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none';"
)
PY
}

cat > "$TEMP_DIR/apps/web/svelte.config.js" << 'JS'
export default {
  kit: {
    csp: {
      mode: "hash",
      directives: {
        "script-src": ["self"],
      },
    },
  },
};
JS

write_static_caddy() {
  local file="$1"
  local connect="$2"
  local strict_matcher="apiResponse"
  local strict_paths="/api/*"
  if [[ "$(basename "$file")" == "Caddyfile.vps" ]]; then
    strict_matcher="nonDocumentResponse"
    strict_paths="/api/* /health/*"
  fi
  cat > "$file" << CADDY
example.test {
  @magicLinkConfirm {
    method GET
    path /api/auth/magic-link/consume
  }
  header @magicLinkConfirm >Content-Security-Policy "$(magic_policy)"
  @${strict_matcher} {
    path ${strict_paths}
    not {
      method GET
      path /api/auth/magic-link/consume
    }
  }
  header @${strict_matcher} >Content-Security-Policy "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';"
  @frontendResponse {
    header Content-Type text/html*
  }
  header @frontendResponse Content-Security-Policy "style-src 'self'; connect-src $connect; img-src 'self' data: blob:; worker-src 'self' blob:; font-src 'self'; media-src 'self'; manifest-src 'self'; child-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none';"
  header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains"
    X-Frame-Options "DENY"
    Referrer-Policy "no-referrer"
    X-Content-Type-Options "nosniff"
    X-Weltgewebe-Build "{\$WELTGEWEBE_BUILD}"
  }
}
CADDY
}

write_prod_caddy() {
  local file="$1"
  cat > "$file" << 'CADDY'
example.test {
  header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains"
    X-Frame-Options "DENY"
    Referrer-Policy "no-referrer"
    X-Content-Type-Options "nosniff"
    X-Weltgewebe-Build "{$WELTGEWEBE_BUILD}"
  }
}
CADDY
}

# The dev proxy fronts the Vite dev server and keeps a style-only inline
# exception. Its permitted value comes from the policy, so the fixture derives
# it the same way the guard does instead of restating it.
dev_style_src() {
  uv run --project "$REPO_ROOT/tools/py" --locked python - \
    "$TEMP_DIR/policies/security.yml" << 'PY'
from pathlib import Path
import sys

import yaml

data = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data["content_security_policy"]["dev_proxy_style_src"])
PY
}

write_dev_caddy() {
  local file="$1"
  local style="${2:-}"
  if [[ -z "$style" ]]; then
    style="$(dev_style_src)"
  fi
  cat > "$file" << CADDY
:8081 {
  handle_path /api/* {
    reverse_proxy api:8080
  }
  reverse_proxy /* web:5173
  header {
    Content-Security-Policy "default-src 'self'; script-src 'self'; style-src ${style}; connect-src 'self' ws: wss:; img-src 'self' data: blob:; worker-src 'self' blob:; object-src 'none';"
    X-Frame-Options "DENY"
    Referrer-Policy "no-referrer"
  }
}
CADDY
}

write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile" "'self' ws: wss:"
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.heim" "'self'"
write_prod_caddy "$TEMP_DIR/infra/caddy/Caddyfile.prod"
write_dev_caddy "$TEMP_DIR/infra/caddy/Caddyfile.dev"

REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null

sed -i '/Strict-Transport-Security/d' "$TEMP_DIR/infra/caddy/Caddyfile.vps"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail without HSTS" >&2
  exit 1
fi
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"

mutate_policy() {
  local operation="$1"
  uv run --project "$REPO_ROOT/tools/py" --locked python - "$TEMP_DIR/policies/security.yml" "$operation" << 'PY'
from pathlib import Path
import sys
import yaml

path = Path(sys.argv[1])
operation = sys.argv[2]
data = yaml.safe_load(path.read_text(encoding="utf-8"))
if operation == "short-max-age":
    data["strict_transport_security"]["max_age_seconds"] = 60
elif operation == "no-subdomains":
    data["strict_transport_security"]["include_subdomains"] = False
elif operation == "preload":
    data["strict_transport_security"]["preload"] = True
elif operation == "frame-ancestors-self":
    data["content_security_policy"]["required_frontend_response_directives"]["frame-ancestors"] = "'self'"
elif operation == "unsupported-script-delivery":
    data["content_security_policy"]["script_delivery"] = "unsupported_delivery"
elif operation == "nonce-script-mode":
    data["content_security_policy"]["script_mode"] = "nonce"
elif operation == "inline-style-mode":
    data["content_security_policy"]["style_mode"] = "inline"
elif operation == "reinstated-style-exception":
    data["csp_exceptions"] = [
        {
            "directive": "style-src 'unsafe-inline'",
            "status": "accepted_residual_risk",
            "reason": "reinstated without review",
        }
    ]
elif operation == "wider-dev-style":
    data["content_security_policy"]["dev_proxy_style_src"] = "'self' 'unsafe-inline' https:"
elif operation == "dropped-dev-style":
    del data["content_security_policy"]["dev_proxy_style_src"]
elif operation == "coverage-control":
    data["effective_caddy_contract"] = {"production_https_caddyfiles": []}
elif operation == "unknown-control":
    data["allowed_origins"] = ["https://example.test"]
else:
    raise SystemExit(f"unknown test policy mutation: {operation}")
path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

reset_policy
mutate_policy "short-max-age"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should derive max_age_seconds from policy" >&2
  exit 1
fi

reset_policy
mutate_policy "no-subdomains"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should derive include_subdomains from policy" >&2
  exit 1
fi

reset_policy
mutate_policy "preload"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should derive preload from policy" >&2
  exit 1
fi

reset_policy
mutate_policy "frame-ancestors-self"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should derive required CSP directives from policy" >&2
  exit 1
fi

reset_policy
mutate_policy "unsupported-script-delivery"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should reject unsupported script delivery policy" >&2
  exit 1
fi

reset_policy
mutate_policy "nonce-script-mode"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should derive SvelteKit script mode from policy" >&2
  exit 1
fi

reset_policy
mutate_policy "inline-style-mode"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should reject an inline style mode" >&2
  exit 1
fi

reset_policy
mutate_policy "reinstated-style-exception"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should reject a silently reinstated CSP exception" >&2
  exit 1
fi

reset_policy
mutate_policy "coverage-control"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should reject policy-controlled validation coverage" >&2
  exit 1
fi

reset_policy
mutate_policy "unknown-control"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should reject undeclared policy control surfaces" >&2
  exit 1
fi
reset_policy
mutate_policy "wider-dev-style"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when the dev style exception widens in policy alone" >&2
  exit 1
fi

reset_policy
mutate_policy "dropped-dev-style"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when the dev style exception is undeclared" >&2
  exit 1
fi

reset_policy

write_dev_caddy "$TEMP_DIR/infra/caddy/Caddyfile.dev" "'self'"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when the dev proxy style-src drifts from policy" >&2
  exit 1
fi
write_dev_caddy "$TEMP_DIR/infra/caddy/Caddyfile.dev"

sed -i "s/script-src 'self';/script-src 'self' 'unsafe-inline';/" "$TEMP_DIR/infra/caddy/Caddyfile.dev"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when the dev proxy opens script-src" >&2
  exit 1
fi
write_dev_caddy "$TEMP_DIR/infra/caddy/Caddyfile.dev"

reset_policy

uv run --project "$REPO_ROOT/tools/py" --locked python - "$TEMP_DIR/infra/caddy/Caddyfile.vps" << 'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
changed = 0
for index, line in enumerate(lines):
    if "header @frontendResponse Content-Security-Policy" not in line:
        continue
    replacement = line.replace(" frame-ancestors 'none';", "")
    if replacement != line:
        lines[index] = replacement
        changed += 1
if changed != 1:
    raise SystemExit(f"expected one frontend CSP mutation, changed={changed}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when frontend CSP loses frame-ancestors even if API CSP retains it" >&2
  exit 1
fi
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"

# Same repo-canonical tools/py environment as make validate / UV_RUN.
if ! command -v uv > /dev/null 2>&1; then
  echo "ERROR: uv is required for security headers guard tests (tools/py/uv.lock)." >&2
  exit 1
fi
uv run --project "$REPO_ROOT/tools/py" --locked python - "$TEMP_DIR/infra/caddy/Caddyfile.vps" << 'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace(
    "  @magicLinkConfirm {\n    method GET\n    path /api/auth/magic-link/consume\n  }",
    "  @magicLinkConfirm {\n    path /api/auth/magic-link/consume\n  }\n  @other {\n    method GET\n    path /other\n  }",
    1,
)
path.write_text(text, encoding="utf-8")
PY
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when GET exists outside the magic-link matcher" >&2
  exit 1
fi
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"

sed -i 's/header @magicLinkConfirm >Content-Security-Policy/header @magicLinkConfirm Content-Security-Policy/' "$TEMP_DIR/infra/caddy/Caddyfile.vps"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when magic-link CSP is not deferred" >&2
  exit 1
fi
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"

sed -i "s/style-src 'self';/script-src 'self' 'unsafe-inline'; style-src 'self';/" "$TEMP_DIR/infra/caddy/Caddyfile.vps"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail with script-src unsafe-inline" >&2
  exit 1
fi
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"

sed -i "s/style-src 'self';/style-src 'self' 'unsafe-inline';/" "$TEMP_DIR/infra/caddy/Caddyfile.vps"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail with style-src unsafe-inline" >&2
  exit 1
fi
write_static_caddy "$TEMP_DIR/infra/caddy/Caddyfile.vps" "'self'"

# A drifted style block must invalidate every edge copy of its hash.
sed -i 's/background: #f4f4f4;/background: #f5f5f5;/' "$TEMP_DIR/apps/api/src/routes/auth.rs"
if REPO_ROOT="$TEMP_DIR" bash "$GUARD" > /dev/null 2>&1; then
  echo "security headers guard should fail when the magic-link style hash is stale" >&2
  exit 1
fi
reset_auth_source

echo "PASS: security headers guard"
