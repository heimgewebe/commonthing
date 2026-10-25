from __future__ import annotations

from pathlib import Path

from scripts.ci.check_ci_workflow_structure import validate, validate_workflow_texts

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _texts() -> dict[str, str]:
    names = ("ci.yml", "web.yml", "heavy.yml", "reusable-web-check.yml")
    return {name: (WORKFLOW_DIR / name).read_text(encoding="utf-8") for name in names}


def _move_line_after(text: str, marker: str, after_marker: str) -> str:
    lines = text.splitlines(keepends=True)
    marker_index = next(index for index, line in enumerate(lines) if marker in line)
    line = lines.pop(marker_index)
    after_index = next(index for index, current in enumerate(lines) if after_marker in current)
    lines.insert(after_index + 1, line)
    return "".join(lines)


def _comment_verification_and_add_active_copy_after_use(
    text: str, verification: str, use: str
) -> str:
    lines = text.splitlines(keepends=True)
    verification_index = next(
        index for index, line in enumerate(lines) if line.strip() == verification
    )
    original = lines[verification_index]
    indent = original[: len(original) - len(original.lstrip())]
    lines[verification_index] = f"{indent}# {verification}\n"
    use_index = next(index for index, line in enumerate(lines) if line.strip() == use)
    lines.insert(use_index + 1, f"{indent}{verification}\n")
    return "".join(lines)


def _wrap_verification_in_false_branch(text: str, verification: str) -> str:
    lines = text.splitlines(keepends=True)
    verification_index = next(
        index for index, line in enumerate(lines) if line.strip() == verification
    )
    original = lines[verification_index]
    indent = original[: len(original) - len(original.lstrip())]
    lines.insert(verification_index, f"{indent}if false; then\n")
    lines.insert(verification_index + 2, f"{indent}fi\n")
    return "".join(lines)


def _assert_installer_contract_rejected(texts: dict[str, str], step_name: str) -> None:
    errors = validate_workflow_texts(texts)
    assert any(
        step_name in error
        and (
            "must match the exact reviewed download-integrity step and script" in error
            or "must retain exactly one" in error
        )
        for error in errors
    )


def test_repository_workflows_use_the_reusable_web_check_without_direct_playwright_duplication() -> None:
    assert validate() == []


def test_guard_rejects_reintroduced_direct_playwright_execution() -> None:
    for installer in ("pnpm playwright install --with-deps", "pnpm playwright install --with-deps chromium"):
        texts = _texts()
        texts["heavy.yml"] += chr(10).join(
            ("", f"      - run: {installer}", "      - run: pnpm test", "")
        )
        errors = validate_workflow_texts(texts)
        assert any("heavy.yml must not reinstall Playwright directly" in error for error in errors)
        assert any("heavy.yml must not invoke the shared Playwright suites directly" in error for error in errors)


def test_reusable_playwright_installer_requires_only_default_chromium() -> None:
    texts = _texts()
    installer = "pnpm playwright install --with-deps chromium"
    assert f"run: {installer}" in texts["reusable-web-check.yml"]
    # The Chromium-only default suite must never regress into installing all
    # browsers and their large unnecessary system dependency set.
    texts["reusable-web-check.yml"] = texts["reusable-web-check.yml"].replace(
        installer, "pnpm playwright install --with-deps", 1
    )
    errors = validate_workflow_texts(texts)
    assert any("reusable web check must own Playwright browser installation" in error for error in errors)


def test_guard_rejects_reusable_workflow_without_workflow_call() -> None:
    texts = _texts()
    old = chr(10).join(('"on":', "  workflow_call:"))
    new = chr(10).join(('"on":', "  workflow_dispatch:"))
    texts["reusable-web-check.yml"] = texts["reusable-web-check.yml"].replace(old, new)
    errors = validate_workflow_texts(texts)
    assert any("must expose the workflow_call contract" in error for error in errors)


def test_guard_rejects_reusable_workflow_that_overrides_caller_permissions() -> None:
    texts = _texts()
    marker = chr(10).join(("name: Reusable Web Check", ""))
    replacement = chr(10).join(
        ("name: Reusable Web Check", "", "permissions:", "  contents: read", "")
    )
    texts["reusable-web-check.yml"] = texts["reusable-web-check.yml"].replace(
        marker, replacement, 1
    )
    errors = validate_workflow_texts(texts)
    assert any(
        "must inherit token permissions from each caller" in error for error in errors
    )


