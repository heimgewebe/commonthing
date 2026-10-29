//! T006 hybrid-order contract tests.
//!
//! PostgreSQL authorization/retrieval is proven in db_semantic_search_projection_worker.rs.
//! These tests deliberately avoid creating a second in-memory search truth.

use weltgewebe_api::{
    routes::nodes::{Location, Node},
    search::{rank_hybrid, ScoredNode},
};

fn candidate(id: &str, rank_class: u8, rank_score: f64, embedding: [f64; 2]) -> ScoredNode {
    ScoredNode {
        node: Node {
            id: id.to_string(),
            kind: "place".to_string(),
            title: id.to_string(),
            created_at: "2026-07-22T00:00:00Z".to_string(),
            updated_at: "2026-07-22T00:00:00Z".to_string(),
            has_authoritative_created_at: true,
            created_by_account_id: None,
            search_visibility: Default::default(),
            summary: None,
            info: None,
            tags: vec![],
            address: None,
            location: Location { lat: 0.0, lon: 0.0 },
        },
        rank_class,
        rank_score,
        embedding: Some(embedding.to_vec()),
    }
}

#[test]
fn semantic_append_preserves_authoritative_postgres_lexical_order() {
    let lexical = vec![
        candidate("lexical-first", 0, 1.0, [0.0, 1.0]),
        candidate("lexical-second", 4, 0.4, [0.0, 1.0]),
    ];
    let mut candidates = lexical.clone();
    candidates.push(candidate("semantic", u8::MAX, 0.0, [1.0, 0.0]));

    let ranked = rank_hybrid(Some(&[1.0, 0.0]), lexical, &candidates, 0.8, 0.05);
    let ids = ranked
        .iter()
        .map(|candidate| candidate.node.id.as_str())
        .collect::<Vec<_>>();

    assert_eq!(ids, vec!["lexical-first", "lexical-second", "semantic"]);
}

#[test]
fn semantic_append_is_bounded_to_one_candidate() {
    let lexical = vec![candidate("lexical", 0, 1.0, [0.0, 1.0])];
    let mut candidates = lexical.clone();
    candidates.push(candidate("semantic-a", u8::MAX, 0.0, [1.0, 0.0]));
    candidates.push(candidate("semantic-b", u8::MAX, 0.0, [1.0, 0.0]));

    let ranked = rank_hybrid(Some(&[1.0, 0.0]), lexical, &candidates, 0.8, 0.0);

    assert_eq!(ranked.len(), 2);
    assert_eq!(ranked[0].node.id, "lexical");
    assert!(matches!(
        ranked[1].node.id.as_str(),
        "semantic-a" | "semantic-b"
    ));
}
