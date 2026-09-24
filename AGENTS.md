---
id: repo.agents
title: AGENTS
doc_type: policy
status: active
canonicality: canonical
summary: Progressive agent entry card for commonThing; machine authority lives in agent-contract.json.
---

# AGENTS

Canonical entry card and orientation guide for agents working in this repository.
While **`agent-contract.json`** defines the strict machine-readable path, check, and generator authority, this document canonically defines the progressive entry sequence and human-facing context.

## Binding Reading Protocol

All agents MUST follow the [Agent Reading Protocol](docs/policies/agent-reading-protocol.md).

**Core Rules (Strictly Binding):**

1. **Reading Order:** `agent-contract.json` -> `repo.meta.yaml` -> `AGENTS.md` -> `agent-policy.yaml` -> `docs/policies/agent-reading-protocol.md`
2. **Conflict Resolution:** Domain contracts and `agent-contract.json` lead for agent operability; broader truth precedence remains in `repo.meta.yaml` (`truth_model.precedence`).
3. **No Interpolation:** Silent interpolation is FORBIDDEN. Explicitly name missing gaps.
4. **Abort Rule:** Agents MUST abort if contradictions are unresolvable, necessary files are missing, or target proof is impossible.
5. **Navigation vs Truth:** `docs/index.md` is strictly navigation. `docs/_generated/*` is diagnostic only; direct agent edits are forbidden. Declared trusted generators may refresh only contract-allowlisted derived targets; `secrets/` and `snapshots/` are never generator targets.

These core rules derive from `agent-contract.json`, `repo.meta.yaml`, and the Agent Reading Protocol.

## Canonical Sources

| Source | Role |
|---|---|
| `agent-contract.json` | Canonical machine agent norm (paths, checks, generators, architecture roles) |
| `repo.meta.yaml` | Repo truth model; overlapping path/check fields are a Compatibility Projection |
| `AGENTS.md` | This progressive entry card |
| `agent-policy.yaml` | Compatibility Projection of write-scope fields plus human-review hints |
| `docs/policies/agent-reading-protocol.md` | Binding read/abort protocol |
| `docs/policies/architecture-critique.md` | Cognitive module; not default reading order |

## Architecture Split

- **Repository role:** `repo_contract_authority` — portable contracts, validators, readiness, and CI checks.
- **Operator role:** `external_operator_execution` (Grabowski) — workspace, lease, execution, review, publish, recovery, cleanup.
- **Rejected:** a second generic repository-owned write operator / free-form write mode.

## Safe / Guarded / Forbidden (summary)

Authoritative arrays live only in `agent-contract.json`; use Validator/CLI.

Validate with:

```bash
python3 -m scripts.agent.validate_repo_agent_contract
```

## Generated Artifacts

- Direct agent edits under generated/forbidden prefixes are forbidden.
- Only generators declared in the registry referenced by the contract may write targets under `generated_artifacts.allowed_target_prefixes` (currently only `docs/_generated/`).
- `secrets/` and `snapshots/` remain forbidden for both direct edits and generator output.
- Prefer named validation profiles over free-form shell authority; task `validation_commands` remain transitional fixtures until profile migration completes.

## Task-Scoped Documents

Load only what the task needs:

- Roadmap / status / auth / UI / map / deploy / agent-operability: `docs/roadmap.md`, the relevant sub-roadmap, and the matching status matrix or report
- Agent safety architecture: `docs/blueprints/blueprint-agent-safety-control-layer.md`
- Write-scope baseline: `docs/security/agent-write-scope-baseline.md`

## Coding Safety

- Inspect the real file before proposing or changing code; do not invent repository structure or APIs.
- Keep snippets syntactically valid and directly executable. Show complete affected blocks; mark omissions explicitly.
- Preserve literal shell syntax, quoting, spacing, paths, and redirections. Do not compress commands into pseudo-tokens.
- Emit success only after the relevant operation succeeded. Mandatory checks must not hide failures with `|| true`, `|| echo`, or equivalent fallbacks.
- Reject non-finite frontend numbers with `Number.isFinite` and apply domain bounds before values reach rendering, coordinates, opacity, widths, radii, or sizes.
- State uncertainty explicitly and verify CI-relevant behavior with the real build, lint, or test command instead of assuming success.

## Discovery

Scan `.github/workflows/`, `apps/`, `contracts/`, `docs/`, `infra/`, `scripts/`, and `tests/` for changes.

## Common Traps

- Do not manually edit `docs/_generated/`.
- Do not treat Compatibility Projections as a second authority.
- Do not treat readiness `plan` pass as write/publish/deploy readiness.
- Critical infrastructure changes belong in `audit/impl-registry.yaml`.
