---
id: docs.generated.system-map
title: System Map
doc_type: generated
status: active
summary: Automatisch generierte System Map.
---
## Weltgewebe System Map

Generated automatically. Do not edit.

Source: scripts/docmeta/generate_system_map.py

## Zone: norm

|id|path|role|organ|status|last_reviewed|depends_on|verifies_with|missing_scripts|
|---|---|---|---|---|---|---|---|---|
|architecture.semantic-search|architecture/semantic-search.md|norm|product-domain|canonical|2026-07-19|overview, architecture.weltgewebe-os, specs.garnrolle-knoten-faden|contracts/search/examples/hybrid-ranking-core.heim-pc.json, contracts/search/examples/postgres-foundation.heim-pc.json, contracts/search/examples/relevance-benchmark.heim-pc.json, contracts/search/examples/relevance-goldset.example.json, contracts/search/hybrid-ranking-core-receipt.schema.json, contracts/search/postgres-foundation-receipt.schema.json, contracts/search/postgres-foundation.down.sql, contracts/search/postgres-foundation.up.sql, contracts/search/relevance-goldset.schema.json, scripts/ci/tests/test_semantic_search_contract.py, scripts/ci/tests/test_semantic_search_postgres_foundation.py, scripts/ci/tests/test_semantic_search_ranking_core.py, scripts/search/benchmark_relevance.py, scripts/search/hybrid_ranking_core.py, scripts/search/probe_hybrid_ranking_core.py, scripts/search/probe_postgres_foundation.py, scripts/search/validate_relevance_goldset.py||
|architecture.weltgewebe-os|architecture/weltgewebe-os.md|norm|governance|canonical|2026-07-15|overview|||
|docmeta.schema|architecture/docmeta.schema.md|norm|docmeta|canonical|2026-06-09||scripts/docmeta/check_doc_review_age.py, scripts/docmeta/check_repo_index_consistency.py, scripts/docmeta/generate_system_map.py, scripts/docmeta/validate_relations.py||
|overview|architecture/overview.md|norm|governance|canonical|2026-07-11||||
|security|architecture/security.md|norm|governance|canonical|2026-07-11||||

## Zone: platform

|id|path|role|organ|status|last_reviewed|depends_on|verifies_with|missing_scripts|
|---|---|---|---|---|---|---|---|---|
|platform.readme|platform/README.md|norm|ops|canonical|2026-09-10||scripts/platform/kind_reference.py, scripts/platform/validate_platform.py||

## Zone: policy

|id|path|role|organ|status|last_reviewed|depends_on|verifies_with|missing_scripts|
|---|---|---|---|---|---|---|---|---|
|docs.policies.agent-reading-protocol|docs/policies/agent-reading-protocol.md|norm|governance|canonical|2026-07-11||||
|docs.policies.architecture-critique|docs/policies/architecture-critique.md|norm|governance|canonical|2026-07-11||||

## Zone: product