def test_guard_rejects_missing_release_download_digest() -> None:
    cases = (
        (
            'YQ_SHA256="a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7"',
            "Install yq",
        ),
        (
            'DENY_SHA256="663f655b23c58e7d8eaf1c6b6bd8e197742757b5314bd292fd8dcbc0a16581c6"',
            "Install cargo-deny",
        ),
    )
    for marker, step_name in cases:
        texts = _texts()
        assert marker in texts["ci.yml"]
        texts["ci.yml"] = texts["ci.yml"].replace(marker, "", 1)
        _assert_installer_contract_rejected(texts, step_name)


def test_guard_rejects_checksum_verification_after_download_use() -> None:
    cases = (
        (
            'printf \'%s  %s\\n\' "$YQ_SHA256" "$YQ_DOWNLOAD" | sha256sum -c -',
            'install -m 0755 "$YQ_DOWNLOAD" "$YQ_BIN"',
            "Install yq",
        ),
        (
            'printf \'%s  %s\\n\' "$DENY_SHA256" "$TARBALL_PATH" | sha256sum -c -',
            'tar xzf "$TARBALL_PATH" -C "$EXTRACT_DIR" --strip-components=1',
            "Install cargo-deny",
        ),
    )
    for verification, use, step_name in cases:
        texts = _texts()
        texts["ci.yml"] = _move_line_after(texts["ci.yml"], verification, use)
        _assert_installer_contract_rejected(texts, step_name)


def test_guard_rejects_commented_early_verification_with_active_late_copy() -> None:
    cases = (
        (
            'printf \'%s  %s\\n\' "$YQ_SHA256" "$YQ_DOWNLOAD" | sha256sum -c -',
            'install -m 0755 "$YQ_DOWNLOAD" "$YQ_BIN"',
            "Install yq",
        ),
        (
            'printf \'%s  %s\\n\' "$DENY_SHA256" "$TARBALL_PATH" | sha256sum -c -',
            'tar xzf "$TARBALL_PATH" -C "$EXTRACT_DIR" --strip-components=1',
            "Install cargo-deny",
        ),
    )
    for verification, use, step_name in cases:
        texts = _texts()
        texts["ci.yml"] = _comment_verification_and_add_active_copy_after_use(
            texts["ci.yml"], verification, use
        )
        _assert_installer_contract_rejected(texts, step_name)


def test_guard_rejects_checksum_hidden_in_false_branch() -> None:
    cases = (
        (
            'printf \'%s  %s\\n\' "$YQ_SHA256" "$YQ_DOWNLOAD" | sha256sum -c -',
            "Install yq",
        ),
        (
            'printf \'%s  %s\\n\' "$DENY_SHA256" "$TARBALL_PATH" | sha256sum -c -',
            "Install cargo-deny",
        ),
    )
    for verification, step_name in cases:
        texts = _texts()
        texts["ci.yml"] = _wrap_verification_in_false_branch(
            texts["ci.yml"], verification
        )
        _assert_installer_contract_rejected(texts, step_name)

def test_guard_rejects_step_level_shell_override() -> None:
    for step_name in ("Install yq", "Install cargo-deny"):
        texts = _texts()
        marker = f"      - name: {step_name}\n        run: |\n"
        replacement = (
            f"      - name: {step_name}\n"
            "        shell: bash -c 'sha256sum() { cat >/dev/null; return 0; }; source \"{0}\"'\n"
            "        run: |\n"
        )
        assert marker in texts["ci.yml"]
        texts["ci.yml"] = texts["ci.yml"].replace(marker, replacement, 1)
        _assert_installer_contract_rejected(texts, step_name)


def test_guard_rejects_duplicate_installer_step_name() -> None:
    cases = (
        ("Install yq", "Verify yq"),
        ("Install cargo-deny", "Setup Just"),
    )
    for step_name, next_step in cases:
        texts = _texts()
        marker = f"      - name: {next_step}\n"
        duplicate = (
            f"      - name: {step_name}\n"
            "        run: echo malicious-second-installer\n\n"
        )
        assert marker in texts["ci.yml"]
        texts["ci.yml"] = texts["ci.yml"].replace(marker, duplicate + marker, 1)
        _assert_installer_contract_rejected(texts, step_name)
