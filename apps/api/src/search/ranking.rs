//! T006 hybrid search semantic append.
//!
//! The lexical prefix is authoritative PostgreSQL T003 output. This module does
//! not reimplement lexical scoring; it only preserves that order and may append
//! at most one confidence-gated semantic candidate with a stable node-id tie-break.

use std::{cmp::Ordering, collections::HashSet};
use unicode_normalization::UnicodeNormalization;

use crate::routes::nodes::Node;

pub const DEFAULT_SEMANTIC_MINIMUM_COSINE: f64 = 0.55;
pub const DEFAULT_SEMANTIC_MINIMUM_MARGIN: f64 = 0.015;
pub const SEMANTIC_APPEND_LIMIT: usize = 1;

#[derive(Debug, Clone)]
pub struct SearchQuery {
    pub raw: String,
    pub normalized: String,
}

impl SearchQuery {
    pub fn new(query: &str) -> Self {
        Self {
            raw: query.to_string(),
            normalized: normalize_query(query),
        }
    }

    pub fn embedding_text(&self) -> String {
        format!(
            "Aufgabe: Finde den relevantesten sichtbaren Weltgewebe-Knoten.\nAnfrage: {}",
            self.raw
        )
    }
}

pub fn normalize_query(value: &str) -> String {
    value
        .nfkc()
        .collect::<String>()
        .to_lowercase()
        .trim()
        .to_owned()
}

pub fn cosine_similarity(v1: &[f64], v2: &[f64]) -> f64 {
    if v1.len() != v2.len() || v1.is_empty() {
        return 0.0;
    }
    let mut dot = 0.0;
    let mut norm1 = 0.0;
    let mut norm2 = 0.0;
    for (a, b) in v1.iter().zip(v2.iter()) {
        dot += a * b;
        norm1 += a * a;
        norm2 += b * b;
    }
    if norm1 <= 0.0 || norm2 <= 0.0 || !dot.is_finite() {
        0.0
    } else {
        dot / (norm1.sqrt() * norm2.sqrt())
    }
}

#[derive(Debug, Clone)]
pub struct ScoredNode {
    pub node: Node,
    pub rank_class: u8,
    pub rank_score: f64,
    pub embedding: Option<Vec<f64>>,
}

fn semantic_candidate_order(a: (&ScoredNode, f64), b: (&ScoredNode, f64)) -> Ordering {
    b.1.partial_cmp(&a.1)
        .unwrap_or(Ordering::Equal)
        .then_with(|| a.0.node.id.cmp(&b.0.node.id))
}