|id|path|role|organ|status|last_reviewed|depends_on|verifies_with|missing_scripts|
|---|---|---|---|---|---|---|---|---|
|docs.specs.federation-core|docs/specs/federation-core.md|norm|governance|canonical|2026-07-15|architecture.weltgewebe-os|||
|docs.specs.federation-wire-v1|docs/specs/federation-wire-v1.md|norm|platform|canonical|2026-07-20|docs.specs.federation-core|||
|specs.garnrolle-knoten-faden|docs/specs/garnrolle-knoten-faden.md|norm|product-domain|canonical|2026-08-02||apps/api/tests/api_accounts.rs, apps/api/tests/api_governance_guards.rs, apps/api/tests/db_domain_edge_write_path.rs, apps/api/tests/db_node_conversations.rs, apps/web/src/lib/demo/resolvers.test.ts, apps/web/tests/garnrolle-relations.spec.ts, apps/web/tests/garnrolle-self-service.spec.ts, apps/web/tests/komposition.spec.ts, contracts/domain/account.schema.json, contracts/domain/edge.schema.json, contracts/domain/node.schema.json||
|specs.governance-antraege|docs/specs/governance-antraege.md|norm|governance|canonical|2026-08-16|specs.garnrolle-knoten-faden|apps/api/tests/api_governance_guards.rs, apps/api/tests/db_governance.rs, apps/web/src/lib/api/governance.test.ts, apps/web/tests/governance.spec.ts, apps/web/tests/proofs/governance-full-flow.proof.ts||
|specs.knoten-gewebe-visualisierung|docs/specs/knoten-gewebe-visualisierung.md|norm|product-map|canonical|2026-08-08|specs.garnrolle-knoten-faden, specs.map-experience|apps/web/src/lib/map/overlay/edges.test.ts, apps/web/src/lib/map/overlay/edges.ts, apps/web/src/lib/map/overlay/nodes.test.ts, apps/web/src/lib/map/overlay/nodes.ts, apps/web/src/lib/map/overlay/weaveRuntime.ts, apps/web/src/lib/map/weaveModel.test.ts, apps/web/src/lib/map/weaveModel.ts, apps/web/src/lib/map/weaveTheme.ts, apps/web/tests/garnrolle-marker-rendering.spec.ts, apps/web/tests/woven-node-visualization.spec.ts||
|specs.map-experience|docs/specs/map-experience.md|norm|product-map|canonical|2026-08-02|specs.ui-interaction, specs.ui-state-machine, specs.garnrolle-knoten-faden|apps/web/src/lib/map/scene.ts, apps/web/src/lib/map/types.ts, apps/web/src/routes/map/+page.svelte, apps/web/src/routes/map/+page.ts, apps/web/tests/edge-visibility.spec.ts, apps/web/tests/map-load-fallback.spec.ts, apps/web/tests/map-url-state.spec.ts, apps/web/tests/webgemeindezentrum-hammer-park.spec.ts||
|specs.objektlebenszyklen-und-loeschwirkungen|docs/specs/objektlebenszyklen-und-loeschwirkungen.md|norm|product-domain|canonical|2026-07-27||apps/api/tests/db_node_conversations.rs, contracts/domain/conversation.schema.json, contracts/domain/message.schema.json||
|specs.ortsweberei-webgemeindezentrum|docs/specs/ortsweberei-webgemeindezentrum.md|norm|product-domain|canonical|2026-08-08|specs.garnrolle-knoten-faden, specs.governance-antraege|apps/api/migrations/20260802000001_ortsweberei_webgemeindezentrum.up.sql, apps/api/src/routes/webgemeindezentren.rs, apps/api/tests/db_ortsweberei_webgemeindezentrum.rs, apps/web/src/lib/components/panels/WebgemeindezentrumPanel.svelte, apps/web/src/lib/map/scene.ts, apps/web/tests/webgemeindezentrum-hammer-park.spec.ts||
|specs.private-nachrichten|docs/specs/private-nachrichten.md|norm|product|canonical|2026-08-04|specs.garnrolle-knoten-faden|apps/api/migrations/20260804000001_web_push_notifications.up.sql, apps/api/src/notifications.rs, apps/api/src/routes/conversations.rs, apps/api/tests/db_node_conversations.rs, apps/web/src/lib/api/directMessages.ts, apps/web/src/lib/api/notifications.ts, apps/web/src/lib/components/NotificationSettings.svelte, apps/web/src/routes/nachrichten/+page.svelte, apps/web/static/sw.js||
|specs.ui-interaction|docs/specs/ui-interaction.md|norm|product-ui|canonical|2026-08-22|specs.ui-state-machine, specs.garnrolle-knoten-faden|apps/web/src/app.html, apps/web/src/lib/accountAttentionRuntime.ts, apps/web/src/lib/components/AttentionBubbles.svelte, apps/web/src/lib/components/ContextPanel.svelte, apps/web/src/lib/components/FilterOverlay.svelte, apps/web/src/lib/components/SearchOverlay.svelte, apps/web/src/lib/components/ToolFan.svelte, apps/web/src/lib/components/TopBarAuth.svelte, apps/web/src/lib/stores/mapChrome.ts, apps/web/src/lib/styles/tokens.css, apps/web/src/routes/settings/+page.svelte, apps/web/static/theme-init.js, apps/web/tests/attention-bubbles.spec.ts, apps/web/tests/context-panel-sheet.spec.ts, apps/web/tests/map-interaction.spec.ts, apps/web/tests/theme.spec.ts, apps/web/tests/tool-fan-layout.spec.ts, apps/web/tests/ui-filter.spec.ts, apps/web/tests/ui-search.spec.ts||
|specs.ui-state-machine|docs/specs/ui-state-machine.md|norm|product-ui|canonical|2026-07-11||apps/web/src/lib/stores/uiInvariants.test.ts, apps/web/src/lib/stores/uiInvariants.ts, apps/web/src/lib/stores/uiView.ts||

## Zone: reality

|id|path|role|organ|status|last_reviewed|depends_on|verifies_with|missing_scripts|
|---|---|---|---|---|---|---|---|---|
|runtime.readme|runtime/README.md|reality|runtime|canonical|2026-07-12||||

## Zone: runbooks

|id|path|role|organ|status|last_reviewed|depends_on|verifies_with|missing_scripts|
|---|---|---|---|---|---|---|---|---|
|runbooks.guest-exit-step-up-rollout|runbooks/guest-exit-step-up-rollout.md|runbooks|ops|canonical|2026-08-30|runbooks.readme|scripts/platform/validate_platform.py||
|runbooks.kubernetes-local-reference|runbooks/kubernetes-local-reference.md|runbooks|ops|canonical|2026-07-16|platform.readme|scripts/platform/kind_reference.py||
|runbooks.readme|runbooks/README.md|runbooks|ops|canonical|2026-07-11||||

## Automated Checks

- scripts/docmeta/check_doc_review_age.py
- scripts/docmeta/check_repo_index_consistency.py
- scripts/docmeta/generate_system_map.py
