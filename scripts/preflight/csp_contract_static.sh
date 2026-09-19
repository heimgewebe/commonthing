#!/usr/bin/env bash
set -euo pipefail
ROOT="${ROOT:-/opt/weltgewebe}"
REQUIRE_FRONTEND="${REQUIRE_FRONTEND:-1}"

if [[ "$REQUIRE_FRONTEND" != "1" ]]; then
  echo "csp_contract_static: Frontend delivery not required, skipping."
  exit 0
fi

if [[ -n "${CADDYFILE_PATH:-}" ]] && [[ -f "$CADDYFILE_PATH" ]]; then
  CADDYFILE="$CADDYFILE_PATH"
elif [[ -f "$ROOT/Caddyfile" ]]; then
  CADDYFILE="$ROOT/Caddyfile"
elif [[ -f "$ROOT/infra/caddy/Caddyfile.heim" ]]; then
  CADDYFILE="$ROOT/infra/caddy/Caddyfile.heim"
elif [[ -f "$ROOT/infra/caddy/Caddyfile.vps" ]]; then
  CADDYFILE="$ROOT/infra/caddy/Caddyfile.vps"
elif [[ -f "$ROOT/infra/caddy/Caddyfile" ]]; then
  CADDYFILE="$ROOT/infra/caddy/Caddyfile"
else
  CADDYFILE=""
fi

if [[ -z "$CADDYFILE" || ! -f "$CADDYFILE" ]]; then
  echo "ERROR: csp_contract_static could not find the target Caddyfile." >&2
  exit 1
fi
if grep -Eq "script-src[^;]*'unsafe-inline'" "$CADDYFILE"; then
  echo "ERROR: target Caddyfile still allows script-src 'unsafe-inline': $CADDYFILE" >&2
  exit 1
fi
if grep -Eq "style-src[^;]*'unsafe-inline'" "$CADDYFILE"; then
  echo "ERROR: target Caddyfile still allows style-src 'unsafe-inline': $CADDYFILE" >&2
  exit 1
fi

INDEX_HTML="$ROOT/apps/web/build/index.html"
if [[ ! -f "$INDEX_HTML" ]]; then
  echo "ERROR: csp_contract_static could not find index.html at $INDEX_HTML." >&2
  exit 1
fi

python3 - "$ROOT/apps/web/build" "$CADDYFILE" << 'PY'
from __future__ import annotations
import base64
import hashlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

JAVASCRIPT_MIME_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/x-ecmascript",
    "application/x-javascript",
    "text/ecmascript",
    "text/javascript",
    "text/javascript1.0",
    "text/javascript1.1",
    "text/javascript1.2",
    "text/javascript1.3",
    "text/javascript1.4",
    "text/javascript1.5",
    "text/jscript",
    "text/livescript",
    "text/x-ecmascript",
    "text/x-javascript",
}
ACTIVE_SCRIPT_TYPES = {"module", "importmap", "speculationrules"}


def executable_script_type(raw: str) -> bool:
    script_type = raw.split(";", 1)[0].strip().lower()
    return (
        not script_type
        or script_type in ACTIVE_SCRIPT_TYPES
        or script_type in JAVASCRIPT_MIME_TYPES
    )


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csp_meta: list[str] = []
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self.executable_script_before_csp = False
        self.csp_meta_outside_head = False
        self.active_markup: list[str] = []
        self._in_head = False
        self._script_inline = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered_tag = tag.lower()
        if lowered_tag == "head":
            self._in_head = True
        values = {key.lower(): (value or "") for key, value in attrs}
        for key, value in values.items():
            if re.fullmatch(r"on[a-z0-9_-]+", key):
                self.active_markup.append(f"inline event handler {key}")
            if value.lstrip().lower().startswith("javascript:"):
                self.active_markup.append(f"javascript URL in {key}")
        if "style" in values:
            self.inline_styles.append(f"style attribute on <{lowered_tag}>")
        if lowered_tag == "style":
            self.inline_styles.append("style element")
        if lowered_tag == "meta" and values.get("http-equiv", "").lower() == "content-security-policy":
            if not self._in_head:
                self.csp_meta_outside_head = True
            self.csp_meta.append(values.get("content", ""))
        if lowered_tag == "script":
            executable = executable_script_type(values.get("type", ""))
            if executable and not self.csp_meta:
                self.executable_script_before_csp = True
            self._script_inline = executable and "src" not in values
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_inline:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered_tag = tag.lower()
        if lowered_tag == "script":
            if self._script_inline:
                self.inline_scripts.append("".join(self._script_parts))
            self._script_inline = False
            self._script_parts = []
        if lowered_tag == "head":
            self._in_head = False


