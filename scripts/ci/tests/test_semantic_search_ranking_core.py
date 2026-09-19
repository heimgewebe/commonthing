"""Regression tests for the T004 local embedding and hybrid-ranking core."""

from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path
from typing import Sequence

from scripts.search.hybrid_ranking_core import (
    MAX_CANDIDATES,
    MAX_TEXT_CHARACTERS,
    SEMANTIC_APPEND_LIMIT,
    BoundVector,
    GenerationIdentity,
    GenerationMismatchError,
    ModelIdentity,
    PrivacyBoundaryError,
    ProviderBoundaryError,
    ProviderUnavailableError,
    SearchCoreError,
    SearchDocument,
    SearchQuery,
    VectorValidationError,
    embed_bound,
    normalize_text,
    rank_hybrid,
    rank_lexical,
    rank_with_provider,
    validate_vector,
)
from scripts.search.probe_hybrid_ranking_core import (
    DEFAULT_OUTPUT,
    DEFAULT_SCHEMA,
    TASK_ID,
    DEFAULT_DATASET,
    check_receipt,
    choose_decision,
)

ROOT = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = ROOT / "contracts/search/hybrid-ranking-core-receipt.schema.json"
ARCHITECTURE = ROOT / "architecture/semantic-search.md"


def document(
    node_id: str,
    title: str,
    *,
    summary: str = "synthetische Beschreibung",
    body: str = "synthetischer Informationstext",
    tags: tuple[str, ...] = ("synthetisch",),
    kind: str = "Knoten",
) -> SearchDocument:
    return SearchDocument(
        node_id=node_id,
        title=title,
        summary=summary,
        body=body,
        tags=tags,
        kind=kind,
        language="de",
    )


def identity(
    *, revision: str = "sha256:" + "a" * 64, dimensions: int = 2
) -> ModelIdentity:
    return ModelIdentity(
        provider="local:test",
        model_id="synthetic-model",
        model_revision=revision,
        runtime_id="test-runtime-v1",
        dimensions=dimensions,
    )


class StaticProvider:
    def __init__(
        self,
        model: ModelIdentity,
        document_vectors: Sequence[Sequence[float]],
        query_vectors: Sequence[Sequence[float]],
    ) -> None:
        self.model = model
        self.outputs = [document_vectors, query_vectors]
        self.calls = 0

    def identity(self) -> ModelIdentity:
        return self.model

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        result = self.outputs[self.calls]
        self.calls += 1
        return result


class ChangingProvider(StaticProvider):
    def __init__(self) -> None:
        super().__init__(identity(), [[1.0, 0.0]], [[1.0, 0.0]])
        self.identity_calls = 0

    def identity(self) -> ModelIdentity:
        self.identity_calls += 1
        if self.identity_calls == 1:
            return identity(revision="sha256:" + "a" * 64)
        return identity(revision="sha256:" + "b" * 64)


class UnavailableProvider:
    def identity(self) -> ModelIdentity:
        raise ProviderUnavailableError("synthetic local provider unavailable")

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise AssertionError("embedding must not start after identity failure")


class BrokenBoundaryProvider:
    def identity(self) -> ModelIdentity:
        raise ProviderBoundaryError("synthetic provider identity violation")

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise AssertionError("embedding must not start after identity failure")


