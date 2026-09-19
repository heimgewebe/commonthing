#!/usr/bin/env bash
set -euo pipefail

TOOLING_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." > /dev/null 2>&1 && pwd)"
REPO_ROOT="${REPO_ROOT:-$TOOLING_ROOT}"
POLICY_FILE="${REPO_ROOT}/policies/security.yml"

if ! command -v uv > /dev/null 2>&1; then
  echo "ERROR: uv is required for security-headers-guard (tools/py/uv.lock)." >&2
  exit 1
fi

uv run --project "$TOOLING_ROOT/tools/py" --locked python - "$REPO_ROOT" "$POLICY_FILE" << 'PY'
from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import re
import sys

import yaml

repo = Path(sys.argv[1]).resolve()
policy_path = Path(sys.argv[2])
errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def exact_keys(value: object, expected: set[str], label: str) -> bool:
    if not isinstance(value, dict):
        fail(f"{label} must be a mapping")
        return False
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown or missing:
        fail(f"{label} keys drifted: unknown={unknown}, missing={missing}")
        return False
    return True


def contract_file(relative: object, label: str) -> Path | None:
    if not isinstance(relative, str) or not relative:
        fail(f"{label} must be a non-empty repository-relative path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        fail(f"{label} must be a safe repository-relative path: {relative!r}")
        return None
    resolved = (repo / candidate).resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        fail(f"{label} escapes repository root: {relative!r}")
        return None
    if not resolved.is_file():
        fail(f"{label} missing: {relative}")
        return None
    return resolved


def named_matcher_block(text: str, matcher: str) -> str | None:
    lines = text.splitlines()
    start_re = re.compile(rf"^\s*@{re.escape(matcher)}\s*\{{\s*$")
    for index, line in enumerate(lines):
        if not start_re.match(line):
            continue
        depth = 0
        block: list[str] = []
        for candidate in lines[index:]:
            block.append(candidate)
            depth += candidate.count("{") - candidate.count("}")
            if depth == 0:
                return "\n".join(block)
        break
    return None


def frontend_response_csp(text: str, relative: str) -> dict[str, set[str]] | None:
    matches = re.findall(
        r'(?m)^\s*header\s+@frontendResponse\s+Content-Security-Policy\s+"([^"]*)"\s*$',
        text,
    )
    if len(matches) != 1:
        fail(
            f"{relative} must contain exactly one @frontendResponse Content-Security-Policy header; "
            f"found {len(matches)}"
        )
        return None

    parsed: dict[str, set[str]] = {}
    for segment in matches[0].split(";"):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split()
        name, values = parts[0], set(parts[1:])
        if name in parsed:
            fail(f"{relative} @frontendResponse CSP repeats directive {name}")
            return None
        parsed[name] = values
    return parsed


if not policy_path.is_file():
    print(f"ERROR: security policy missing: {policy_path}", file=sys.stderr)
    raise SystemExit(1)

try:
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
except (OSError, yaml.YAMLError) as error:
    print(f"ERROR: failed to read security policy: {error}", file=sys.stderr)
    raise SystemExit(1)

root_keys = {
    "version",
    "content_security_policy",
    "csp_exceptions",
    "strict_transport_security",
}
if not exact_keys(policy, root_keys, "policies/security.yml"):
    policy = policy if isinstance(policy, dict) else {}
if policy.get("version") != 2:
    fail("policies/security.yml version must be 2")

csp = policy.get("content_security_policy")
csp_keys = {
    "script_delivery",
    "script_mode",
    "style_delivery",
    "style_mode",
    "magic_link_confirm_style_source",
    "required_frontend_response_directives",
}
if exact_keys(csp, csp_keys, "content_security_policy"):
    assert isinstance(csp, dict)
    if csp["script_delivery"] != "sveltekit_prerender_meta":
        fail("content_security_policy.script_delivery must be sveltekit_prerender_meta")
    if csp["script_mode"] != "hash":
        fail("content_security_policy.script_mode must be hash")
    if csp["style_delivery"] != "external_stylesheets":
        fail("content_security_policy.style_delivery must be external_stylesheets")
    if csp["style_mode"] != "external":
        fail("content_security_policy.style_mode must be external")
    directives = csp["required_frontend_response_directives"]
    directive_names = {
        "style-src", "connect-src", "img-src", "worker-src", "font-src",
        "media-src", "manifest-src", "child-src", "frame-src", "object-src",
        "base-uri", "form-action", "frame-ancestors",
    }
    if exact_keys(directives, directive_names, "content_security_policy.required_frontend_response_directives"):
        assert isinstance(directives, dict)
        for name, value in directives.items():
            if not isinstance(value, str) or not value.strip():
                fail(f"required CSP directive {name} must be a non-empty string")
else:
    directives = {}

# The inline-script and inline-style exceptions are both gone. Re-introducing
# one is a reviewed constitutional change, not a policy edit: this guard keeps
# the list empty so a silent re-entry cannot pass CI.
exceptions = policy.get("csp_exceptions")
if not isinstance(exceptions, list):
    fail("csp_exceptions must be a list")
elif exceptions:
    fail(
        "csp_exceptions must be empty; no residual CSP risk is accepted "
        f"(found {len(exceptions)})"
    )

hsts = policy.get("strict_transport_security")
expected_hsts = None
hsts_valid = exact_keys(hsts, {"max_age_seconds", "include_subdomains", "preload"}, "strict_transport_security")
if hsts_valid:
    assert isinstance(hsts, dict)
    max_age = hsts["max_age_seconds"]
    include_subdomains = hsts["include_subdomains"]
    preload = hsts["preload"]
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
        fail("strict_transport_security.max_age_seconds must be a positive integer")
        hsts_valid = False
    if not isinstance(include_subdomains, bool):
        fail("strict_transport_security.include_subdomains must be boolean")
        hsts_valid = False
    if not isinstance(preload, bool):
        fail("strict_transport_security.preload must be boolean")
        hsts_valid = False
    if hsts_valid:
        parts = [f"max-age={max_age}"]
        if include_subdomains:
            parts.append("includeSubDomains")
        if preload:
            parts.append("preload")
        expected_hsts = f'Strict-Transport-Security "{"; ".join(parts)}"'

# The set of active Caddy contracts is an architecture invariant, not a
# policy switch. A policy edit must not be able to remove a production file
# from security validation.
production_paths = [
    "infra/caddy/Caddyfile.vps",
    "infra/caddy/Caddyfile.heim",
    "infra/caddy/Caddyfile.prod",
]
static_paths = [
    "infra/caddy/Caddyfile",
    "infra/caddy/Caddyfile.vps",
    "infra/caddy/Caddyfile.heim",
]

fixed_headers = [
    'X-Frame-Options "DENY"',
    'Referrer-Policy "no-referrer"',
    'X-Content-Type-Options "nosniff"',
    'X-Weltgewebe-Build "{$WELTGEWEBE_BUILD}"',
]

for relative in production_paths:
    path = contract_file(relative, "production HTTPS Caddyfile")
    if path is None:
        continue
    text = path.read_text(encoding="utf-8")
    if expected_hsts is not None and expected_hsts not in text:
        fail(f"{relative} missing HSTS policy derived from policies/security.yml: {expected_hsts}")
    for header in fixed_headers:
        if header not in text:
            fail(f"{relative} missing constitutional header: {header}")

# The magic-link confirmation document is the one place that still serves an
# inline style element. Its policy is derived from the Rust constant that
# renders it, so a byte changed there fails this guard instead of silently
# shipping a stale hash to the edge.
STYLE_CONST = re.compile(
    r'const\s+MAGIC_LINK_CONFIRM_STYLE:\s*&str\s*=\s*"([^"\\]*)"\s*;'
)


def magic_link_policy() -> str | None:
    source = csp.get("magic_link_confirm_style_source") if isinstance(csp, dict) else None
    path = contract_file(source, "content_security_policy.magic_link_confirm_style_source")
    if path is None:
        return None
    matches = STYLE_CONST.findall(path.read_text(encoding="utf-8"))
    if len(matches) != 1:
        fail(
            f"{source} must define exactly one escape-free MAGIC_LINK_CONFIRM_STYLE "
            f"string literal; found {len(matches)}"
        )
        return None
    digest = base64.b64encode(hashlib.sha256(matches[0].encode("utf-8")).digest()).decode("ascii")
    return (
        "default-src 'none'; "
        f"style-src 'sha256-{digest}'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none';"
    )


magic_policy = magic_link_policy()
strict_policy = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';"
for relative in static_paths:
    path = contract_file(relative, "static-app Caddyfile")
    if path is None:
        continue
    text = path.read_text(encoding="utf-8")
    if "Content-Security-Policy" not in text:
        fail(f"{relative} missing static-app CSP")
        continue
    if re.search(r"script-src[^;]*'unsafe-inline'", text):
        fail(f"{relative} must not allow script-src unsafe-inline")
    if re.search(r"style-src[^;]*'unsafe-inline'", text):
        fail(f"{relative} must not allow style-src unsafe-inline")
    if strict_policy not in text:
        fail(f"{relative} missing strict non-document/error CSP baseline")

    magic_block = named_matcher_block(text, "magicLinkConfirm")
    if magic_block is None:
        fail(f"{relative} missing exact magic-link confirmation CSP matcher")
    else:
        if len(re.findall(r"(?m)^\s*method\s+GET\s*$", magic_block)) != 1:
            fail(f"{relative} magic-link matcher must contain exactly one GET method constraint")
        if len(re.findall(r"(?m)^\s*path\s+/api/auth/magic-link/consume\s*$", magic_block)) != 1:
            fail(f"{relative} magic-link matcher must contain the exact consume path")
    if magic_policy is not None:
        expected_magic_header = (
            f'header @magicLinkConfirm >Content-Security-Policy "{magic_policy}"'
        )
        if expected_magic_header not in text:
            fail(
                f"{relative} must defer and overwrite the canonical magic-link confirmation CSP: "
                f"{magic_policy}"
            )

    strict_matcher = "@nonDocumentResponse" if path.name == "Caddyfile.vps" else "@apiResponse"
    expected_strict_header = f'header {strict_matcher} >Content-Security-Policy "{strict_policy}"'
    if expected_strict_header not in text:
        fail(f"{relative} must defer and overwrite the canonical strict API CSP")

    frontend_directives = frontend_response_csp(text, relative)
    if frontend_directives is not None:
        for name, tokens in frontend_directives.items():
            if "'unsafe-inline'" in tokens:
                fail(f"{relative} @frontendResponse CSP must not allow {name} 'unsafe-inline'")
    if frontend_directives is not None and isinstance(directives, dict):
        for name, value in directives.items():
            actual_tokens = frontend_directives.get(name)
            required_tokens = set(value.split())
            if actual_tokens is None:
                fail(f"{relative} @frontendResponse CSP missing policy-required directive: {name} {value}")
            elif not required_tokens.issubset(actual_tokens):
                fail(
                    f"{relative} @frontendResponse CSP weakens policy-required directive "
                    f"{name}: required={sorted(required_tokens)}, actual={sorted(actual_tokens)}"
                )

svelte = contract_file("apps/web/svelte.config.js", "SvelteKit CSP config")
if svelte is not None and isinstance(csp, dict):
    text = svelte.read_text(encoding="utf-8")
    script_mode = csp.get("script_mode")
    if f'mode: "{script_mode}"' not in text:
        fail(f"apps/web/svelte.config.js must use policy script mode {script_mode!r}")
    if '"script-src": ["self"]' not in text:
        fail("apps/web/svelte.config.js must restrict SvelteKit script-src to self")

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
print("PASS: security header policy drives production Caddy/Svelte contracts")
PY