def directives(policy: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw in policy.split(";"):
        parts = raw.strip().split()
        if parts:
            result[parts[0].lower()] = parts[1:]
    return result

build = Path(sys.argv[1])
caddyfile = Path(sys.argv[2])
caddy_text = caddyfile.read_text(encoding="utf-8")
frontend_policies = re.findall(
    r'^\s*header\s+@frontendResponse\s+Content-Security-Policy\s+"([^"]*)"\s*$',
    caddy_text,
    flags=re.MULTILINE,
)
if len(frontend_policies) != 1:
    raise SystemExit(
        f"ERROR: expected exactly one @frontendResponse CSP in {caddyfile}, found {len(frontend_policies)}"
    )
frontend_directives = directives(frontend_policies[0])
for forbidden in ("default-src", "script-src"):
    if forbidden in frontend_directives:
        raise SystemExit(
            f"ERROR: frontend edge CSP in {caddyfile} must delegate {forbidden} to the document policy"
        )
# Styles are edge-owned: unlike script-src there is no per-document style
# policy to delegate to, so the header itself must carry a closed style-src.
edge_style_src = frontend_directives.get("style-src")
if not edge_style_src:
    raise SystemExit(f"ERROR: frontend edge CSP in {caddyfile} has no style-src directive")
if "'unsafe-inline'" in edge_style_src:
    raise SystemExit(f"ERROR: frontend edge CSP in {caddyfile} still allows style-src 'unsafe-inline'")

html_files = sorted(build.rglob("*.html"))
if not html_files:
    raise SystemExit(f"ERROR: no HTML files found under {build}")

validated = 0
for path in html_files:
    parser = DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    if len(parser.csp_meta) != 1:
        raise SystemExit(f"ERROR: expected exactly one CSP meta tag in {path}, found {len(parser.csp_meta)}")
    if parser.csp_meta_outside_head:
        raise SystemExit(f"ERROR: CSP meta tag must appear inside <head> in {path}")
    if parser.active_markup:
        raise SystemExit(f"ERROR: active inline HTML markup is forbidden in {path}: {parser.active_markup[0]}")
    # The edge style-src is 'self' with no hashes, so any inline style in a
    # compiled artifact would be dropped by the browser rather than applied.
    if parser.inline_styles:
        raise SystemExit(
            f"ERROR: inline styles are forbidden in {path}: {parser.inline_styles[0]}"
        )
    if parser.executable_script_before_csp:
        raise SystemExit(f"ERROR: executable script appears before the CSP meta tag in {path}")
    policy = directives(parser.csp_meta[0])
    script_src = policy.get("script-src")
    if not script_src:
        raise SystemExit(f"ERROR: CSP meta tag has no script-src directive in {path}")
    if "'unsafe-inline'" in script_src:
        raise SystemExit(f"ERROR: CSP meta tag still allows script-src 'unsafe-inline' in {path}")
    for body in parser.inline_scripts:
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        token = f"'sha256-{digest}'"
        if token not in script_src:
            raise SystemExit(f"ERROR: inline script hash missing from script-src in {path}: {token}")
    validated += 1
    print(
        f"csp_contract_static: {path.relative_to(build)} "
        f"scripts={len(parser.inline_scripts)} hash-bound, inline styles=0"
    )

# The edge CSP intentionally delegates script-src to each HTML document so that
# SvelteKit can authorize its inline bootstrap with build-generated hashes.
# Non-HTML active documents therefore need a separate fail-closed rule. Static
# SVG assets are repository/build artifacts only and must not contain scriptable
# constructs when served directly by Caddy.
active_svg = re.compile(
    r"<\s*script\b|javascript\s*:|\son[a-z0-9_-]+\s*=|"
    r"<\s*foreignobject\b|<\s*(?:iframe|object|embed)\b",
    re.IGNORECASE,
)
svg_files = sorted(build.rglob("*.svg"))
for path in svg_files:
    text = path.read_text(encoding="utf-8")
    match = active_svg.search(text)
    if match:
        raise SystemExit(
            f"ERROR: active/scriptable SVG content is forbidden in {path}: {match.group(0)!r}"
        )
    print(f"csp_contract_static: {path.relative_to(build)} passive-svg")

print(
    f"csp_contract_static: OK ({validated} HTML artifact(s) validated, "
    f"{len(svg_files)} passive SVG artifact(s) validated)"
)
PY