class SemanticSearchRankingCoreTests(unittest.TestCase):
    def test_normalization_is_shared_deterministic_nfkc_contract(self) -> None:
        self.assertEqual(normalize_text("  STRAẞE—Café  "), "strasse café")
        self.assertEqual(normalize_text("ＡＢＣ"), "abc")
        self.assertEqual(
            normalize_text("Reparatur\n  Werkstatt"), "reparatur werkstatt"
        )
        self.assertEqual(
            normalize_text("Reparatur Werkstatt"), normalize_text("REPARATUR—WERKSTATT")
        )

    def test_model_identity_rejects_nonlocal_or_invalid_dimensions(self) -> None:
        with self.assertRaisesRegex(ProviderBoundaryError, "local"):
            ModelIdentity("https://provider.invalid", "model", "revision", "runtime", 2)
        for dimensions in (0, 8193, True):
            with (
                self.subTest(dimensions=dimensions),
                self.assertRaises(VectorValidationError),
            ):
                ModelIdentity("local:test", "model", "revision", "runtime", dimensions)  # type: ignore[arg-type]

    def test_generation_identity_is_deterministic_and_tamper_evident(self) -> None:
        model = identity()
        first = GenerationIdentity.build(model, "document-v1")
        second = GenerationIdentity.build(model, "document-v1")
        changed = GenerationIdentity.build(model, "document-v2")
        self.assertEqual(first, second)
        self.assertNotEqual(first.generation_id, changed.generation_id)
        with self.assertRaisesRegex(GenerationMismatchError, "generation id"):
            GenerationIdentity("search-gen-" + "0" * 64, model, "document-v1")

    def test_search_document_accepts_only_authorized_synthetic_fields(self) -> None:
        payload = {
            "node_id": "synthetic-node",
            "title": "Werkstatt",
            "summary": "Synthetische Hilfe",
            "body": "Nur Testdaten",
            "tags": ["Reparatur"],
            "kind": "Werkstatt",
            "language": "de",
            "privacy_class": "synthetic",
            "contains_personal_data": False,
        }
        parsed = SearchDocument.from_mapping(payload)
        self.assertEqual(parsed.node_id, "synthetic-node")
        extra = dict(payload)
        extra["private_notes"] = "forbidden"
        with self.assertRaisesRegex(PrivacyBoundaryError, "exactly"):
            SearchDocument.from_mapping(extra)
        personal = dict(payload)
        personal["contains_personal_data"] = True
        with self.assertRaisesRegex(PrivacyBoundaryError, "PII-free"):
            SearchDocument.from_mapping(personal)

    def test_query_and_document_reject_common_personal_data_patterns(self) -> None:
        samples = (
            "kontakt person@example.invalid",
            "Rückruf unter +49 30 1234567",
            "Interner Dienst 192.168.1.1",
        )
        for value in samples:
            with self.subTest(value=value), self.assertRaises(PrivacyBoundaryError):
                SearchQuery(value)
            with self.subTest(document=value), self.assertRaises(PrivacyBoundaryError):
                document("node", "Titel", body=value)

    def test_direct_document_requires_immutable_tag_tuple(self) -> None:
        with self.assertRaisesRegex(SearchCoreError, "immutable tuple"):
            SearchDocument(
                node_id="node",
                title="Titel",
                summary="Synthetisch",
                body="Synthetisch",
                tags="synthetisch",  # type: ignore[arg-type]
                kind="Knoten",
                language="de",
            )

    def test_text_length_boundary_accepts_exact_limit_and_rejects_overflow(
        self,
    ) -> None:
        exact = "a" * MAX_TEXT_CHARACTERS
        self.assertEqual(SearchQuery(exact).text, exact)
        self.assertEqual(document("node", "Titel", body=exact).body, exact)
        with self.assertRaisesRegex(SearchCoreError, "exceeds"):
            SearchQuery(exact + "a")
        with self.assertRaisesRegex(SearchCoreError, "exceeds"):
            document("node", "Titel", body=exact + "a")

    def test_content_hash_binds_normalized_search_fields(self) -> None:
        first = document("node", "Café Werkstatt", tags=("Hilfe",))
        equivalent = document("node", "CAFÉ—WERKSTATT", tags=("HILFE",))
        changed = document("node", "Café Werkstatt", tags=("Anders",))
        self.assertEqual(first.content_sha256(), equivalent.content_sha256())
        self.assertNotEqual(first.content_sha256(), changed.content_sha256())

    def test_vector_validation_rejects_dimension_nonfinite_bool_and_zero_norm(
        self,
    ) -> None:
        invalid = (
            ([], "dimension"),
            ([1.0], "dimension"),
            ([math.nan, 1.0], "non-finite"),
            ([math.inf, 1.0], "non-finite"),
            ([True, 1.0], "non-number"),
            ([0.0, 0.0], "zero norm"),
        )
        for vector, message in invalid:
            with (
                self.subTest(vector=vector),
                self.assertRaisesRegex(VectorValidationError, message),
            ):
                validate_vector(vector, 2)
        self.assertEqual(validate_vector([1, 2.5], 2), (1.0, 2.5))

    def test_embed_bound_binds_one_generation_and_exact_counts(self) -> None:
        docs = [document("a", "Alpha"), document("b", "Beta")]
        queries = [SearchQuery("Alpha")]
        provider = StaticProvider(identity(), [[1.0, 0.0], [0.0, 1.0]], [[1.0, 0.0]])
        generation, document_vectors, query_vectors = embed_bound(
            provider, docs, queries, document_revision="document-v1"
        )
        self.assertEqual(set(document_vectors), {"a", "b"})
        self.assertEqual(len(query_vectors), 1)
        self.assertTrue(
            all(
                item.generation_id == generation.generation_id
                for item in document_vectors.values()
            )
        )
        self.assertEqual(query_vectors[0].generation_id, generation.generation_id)

        wrong_count = StaticProvider(identity(), [[1.0, 0.0]], [[1.0, 0.0]])
        with self.assertRaisesRegex(VectorValidationError, "count"):
            embed_bound(wrong_count, docs, queries, document_revision="document-v1")

    def test_provider_identity_change_during_measurement_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProviderBoundaryError, "changed"):
            embed_bound(
                ChangingProvider(),
                [document("a", "Alpha")],
                [SearchQuery("Alpha")],
                document_revision="document-v1",
            )

    def test_exact_title_tag_and_prefix_precede_semantic_similarity(self) -> None:
        model = identity()
        generation = GenerationIdentity.build(model, "document-v1")
        candidates = [
            document("exact", "Gemeinschaftsgarten", tags=("Garten",)),
            document("semantic", "Treffpunkt", tags=("Nachbarschaft",)),
        ]
        exact_result = rank_hybrid(
            SearchQuery("Gemeinschaftsgarten"),
            candidates,
            generation,
            BoundVector.build(generation, [0.0, 1.0]),
            {
                "exact": BoundVector.build(generation, [1.0, 0.0]),
                "semantic": BoundVector.build(generation, [0.0, 1.0]),
            },
            lexical_ranked_node_ids=("exact",),
        )
        self.assertEqual(exact_result.ranked_node_ids[0], "exact")

        tag_result = rank_hybrid(
            SearchQuery("Garten"),
            candidates,
            generation,
            BoundVector.build(generation, [0.0, 1.0]),
            {
                "exact": BoundVector.build(generation, [1.0, 0.0]),
                "semantic": BoundVector.build(generation, [0.0, 1.0]),
            },
            lexical_ranked_node_ids=("exact",),
        )
        self.assertEqual(tag_result.ranked_node_ids[0], "exact")

        prefix_result = rank_hybrid(
            SearchQuery("Gemeinschaft"),
            candidates,
            generation,
            BoundVector.build(generation, [0.0, 1.0]),
            {
                "exact": BoundVector.build(generation, [1.0, 0.0]),
                "semantic": BoundVector.build(generation, [0.0, 1.0]),
            },
            lexical_ranked_node_ids=("exact",),
        )
        self.assertEqual(prefix_result.ranked_node_ids[0], "exact")

    def test_hybrid_ranking_rejects_generation_dimension_and_candidate_mixing(
        self,
    ) -> None:
        first = GenerationIdentity.build(identity(), "document-v1")
        second = GenerationIdentity.build(
            identity(revision="sha256:" + "b" * 64), "document-v1"
        )
        candidates = [document("a", "Alpha"), document("b", "Beta")]
        with self.assertRaisesRegex(GenerationMismatchError, "query"):
            rank_hybrid(
                SearchQuery("unbekannte Anfrage"),
                candidates,
                first,
                BoundVector.build(second, [1.0, 0.0]),
                {
                    "a": BoundVector.build(first, [1.0, 0.0]),
                    "b": BoundVector.build(first, [0.0, 1.0]),
                },
                lexical_ranked_node_ids=(),
            )
        with self.assertRaisesRegex(GenerationMismatchError, "exact authorized"):
            rank_hybrid(
                SearchQuery("unbekannte Anfrage"),
                candidates,
                first,
                BoundVector.build(first, [1.0, 0.0]),
                {"a": BoundVector.build(first, [1.0, 0.0])},
                lexical_ranked_node_ids=(),
            )
        with self.assertRaisesRegex(VectorValidationError, "dimension"):
            rank_hybrid(
                SearchQuery("unbekannte Anfrage"),
                candidates,
                first,
                BoundVector(first.generation_id, (1.0, 0.0, 0.0)),
                {
                    "a": BoundVector.build(first, [1.0, 0.0]),
                    "b": BoundVector.build(first, [0.0, 1.0]),
                },
                lexical_ranked_node_ids=(),
            )

    def test_hybrid_thresholds_must_be_finite_and_in_range(self) -> None:
        generation = GenerationIdentity.build(identity(), "document-v1")
        candidates = [document("node", "Titel")]
        vector = BoundVector.build(generation, [1.0, 0.0])
        for kwargs in (
            {"semantic_minimum_cosine": math.nan},
            {"semantic_minimum_cosine": 1.1},
            {"semantic_minimum_margin": -0.1},
            {"semantic_minimum_margin": math.inf},
            {"semantic_minimum_margin": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(SearchCoreError):
                rank_hybrid(
                    SearchQuery("synthetische Anfrage"),
                    candidates,
                    generation,
                    vector,
                    {"node": vector},
                    lexical_ranked_node_ids=(),
                    **kwargs,
                )

    def test_threshold_boundaries_are_inclusive_and_semantic_append_is_top_only(
        self,
    ) -> None:
        generation = GenerationIdentity.build(identity(), "document-v1")
        candidates = [
            document("lexical", "Lexikalisch"),
            document("top", "Semantisch eins"),
            document("runner", "Semantisch zwei"),
            document("lower", "Semantisch drei"),
        ]

        def vector_for_cosine(score: float) -> BoundVector:
            return BoundVector.build(
                generation, [score, math.sqrt(1.0 - score * score)]
            )

        result = rank_hybrid(
            SearchQuery("synthetische Grenzanfrage"),
            candidates,
            generation,
            BoundVector.build(generation, [1.0, 0.0]),
            {
                "lexical": vector_for_cosine(0.1),
                "top": vector_for_cosine(1.0),
                "runner": vector_for_cosine(0.985),
                "lower": vector_for_cosine(0.9),
            },
            lexical_ranked_node_ids=("lexical",),
            semantic_minimum_cosine=1.0,
            semantic_minimum_margin=0.015,
        )
        self.assertEqual(SEMANTIC_APPEND_LIMIT, 1)
        self.assertEqual(result.ranked_node_ids, ("lexical", "top"))

        below = math.nextafter(0.55, 0.0)
        rejected = rank_hybrid(
            SearchQuery("synthetische Grenzanfrage"),
            [document("below", "Unter Grenze")],
            generation,
            BoundVector.build(generation, [1.0, 0.0]),
            {"below": vector_for_cosine(below)},
            lexical_ranked_node_ids=(),
            semantic_minimum_cosine=0.55,
        )
        self.assertEqual(rejected.ranked_node_ids, ())

    def test_precomputed_postgres_ranking_is_authoritative_and_scope_checked(
        self,
    ) -> None:
        generation = GenerationIdentity.build(identity(), "document-v1")
        candidates = [document("node-a", "Alpha"), document("node-b", "Beta")]
        vectors = {
            "node-a": BoundVector.build(generation, [1.0, 0.0]),
            "node-b": BoundVector.build(generation, [0.0, 1.0]),
        }
        result = rank_hybrid(
            SearchQuery("fremde semantische Anfrage"),
            candidates,
            generation,
            BoundVector.build(generation, [1.0, 0.0]),
            vectors,
            lexical_ranked_node_ids=("node-b",),
        )
        self.assertEqual(result.ranked_node_ids[0], "node-b")
        with self.assertRaisesRegex(SearchCoreError, "authorized"):
            rank_hybrid(
                SearchQuery("fremde semantische Anfrage"),
                candidates,
                generation,
                BoundVector.build(generation, [1.0, 0.0]),
                vectors,
                lexical_ranked_node_ids=("foreign-node",),
            )

    def test_ties_are_stable_by_node_id(self) -> None:
        generation = GenerationIdentity.build(identity(), "document-v1")
        candidates = [
            document("node-b", "Anderer Ort"),
            document("node-a", "Weiterer Ort"),
        ]
        result = rank_hybrid(
            SearchQuery("vollständig fremde semantische Anfrage"),
            candidates,
            generation,
            BoundVector.build(generation, [1.0, 0.0]),
            {
                "node-b": BoundVector.build(generation, [1.0, 0.0]),
                "node-a": BoundVector.build(generation, [1.0, 0.0]),
            },
            lexical_ranked_node_ids=(),
            semantic_minimum_margin=0.0,
        )
        self.assertEqual(result.ranked_node_ids, ("node-a",))

    def test_only_explicit_provider_unavailability_falls_back_lexically(self) -> None:
        candidates = [document("exact", "Werkstatt"), document("other", "Treffpunkt")]
        result = rank_with_provider(
            UnavailableProvider(),
            SearchQuery("Werkstatt"),
            candidates,
            document_revision="document-v1",
            lexical_ranked_node_ids=("exact",),
        )
        self.assertEqual(result.mode, "lexical_fallback")
        self.assertEqual(result.ranked_node_ids[0], "exact")
        self.assertIn("unavailable", result.fallback_reason or "")
        limited = rank_with_provider(
            UnavailableProvider(),
            SearchQuery("Werkstatt"),
            candidates,
            document_revision="document-v1",
            lexical_ranked_node_ids=("exact", "other"),
            limit=1,
        )
        self.assertEqual(limited.ranked_node_ids, ("exact",))
        empty = rank_with_provider(
            UnavailableProvider(),
            SearchQuery("Werkstatt"),
            candidates,
            document_revision="document-v1",
            lexical_ranked_node_ids=(),
        )
        self.assertEqual(empty.ranked_node_ids, ())
        with self.assertRaisesRegex(SearchCoreError, "duplicate"):
            rank_with_provider(
                UnavailableProvider(),
                SearchQuery("Werkstatt"),
                candidates,
                document_revision="document-v1",
                lexical_ranked_node_ids=("exact", "exact"),
            )
        with self.assertRaises(ProviderBoundaryError):
            rank_with_provider(
                BrokenBoundaryProvider(),
                SearchQuery("Werkstatt"),
                candidates,
                document_revision="document-v1",
                lexical_ranked_node_ids=("exact",),
            )

    def test_empty_candidate_sets_return_empty_rankings(self) -> None:
        query = SearchQuery("synthetische Anfrage")
        self.assertEqual(rank_lexical(query, ()).ranked_node_ids, ())
        generation = GenerationIdentity.build(identity(), "document-v1")
        result = rank_hybrid(
            query,
            (),
            generation,
            BoundVector.build(generation, [1.0, 0.0]),
            {},
            lexical_ranked_node_ids=(),
        )
        self.assertEqual(result.ranked_node_ids, ())

    def test_candidate_limit_and_duplicate_ids_are_rejected(self) -> None:
        duplicate = [document("same", "Alpha"), document("same", "Beta")]
        with self.assertRaisesRegex(SearchCoreError, "unique"):
            rank_lexical(SearchQuery("Alpha"), duplicate)
        oversized = [
            document(f"node-{index}", f"Titel {index}")
            for index in range(MAX_CANDIDATES + 1)
        ]
        with self.assertRaisesRegex(SearchCoreError, "exceeds"):
            rank_lexical(SearchQuery("Titel"), oversized)

    def test_decision_selects_smallest_only_when_all_hard_gates_hold(self) -> None:
        def measured(
            model_id: str, size: int, top3: int, false_top1: int
        ) -> dict[str, object]:
            return {
                "model": {
                    "model_id": model_id,
                    "model_revision": "sha256:" + model_id[0] * 64,
                    "size_bytes": size,
                },
                "quality": {
                    "natural_top3_relevant_count": top3,
                    "natural_top3_relevant_rate": top3 / 19,
                    "false_top1_count": false_top1,
                    "visibility_leak_count": 0,
                    "exact_guard_failures": [],
                },
            }

        models = [
            measured("alpha", 1, 18, 1),
            measured("beta", 2, 16, 0),
            measured("gamma", 3, 19, 0),
        ]
        decision = choose_decision(models)  # type: ignore[arg-type]
        self.assertEqual(decision["selected_model"], "gamma")
        self.assertFalse(decision["assessed_models"][0]["qualifies"])

        stopped = copy.deepcopy(models)
        stopped[2]["quality"]["false_top1_count"] = 1  # type: ignore[index]
        self.assertEqual(choose_decision(stopped)["outcome"], "stop_embedding_path")  # type: ignore[arg-type]

    def test_receipt_schema_declares_strict_draft_2020_12_contract(self) -> None:
        schema = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["quality"]["additionalProperties"])
        self.assertEqual(schema["properties"]["task_id"]["const"], TASK_ID)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 2)
        ranking = schema["properties"]["ranking_contract"]["properties"]
        self.assertEqual(ranking["semantic_append_limit"]["const"], 1)
        self.assertEqual(ranking["semantic_tie_breaker"]["const"], "node_id_ascending")
        self.assertNotIn("hard_precedence", ranking)
        self.assertEqual(
            schema["properties"]["baseline"]["required"], ["t003_postgresql"]
        )

    def test_receipt_quality_aggregates_distinguish_all_cases_from_natural_subset(
        self,
    ) -> None:
        receipt = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        dataset = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
        cases = {item["id"]: item for item in dataset["cases"]}
        qualities = [
            receipt["baseline"]["t003_postgresql"]["quality"],
            *(item["quality"] for item in receipt["models"]),
        ]
        for quality in qualities:
            with self.subTest(natural=quality["natural_top3_relevant_count"]):
                self.assertEqual(
                    sum(
                        bucket["top3"]
                        for bucket in quality["per_expected_rank_class"].values()
                    ),
                    quality["top3_relevant_count"],
                )
                self.assertEqual(
                    sum(item["top3_relevant"] for item in quality["cases"]),
                    quality["top3_relevant_count"],
                )
                self.assertEqual(
                    sum(
                        item["top3_relevant"]
                        and cases[item["case_id"]]["natural_language"]
                        for item in quality["cases"]
                    ),
                    quality["natural_top3_relevant_count"],
                )

        four_b = next(
            item["quality"]
            for item in receipt["models"]
            if item["model"]["model_id"] == "qwen3-embedding:4b"
        )
        self.assertEqual(four_b["top3_relevant_count"], 23)
        self.assertEqual(four_b["natural_top3_relevant_count"], 18)
        self.assertEqual(four_b["per_expected_rank_class"]["semantic"]["top3"], 16)

    def test_checked_in_receipt_is_schema_valid_hash_bound_and_vector_free(
        self,
    ) -> None:
        if not DEFAULT_OUTPUT.exists():
            self.skipTest("T004 live receipt not generated yet")
        schema = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
        receipt = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        self.assertEqual(receipt["task_id"], TASK_ID)
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(set(receipt["baseline"]), {"t003_postgresql"})
        self.assertEqual(receipt["ranking_contract"]["semantic_append_limit"], 1)
        self.assertIn("hybrid ranking in production", receipt["does_not_establish"])
        check_receipt(DEFAULT_OUTPUT, DEFAULT_DATASET, DEFAULT_SCHEMA)
        serialized = DEFAULT_OUTPUT.read_text(encoding="utf-8")
        self.assertNotIn('"embedding"', serialized)
        self.assertNotIn('"vector"', serialized)

    def test_architecture_declares_t004_core_and_runtime_non_effects(self) -> None:
        text = ARCHITECTURE.read_text(encoding="utf-8")
        required = (
            "T004-Livebeleg",
            "weltgewebe-hybrid-ranking-v2",
            "ProviderUnavailableError",
            "keine persistente Projektion",
            "keine Search-API",
            "keine Webintegration",
            "kein hybrides Ranking in Produktion",
            "keine SemantAH-Stilllegung",
        )
        for phrase in required:
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
