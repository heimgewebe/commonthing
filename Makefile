.PHONY: up down logs ps smoke docs-guard validate ci-validate validate-tests validate-core validate-guards validate-shell-tests cell-pilot-check platform-check platform-render platform-kind-proof generate diagnose prepare-commit generate-system-map check-system-map-drift require-uv-tooling agent-contract-check

# Fixture repositories are short-lived. Detached Git maintenance can outlive a
# command and race with TemporaryDirectory cleanup or a following local clone.
CI_TEST_GIT_ENV = \
	GIT_CONFIG_COUNT=2 \
	GIT_CONFIG_KEY_0=maintenance.auto \
	GIT_CONFIG_VALUE_0=false \
	GIT_CONFIG_KEY_1=gc.auto \
	GIT_CONFIG_VALUE_1=0

# Repo-canonical Python for agent and contract checks: tools/py + uv.lock.
# Direct `uv run --project tools/py --locked …` and make validate share this path.
UV_PROJECT := tools/py
UV_RUN := uv run --project $(UV_PROJECT) --locked

require-uv-tooling:
	@command -v uv >/dev/null 2>&1 || { \
		echo "ERROR: uv is required for repo-canonical Python checks (tools/py/uv.lock)." >&2; \
		echo "Install the version pinned in toolchain.versions.yml (see docs/runbooks/uv-tooling.md)." >&2; \
		exit 1; \
	}
	@$(UV_RUN) python -c "import sys, yaml, pytest; assert sys.version_info >= (3, 11), sys.version; assert yaml.__version__ == '6.0.2', yaml.__version__; assert pytest.__version__ == '9.0.3', pytest.__version__" || { \
		echo "ERROR: tools/py environment drifted (need Python >=3.11, PyYAML==6.0.2, pytest==9.0.3)." >&2; \
		echo "Run: uv sync --project tools/py --locked" >&2; \
		exit 1; \
	}

# Same semantics as `just agent-contract-check` / direct uv-bound validators.
agent-contract-check: require-uv-tooling
	$(UV_RUN) python -m scripts.agent.validate_agent_tooling_lock
	$(UV_RUN) python -m scripts.agent.validate_repo_agent_contract

validate-tests: require-uv-tooling agent-contract-check
	$(UV_RUN) python -m unittest discover scripts/docmeta/tests/
	$(UV_RUN) python -m unittest discover scripts/agent/tests/
	# Full make validate Python path is tools/py only (no bare host python3).
	$(CI_TEST_GIT_ENV) $(UV_RUN) python -m unittest discover scripts/ci/tests/
	$(UV_RUN) python -m pytest -q scripts/ci/tests/test_semantic_search_production_activation.py scripts/ci/tests/test_schauwerk_editor_release_preflight.py
	$(UV_RUN) python scripts/docmeta/validate_claim_registry.py
	$(UV_RUN) python scripts/docmeta/validate_doc_freshness_registry.py
	$(UV_RUN) python -m scripts.docmeta.generate_claim_evidence_map --check

cell-pilot-check: require-uv-tooling
	$(UV_RUN) python scripts/platform/validate_two_operator_pilot.py --mode example platform/cell-pilot/two-operator-pilot.example.invalid.json
	$(UV_RUN) python -m unittest scripts.ci.tests.test_two_operator_cell_pilot

platform-check: cell-pilot-check
	$(UV_RUN) python scripts/platform/validate_platform.py
	$(UV_RUN) python -m unittest scripts.ci.tests.test_kubernetes_platform_contract

platform-render: require-uv-tooling
	$(UV_RUN) python scripts/platform/validate_platform.py --render

platform-kind-proof: require-uv-tooling
	$(UV_RUN) python scripts/platform/kind_reference.py proof --mode direct

