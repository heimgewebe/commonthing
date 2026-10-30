#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
REUSABLE = "./.github/workflows/reusable-web-check.yml"
CALLERS = ("ci.yml", "web.yml", "heavy.yml")

CI_TOOL_DOWNLOAD_SCRIPTS = {
    "Install yq": dedent(
        r'''
        set -e
        YQ_VERSION="4.44.3"
        YQ_SHA256="a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7"
        YQ_DIR="$HOME/.local/bin"
        YQ_BIN="$YQ_DIR/yq"
        mkdir -p "$YQ_DIR"

        if [[ ! -f "$YQ_BIN" ]] || ! "$YQ_BIN" --version 2>/dev/null | grep -q "$YQ_VERSION"; then
          echo "Installing yq v${YQ_VERSION}..."
          INSTALL_TMPDIR=$(mktemp -d)
          trap 'rm -rf "$INSTALL_TMPDIR"' EXIT
          YQ_DOWNLOAD="$INSTALL_TMPDIR/yq_linux_amd64"
          wget -qO "$YQ_DOWNLOAD" "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_amd64"
          printf '%s  %s\n' "$YQ_SHA256" "$YQ_DOWNLOAD" | sha256sum -c -
          install -m 0755 "$YQ_DOWNLOAD" "$YQ_BIN"
          rm -rf "$INSTALL_TMPDIR"
          trap - EXIT
        else
          echo "yq v${YQ_VERSION} already installed"
        fi

        echo "$YQ_DIR" >> "$GITHUB_PATH"
        '''
    ).strip(),
    "Install cargo-deny": dedent(
        r'''
        set -euo pipefail
        # DENY_VERSION kommt aus toolchain.versions.yml (Step "Read toolchain versions").
        if [ -z "${DENY_VERSION:-}" ]; then
          echo "DENY_VERSION is not set (expected from toolchain.versions.yml)" >&2
          exit 1
        fi
        DENY_SHA256="663f655b23c58e7d8eaf1c6b6bd8e197742757b5314bd292fd8dcbc0a16581c6"
        DENY_DIR="$HOME/.local/bin"
        DENY_BIN="$DENY_DIR/cargo-deny"
        mkdir -p "$DENY_DIR"

        if [[ ! -f "$DENY_BIN" ]] || ! "$DENY_BIN" --version 2>/dev/null | grep -q "${DENY_VERSION}"; then
          echo "Installing cargo-deny v${DENY_VERSION}..."
          INSTALL_TMPDIR=$(mktemp -d)
          trap 'rm -rf "$INSTALL_TMPDIR"' EXIT
          TARBALL_URL="https://github.com/EmbarkStudios/cargo-deny/releases/download"
          TARBALL_URL="${TARBALL_URL}/${DENY_VERSION}/cargo-deny-${DENY_VERSION}-x86_64-unknown-linux-musl.tar.gz"
          TARBALL_PATH="$INSTALL_TMPDIR/cargo-deny.tar.gz"
          EXTRACT_DIR="$INSTALL_TMPDIR/extract"
          mkdir -p "$EXTRACT_DIR"
          curl -sLf "$TARBALL_URL" -o "$TARBALL_PATH"
          printf '%s  %s\n' "$DENY_SHA256" "$TARBALL_PATH" | sha256sum -c -
          tar xzf "$TARBALL_PATH" -C "$EXTRACT_DIR" --strip-components=1
          install -m 0755 "$EXTRACT_DIR/cargo-deny" "$DENY_BIN"
          rm -rf "$INSTALL_TMPDIR"
          trap - EXIT
        else
          echo "cargo-deny v${DENY_VERSION} already installed"
        fi

        echo "$DENY_DIR" >> "$GITHUB_PATH"
        '''
    ).strip(),
}


def _has_run_line(text: str, command: str) -> bool:
    expected = f"run: {command}"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == expected or stripped == f"- {expected}":
            return True
    return False


