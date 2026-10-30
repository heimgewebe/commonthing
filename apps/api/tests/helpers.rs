use std::path::Path;

use anyhow::Result;
use axum::{
    body,
    http::{Request, StatusCode},
    Router,
};
use tower::ServiceExt;
use weltgewebe_api::{
    routes::nodes::{Location as NodeLocation, Node},
    state::ApiState,
    test_helpers::EnvGuard,
};

pub const TEST_NODE_TIMESTAMP: &str = "2026-07-13T04:00:00Z";

#[allow(dead_code)]
pub fn set_gewebe_in_dir(dir: &Path) -> EnvGuard {
    EnvGuard::set(
        "GEWEBE_IN_DIR",
        dir.to_str()
            .expect("GEWEBE_IN_DIR must be valid UTF-8 for tests"),
    )
}

#[allow(dead_code)]
pub fn test_node(id: &str, title: &str, summary: Option<&str>) -> Node {
    Node {
        id: id.to_string(),
        kind: "resource".to_string(),
        title: title.to_string(),
        created_at: TEST_NODE_TIMESTAMP.to_string(),
        updated_at: TEST_NODE_TIMESTAMP.to_string(),
        has_authoritative_created_at: true,
        created_by_account_id: None,
        search_visibility: Default::default(),
        summary: summary.map(str::to_string),
        info: None,
        tags: vec![],
        address: None,
        location: Some(NodeLocation {
            lat: 53.55,
            lon: 10.0,
        }),
    }
}

#[allow(dead_code)]
pub async fn insert_test_node(state: &ApiState, node: Node) {
    state.nodes.write().await.insert(node.id.clone(), node);
}

#[allow(dead_code)]
pub async fn read_account_details(app: &Router, account_id: &str) -> Result<serde_json::Value> {
    let response = app
        .clone()
        .oneshot(
            Request::get(format!("/accounts/{account_id}"))
                .header("Host", "localhost")
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    Ok(serde_json::from_slice(&bytes)?)
}

#[allow(dead_code)]
pub fn assert_account_has_single_node_relation(
    details: &serde_json::Value,
    account_id: &str,
    node_id: &str,
    node_title: &str,
    edge_kind: &str,
    expected_event: &str,
) -> String {
    assert_eq!(details["id"], account_id, "unexpected account projection");
    assert_eq!(
        details["nodes"].as_array().map(Vec::len),
        Some(1),
        "account must project exactly one related node"
    );
    assert_eq!(details["nodes"][0]["node_id"], node_id);
    assert_eq!(details["nodes"][0]["node_title"], node_title);
    assert_eq!(details["nodes"][0]["edge_kind"], edge_kind);
    assert!(
        details["nodes"][0].get("note").is_none(),
        "public account relations must not expose free-text edge notes"
    );
    assert_eq!(
        details["activity"].as_array().map(Vec::len),
        Some(1),
        "account must project exactly one related activity"
    );
    assert_eq!(details["activity"][0]["event"], expected_event);
    details["activity"][0]["date"]
        .as_str()
        .expect("projected activity date must be a string")
        .to_string()
}