validate-core: require-uv-tooling
	$(UV_RUN) python -m scripts.docmeta.validate_schema
	$(UV_RUN) python -m scripts.docmeta.validate_relations
	$(UV_RUN) python -m scripts.docmeta.check_repo_index_consistency
	$(UV_RUN) python -m scripts.docmeta.check_doc_review_age
	$(UV_RUN) python -m scripts.docmeta.review_impact
	$(UV_RUN) python -m scripts.docmeta.validate_opt_arc_001_db_proof_matrix
	$(UV_RUN) python -m scripts.docmeta.export_docs_index
	$(UV_RUN) python -m scripts.docmeta.generate_audit_gaps
	$(UV_RUN) python -m scripts.docmeta.check_links
	$(UV_RUN) python -m scripts.search.validate_relevance_goldset

generate-system-map: require-uv-tooling
	$(UV_RUN) python -m scripts.docmeta.generate_system_map

check-system-map-drift: generate-system-map
	git diff --exit-code HEAD -- docs/_generated/system-map.md

validate-guards: check-system-map-drift
	$(UV_RUN) python -m scripts.docmeta.attention_impact_guard
	bash scripts/docmeta/repo-structure-guard.sh
	bash scripts/docmeta/docs-relations-guard.sh
	bash scripts/docmeta/generated-files-guard.sh
	bash scripts/docmeta/coverage-guard.sh

validate-shell-tests:
	bash scripts/tests/test_weltgewebe_up_git_branch.sh
	bash scripts/tests/test_weltgewebe_up_frontend_required.sh
	bash scripts/tests/test_weltgewebe_up_deploy_scope.sh
	bash scripts/tests/test_version_guard.sh
	bash scripts/tests/test_basemap_mode_guard.sh
	bash scripts/tests/test_basemap_runtime_proof_contract.sh
	bash scripts/tests/test_security_headers_guard.sh
	bash scripts/tests/test_repo_contract_guards.sh
	bash scripts/tests/test_postgres_backup_restore_contract.sh
	bash scripts/tests/test_offhost_backup_pull_contract.sh
	bash scripts/tests/test_web_artifact_install_contract.sh
	bash scripts/tests/test_api_release_identity_contract.sh
	bash scripts/tests/test_runtime_contract.sh

validate: platform-check validate-tests validate-core validate-guards validate-shell-tests

ci-validate: validate

docs-guard: validate

generate: require-uv-tooling
	bash scripts/docmeta/generate-doc-index.sh
	$(UV_RUN) python -m scripts.docmeta.generate_backlinks
	bash scripts/docmeta/generate-impl-index.sh
	$(UV_RUN) python -m scripts.docmeta.generate_orphans
	$(UV_RUN) python -m scripts.docmeta.generate_supersession_map
	$(UV_RUN) python -m scripts.docmeta.generate_system_map
	$(UV_RUN) python -m scripts.docmeta.generate_doc_coverage
	$(UV_RUN) python -m scripts.docmeta.generate_knowledge_gaps
	$(UV_RUN) python -m scripts.docmeta.generate_implicit_dependencies
	$(UV_RUN) python -m scripts.docmeta.generate_staleness_report
	$(UV_RUN) python -m scripts.docmeta.generate_agent_readiness
	$(UV_RUN) python -m scripts.docmeta.generate_claim_evidence_map
	$(UV_RUN) python -m scripts.docmeta.generate_relations_analysis
	$(UV_RUN) python -m scripts.docmeta.generate_relates_to_audit
	$(UV_RUN) python -m scripts.docmeta.generate_report_lifecycle
	$(UV_RUN) python -m scripts.docmeta.generate_report_lifecycle_inventory

diagnose: generate

# prepare-commit intentionally runs only blocking validation checks.
prepare-commit: validate

up:
	GIT_COMMIT_SHA="$$(git rev-parse HEAD)" docker compose -f infra/compose/compose.core.yml --profile dev up -d --build

down:
	docker compose -f infra/compose/compose.core.yml --profile dev down -v

logs:
	docker compose -f infra/compose/compose.core.yml --profile dev logs -f --tail=200

ps:
	docker compose -f infra/compose/compose.core.yml --profile dev ps

smoke:
	gh workflow run compose-smoke --ref main || true