def _named_steps(text: str, name: str) -> list[str]:
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line == f"      - name: {name}"
    ]
    blocks: list[str] = []
    for start in starts:
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].startswith("      - name: ")
            ),
            len(lines),
        )
        blocks.append("\n".join(lines[start:end]))
    return blocks


def _named_run_script(block: str, name: str) -> str | None:
    lines = block.splitlines()
    if len(lines) < 3:
        return None
    if lines[0] != f"      - name: {name}" or lines[1] != "        run: |":
        return None
    script = "\n".join(lines[2:])
    return dedent(script).strip()


def _validate_tool_download_integrity(ci: str, errors: list[str]) -> None:
    for step_name, expected_script in CI_TOOL_DOWNLOAD_SCRIPTS.items():
        blocks = _named_steps(ci, step_name)
        if len(blocks) != 1:
            errors.append(
                f"ci.yml must retain exactly one {step_name} step, found {len(blocks)}"
            )
            continue

        observed_script = _named_run_script(blocks[0], step_name)
        if observed_script != expected_script:
            errors.append(
                f"ci.yml {step_name} must match the exact reviewed download-integrity step and script"
            )


def validate_workflow_texts(texts: dict[str, str]) -> list[str]:
    errors: list[str] = []
    reusable = texts["reusable-web-check.yml"]
    if "  workflow_call:" not in reusable.splitlines():
        errors.append("reusable-web-check.yml must expose the workflow_call contract")
    if "permissions:" in reusable.splitlines():
        errors.append("reusable-web-check.yml must inherit token permissions from each caller")
    if not _has_run_line(reusable, "pnpm playwright install --with-deps chromium"):
        errors.append("reusable web check must own Playwright browser installation")
    if 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: ""' not in reusable:
        errors.append("reusable web check must force-enable Playwright browser installation")
    if not _has_run_line(reusable, "pnpm test:ci") or not _has_run_line(reusable, "pnpm test"):
        errors.append("reusable web check must own both ci and full Playwright suite execution")

    for caller in CALLERS:
        text = texts[caller]
        if f"uses: {REUSABLE}" not in text:
            errors.append(f"{caller} must call {REUSABLE}")
        if any(
            _has_run_line(text, installer)
            for installer in (
                "pnpm playwright install --with-deps",
                "pnpm playwright install --with-deps chromium",
            )
        ):
            errors.append(f"{caller} must not reinstall Playwright directly")
        if _has_run_line(text, "pnpm test:ci") or _has_run_line(text, "pnpm test"):
            errors.append(f"{caller} must not invoke the shared Playwright suites directly")

    ci = texts["ci.yml"]
    if "run_demo_api: true" not in ci or "suite: ci" not in ci:
        errors.append("ci.yml web-e2e must preserve the demo API plus ci-suite contract")
    if "- web-e2e" not in ci:
        errors.append("ci.yml required merge gate must continue to depend on web-e2e")
    _validate_tool_download_integrity(ci, errors)

    web = texts["web.yml"]
    for marker in (
        "run_unit_tests: ${{ github.event_name == 'push' && github.ref != 'refs/heads/main' }}",
        "run_typecheck: true",
        "run_lint: true",
        "run_build: true",
        ".github/workflows/reusable-web-check.yml",
    ):
        if marker not in web:
            errors.append(f"web.yml lost required reusable-web-check contract marker: {marker}")

    heavy = texts["heavy.yml"]
    for marker in (
        "workflow_dispatch: {}",
        "full-ci",
        "if: needs.gate.outputs.run_heavy == 'true'",
        "run_build: true",
        "suite: full",
    ):
        if marker not in heavy:
            errors.append(f"heavy.yml lost on-demand/label gate marker: {marker}")

    return errors


def validate(root: Path = WORKFLOW_DIR) -> list[str]:
    names = (*CALLERS, "reusable-web-check.yml")
    texts = {name: (root / name).read_text(encoding="utf-8") for name in names}
    return validate_workflow_texts(texts)


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("CI workflow composition guard: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