/// Performs hybrid ranking and fusion.
///
/// Pre-conditions:
/// 1. `lexical_candidates` are already visible, authorized, filtered, and sorted lexically.
/// 2. `all_eligible_candidates` contains all visible, authorized, filtered candidates.
///
/// Post-conditions:
/// - Hard lexical precedence is preserved: exact title/tag/prefix/FTS rank order is maintained.
/// - At most ONE (1) additional semantic candidate is appended if query_vector is provided,
///   its cosine similarity >= `semantic_minimum_cosine`, and margin >= `semantic_minimum_margin`.
/// - Stable tie-breaks: node.id ASC.
pub fn rank_hybrid(
    query_vector: Option<&[f64]>,
    lexical_candidates: Vec<ScoredNode>,
    all_eligible_candidates: &[ScoredNode],
    semantic_minimum_cosine: f64,
    semantic_minimum_margin: f64,
) -> Vec<ScoredNode> {
    let mut result = lexical_candidates;
    let Some(q_vec) = query_vector else {
        return result;
    };

    let lexical_ids: HashSet<String> = result.iter().map(|item| item.node.id.clone()).collect();

    // Track only the two candidates needed by the confidence gate.
    let mut top: Option<(&ScoredNode, f64)> = None;
    let mut runner_up: Option<(&ScoredNode, f64)> = None;

    for candidate in all_eligible_candidates {
        if lexical_ids.contains(&candidate.node.id) {
            continue;
        }
        if let Some(c_vec) = &candidate.embedding {
            let sim = cosine_similarity(q_vec, c_vec);
            if sim >= semantic_minimum_cosine {
                let ranked = (candidate, sim);
                if top.is_none_or(|current| semantic_candidate_order(ranked, current).is_lt()) {
                    runner_up = top;
                    top = Some(ranked);
                } else if runner_up
                    .is_none_or(|current| semantic_candidate_order(ranked, current).is_lt())
                {
                    runner_up = Some(ranked);
                }
            }
        }
    }

    if let Some((top_candidate, top_score)) = top {
        let margin_ok = runner_up
            .is_none_or(|(_, second_score)| (top_score - second_score) >= semantic_minimum_margin);

        if margin_ok {
            // Append exactly ONE semantic candidate
            let mut scored = top_candidate.clone();
            scored.rank_class = 6;
            scored.rank_score = top_score;
            result.push(scored);
        }
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::routes::nodes::Location;

    fn test_node(id: &str, title: &str, tags: Vec<&str>) -> Node {
        Node {
            id: id.to_string(),
            kind: "place".to_string(),
            title: title.to_string(),
            created_at: "2026-01-01T00:00:00Z".to_string(),
            updated_at: "2026-01-01T00:00:00Z".to_string(),
            has_authoritative_created_at: true,
            created_by_account_id: None,
            search_visibility: Default::default(),
            summary: Some("Test summary".to_string()),
            info: Some("Test info".to_string()),
            tags: tags.into_iter().map(String::from).collect(),
            address: Some("Test str. 1".to_string()),
            location: Location {
                lat: 52.5,
                lon: 13.4,
            },
        }
    }

    fn candidate(id: &str, embedding: Option<Vec<f64>>) -> ScoredNode {
        ScoredNode {
            node: test_node(id, id, vec![]),
            rank_class: 99,
            rank_score: -1.0,
            embedding,
        }
    }

    fn vector_for_cosine(cosine: f64) -> Vec<f64> {
        vec![cosine, (1.0 - cosine * cosine).sqrt()]
    }

    fn rank_hybrid_reference(
        query_vector: Option<&[f64]>,
        lexical_candidates: Vec<ScoredNode>,
        all_eligible_candidates: &[ScoredNode],
        semantic_minimum_cosine: f64,
        semantic_minimum_margin: f64,
    ) -> Vec<ScoredNode> {
        let mut result = lexical_candidates;
        let Some(q_vec) = query_vector else {
            return result;
        };

        let lexical_ids: HashSet<String> = result.iter().map(|item| item.node.id.clone()).collect();
        let mut semantic_candidates: Vec<(ScoredNode, f64)> = Vec::new();

        for candidate in all_eligible_candidates {
            if lexical_ids.contains(&candidate.node.id) {
                continue;
            }
            if let Some(c_vec) = &candidate.embedding {
                let sim = cosine_similarity(q_vec, c_vec);
                if sim >= semantic_minimum_cosine {
                    let mut scored = candidate.clone();
                    scored.rank_class = 6;
                    scored.rank_score = sim;
                    semantic_candidates.push((scored, sim));
                }
            }
        }

        semantic_candidates.sort_by(|a, b| {
            b.1.partial_cmp(&a.1)
                .unwrap_or(Ordering::Equal)
                .then_with(|| a.0.node.id.cmp(&b.0.node.id))
        });

        if let Some((top_candidate, top_score)) = semantic_candidates.first() {
            let margin_ok = semantic_candidates.get(1).is_none_or(|(_, second_score)| {
                (top_score - second_score) >= semantic_minimum_margin
            });
            if margin_ok {
                result.push(top_candidate.clone());
            }
        }

        result
    }

    fn assert_rankings_equal(actual: &[ScoredNode], expected: &[ScoredNode], case: usize) {
        assert_eq!(
            actual.len(),
            expected.len(),
            "candidate count in case {case}"
        );
        for (position, (actual, expected)) in actual.iter().zip(expected).enumerate() {
            assert_eq!(
                actual.node, expected.node,
                "node at position {position} in case {case}"
            );
            assert_eq!(
                actual.rank_class, expected.rank_class,
                "rank class at position {position} in case {case}"
            );
            assert_eq!(
                actual.rank_score.to_bits(),
                expected.rank_score.to_bits(),
                "rank score at position {position} in case {case}"
            );
            assert_eq!(
                actual.embedding, expected.embedding,
                "embedding at position {position} in case {case}"
            );
        }
    }

    struct DeterministicRng(u64);

    impl DeterministicRng {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            self.0
        }

        fn index(&mut self, upper: usize) -> usize {
            (self.next() % upper as u64) as usize
        }
    }

    #[test]
    fn query_embedding_text_matches_t004_reference_contract() {
        let query = SearchQuery::new("Hilfe mit dem Telefon");
        assert_eq!(
            query.embedding_text(),
            "Aufgabe: Finde den relevantesten sichtbaren Weltgewebe-Knoten.\nAnfrage: Hilfe mit dem Telefon"
        );
    }

    #[test]
    fn hybrid_ranking_appends_at_most_one_semantic_candidate() {
        let lexical = vec![ScoredNode {
            node: test_node("node-lexical", "Fahrrad", vec![]),
            rank_class: 0,
            rank_score: 1.0,
            embedding: Some(vec![1.0, 0.0]),
        }];

        let eligible = vec![
            lexical[0].clone(),
            ScoredNode {
                node: test_node("node-sem-1", "Radhilfe", vec![]),
                rank_class: 99,
                rank_score: 0.0,
                embedding: Some(vec![0.9, 0.1]),
            },
            ScoredNode {
                node: test_node("node-sem-2", "Zweirad", vec![]),
                rank_class: 99,
                rank_score: 0.0,
                embedding: Some(vec![0.8, 0.2]),
            },
        ];

        let query_vec = vec![1.0, 0.0];
        let ranked = rank_hybrid(Some(&query_vec), lexical, &eligible, 0.55, 0.015);

        // Lexical candidate plus AT MOST ONE semantic candidate = 2 candidates total
        assert_eq!(ranked.len(), 2);
        assert_eq!(ranked[0].node.id, "node-lexical");
        assert_eq!(ranked[1].node.id, "node-sem-1");
    }

    #[test]
    fn semantic_ties_use_node_id_ascending_and_still_obey_margin() {
        let eligible = vec![
            candidate("node-z", Some(vec![1.0, 0.0])),
            candidate("node-a", Some(vec![1.0, 0.0])),
            candidate("node-m", Some(vec![1.0, 0.0])),
        ];

        let accepted = rank_hybrid(Some(&[1.0, 0.0]), vec![], &eligible, 0.55, 0.0);
        assert_eq!(accepted.len(), 1);
        assert_eq!(accepted[0].node.id, "node-a");

        let rejected = rank_hybrid(Some(&[1.0, 0.0]), vec![], &eligible, 0.55, 0.015);
        assert!(rejected.is_empty());
    }

    #[test]
    fn semantic_threshold_is_inclusive_at_the_boundary() {
        let embedding = vector_for_cosine(0.55);
        let threshold = cosine_similarity(&[1.0, 0.0], &embedding);
        let eligible = vec![candidate("boundary", Some(embedding))];

        let accepted = rank_hybrid(Some(&[1.0, 0.0]), vec![], &eligible, threshold, 0.015);
        assert_eq!(accepted.len(), 1);
        assert_eq!(accepted[0].node.id, "boundary");

        let rejected = rank_hybrid(
            Some(&[1.0, 0.0]),
            vec![],
            &eligible,
            threshold + f64::EPSILON,
            0.015,
        );
        assert!(rejected.is_empty());
    }

    #[test]
    fn semantic_margin_is_inclusive_at_the_boundary() {
        let top_embedding = vector_for_cosine(1.0);
        let runner_up_embedding = vector_for_cosine(0.8);
        let margin = cosine_similarity(&[1.0, 0.0], &top_embedding)
            - cosine_similarity(&[1.0, 0.0], &runner_up_embedding);
        let eligible = vec![
            candidate("top", Some(top_embedding)),
            candidate("runner-up", Some(runner_up_embedding)),
        ];

        let accepted = rank_hybrid(Some(&[1.0, 0.0]), vec![], &eligible, 0.55, margin);
        assert_eq!(accepted.len(), 1);
        assert_eq!(accepted[0].node.id, "top");

        let rejected = rank_hybrid(
            Some(&[1.0, 0.0]),
            vec![],
            &eligible,
            0.55,
            margin + f64::EPSILON,
        );
        assert!(rejected.is_empty());
    }

    #[test]
    fn below_threshold_candidate_is_excluded_from_margin_gate() {
        let eligible = vec![
            candidate("top", Some(vector_for_cosine(0.56))),
            candidate("below-threshold", Some(vector_for_cosine(0.54))),
        ];

        let ranked = rank_hybrid(Some(&[1.0, 0.0]), vec![], &eligible, 0.55, 0.5);

        assert_eq!(ranked.len(), 1);
        assert_eq!(ranked[0].node.id, "top");
    }

    #[test]
    fn lexical_candidates_are_excluded_without_changing_the_prefix() {
        let mut lexical_first = candidate("lexical-first", Some(vec![1.0, 0.0]));
        lexical_first.rank_class = 4;
        lexical_first.rank_score = 0.25;
        let mut lexical_second = candidate("lexical-second", Some(vec![1.0, 0.0]));
        lexical_second.rank_class = 1;
        lexical_second.rank_score = 0.75;
        let lexical = vec![lexical_first.clone(), lexical_second.clone()];
        let eligible = vec![
            candidate("semantic-runner-up", Some(vector_for_cosine(0.6))),
            lexical_second,
            candidate("semantic-top", Some(vector_for_cosine(0.8))),
            lexical_first,
        ];

        let ranked = rank_hybrid(Some(&[1.0, 0.0]), lexical.clone(), &eligible, 0.55, 0.1);

        assert_eq!(ranked.len(), lexical.len() + SEMANTIC_APPEND_LIMIT);
        assert_eq!(ranked[0].node, lexical[0].node);
        assert_eq!(ranked[0].rank_class, lexical[0].rank_class);
        assert_eq!(ranked[0].rank_score, lexical[0].rank_score);
        assert_eq!(ranked[1].node, lexical[1].node);
        assert_eq!(ranked[1].rank_class, lexical[1].rank_class);
        assert_eq!(ranked[1].rank_score, lexical[1].rank_score);
        assert_eq!(ranked[2].node.id, "semantic-top");
        assert_eq!(ranked[2].rank_class, 6);
    }

    #[test]
    fn streaming_top_two_matches_reference_full_sort_over_generated_candidates() {
        const THRESHOLDS: [f64; 6] = [-1.0, 0.0, 0.3, 0.55, 0.9, 1.0];
        const MARGINS: [f64; 6] = [0.0, 0.015, 0.1, 0.5, 1.0, 2.0];

        let query = [3.0, -2.0, 1.0];
        let mut rng = DeterministicRng(0x1832_0001_5eed_f00d);

        for case in 0..512 {
            let candidate_count = rng.index(48);
            let mut eligible = Vec::with_capacity(candidate_count);
            for position in 0..candidate_count {
                let id_order = rng.index(128);
                let id = format!("node-{id_order:03}-{position:03}");
                let embedding = match rng.index(10) {
                    0 => None,
                    1 => Some(vec![]),
                    2 => Some(vec![rng.index(9) as f64 - 4.0; 2]),
                    3 => Some(vec![0.0, 0.0, 0.0]),
                    _ => Some(
                        (0..3)
                            .map(|_| rng.index(9) as f64 - 4.0)
                            .collect::<Vec<_>>(),
                    ),
                };
                eligible.push(candidate(&id, embedding));
            }

            let lexical = eligible
                .iter()
                .enumerate()
                .filter(|(position, _)| (position + case) % 7 == 0)
                .map(|(position, item)| {
                    let mut lexical = item.clone();
                    lexical.rank_class = (position % 5) as u8;
                    lexical.rank_score = position as f64 / 10.0;
                    lexical
                })
                .collect::<Vec<_>>();
            let threshold = THRESHOLDS[rng.index(THRESHOLDS.len())];
            let margin = MARGINS[rng.index(MARGINS.len())];

            let actual = rank_hybrid(Some(&query), lexical.clone(), &eligible, threshold, margin);
            let expected =
                rank_hybrid_reference(Some(&query), lexical, &eligible, threshold, margin);

            assert_rankings_equal(&actual, &expected, case);
        }
    }
}
