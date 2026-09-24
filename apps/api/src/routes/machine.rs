use std::sync::LazyLock;

use axum::{
    extract::{Path, State},
    http::{
        header::{CONTENT_TYPE, IF_MATCH},
        HeaderMap, HeaderValue, StatusCode,
    },
    middleware::from_fn,
    response::{IntoResponse, Response},
    routing::{get, post},
    Extension, Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use uuid::Uuid;

use crate::{
    middleware::{auth::AuthContext, authz::require_authenticated},
    state::ApiState,
    telemetry::BuildInfo,
};

use super::{
    auth::SESSION_COOKIE_NAME,
    collective_write_guard::{
        create_node_serialized, delete_node_serialized, patch_node_serialized,
        replace_node_serialized,
    },
    nodes::UpdateNode,
};

pub const MACHINE_PROTOCOL: &str = "weltgewebe-machine/1";
const OPERATION_MANIFEST: &str = include_str!("../../../../contracts/machine/operations.json");

const DOMAIN_SCHEMAS: &[(&str, &str)] = &[
    (
        "account",
        include_str!("../../../../contracts/domain/account.schema.json"),
    ),
    (
        "conversation",
        include_str!("../../../../contracts/domain/conversation.schema.json"),
    ),
    (
        "edge",
        include_str!("../../../../contracts/domain/edge.schema.json"),
    ),
    (
        "message",
        include_str!("../../../../contracts/domain/message.schema.json"),
    ),
    (
        "node",
        include_str!("../../../../contracts/domain/node.schema.json"),
    ),
    (
        "role",
        include_str!("../../../../contracts/domain/role.schema.json"),
    ),
];

const FEDERATION_SCHEMAS: &[(&str, &str, &str)] = &[
    (
        "cell-descriptor",
        "cell_descriptor",
        include_str!("../../../../contracts/federation/v1/cell-descriptor.schema.json"),
    ),
    (
        "event",
        "event",
        include_str!("../../../../contracts/federation/v1/event.schema.json"),
    ),
];

#[derive(Clone, Debug, Deserialize)]
struct MachineManifest {
    schema_version: u8,
    contract: String,
    operations: Vec<ApiOperation>,
}

#[derive(Clone, Debug, Deserialize)]
struct ApiOperation {
    method: String,
    path: String,
    auth: String,
    write_safety: String,
}

static MACHINE_MANIFEST: LazyLock<Result<MachineManifest, String>> =
    LazyLock::new(|| serde_json::from_str(OPERATION_MANIFEST).map_err(|error| error.to_string()));

static BUILD_INFO: LazyLock<BuildInfo> = LazyLock::new(BuildInfo::collect);

static OPENAPI_DOCUMENT: LazyLock<Result<Value, String>> = LazyLock::new(|| {
    let manifest = machine_manifest().map_err(|error| error.to_string())?;
    build_openapi_document(manifest)
});

static OPENAPI_BODY: LazyLock<Result<String, String>> = LazyLock::new(|| {
    let document = openapi_document().map_err(|error| error.to_string())?;
    serde_json::to_string(document).map_err(|error| error.to_string())
});

static MACHINE_DESCRIPTOR_BODY: LazyLock<Result<String, String>> = LazyLock::new(|| {
    let manifest = machine_manifest().map_err(|error| error.to_string())?;
    serde_json::to_string(&machine_descriptor_document(manifest, &BUILD_INFO))
        .map_err(|error| error.to_string())
});

pub fn api_routes() -> Router<ApiState> {
    Router::new()
        .route(
            "/machine/v1/nodes",
            post(create_machine_node).route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/machine/v1/nodes/{id}",
            axum::routing::patch(patch_machine_node)
                .put(replace_machine_node)
                .delete(delete_machine_node)
                .route_layer(from_fn(require_authenticated)),
        )
}

pub fn root_routes() -> Router<ApiState> {
    Router::new()
        .route("/.well-known/weltgewebe", get(machine_descriptor))
        .route("/openapi.json", get(openapi))
        .route("/schemas/domain/{name}", get(domain_schema))
        .route("/schemas/federation/v1/{name}", get(federation_schema))
}

fn machine_problem(status: StatusCode, code: &'static str, message: &'static str) -> Response {
    (
        status,
        Json(json!({
            "code": code,
            "message": message,
            "contract": MACHINE_PROTOCOL,
        })),
    )
        .into_response()
}

fn canonical_operation_id(payload: &Value) -> bool {
    let Some(raw) = payload.get("operation_id").and_then(Value::as_str) else {
        return false;
    };

    Uuid::parse_str(raw)
        .ok()
        .is_some_and(|parsed| parsed.to_string() == raw)
}

fn missing_if_match_response(headers: &HeaderMap) -> Option<Response> {
    if headers.get(IF_MATCH).is_some() {
        return None;
    }

    Some(machine_problem(
        StatusCode::PRECONDITION_REQUIRED,
        "machine_if_match_required",
        "Machine node mutations require If-Match with the current node ETag.",
    ))
}

async fn create_machine_node(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Response {
    if !canonical_operation_id(&payload) {
        return machine_problem(
            StatusCode::UNPROCESSABLE_ENTITY,
            "machine_operation_id_required",
            "Machine node creation requires a canonical lowercase UUID operation_id.",
        );
    }

    create_node_serialized(State(state), Extension(auth), Json(payload)).await
}

async fn patch_machine_node(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(payload): Json<UpdateNode>,
) -> Response {
    if let Some(response) = missing_if_match_response(&headers) {
        return response;
    }

    patch_node_serialized(
        State(state),
        Extension(auth),
        Path(id),
        headers,
        Json(payload),
    )
    .await
}

async fn replace_machine_node(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Response {
    if let Some(response) = missing_if_match_response(&headers) {
        return response;
    }

    replace_node_serialized(
        State(state),
        Extension(auth),
        Path(id),
        headers,
        Json(payload),
    )
    .await
}

async fn delete_machine_node(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    if let Some(response) = missing_if_match_response(&headers) {
        return response;
    }

    delete_node_serialized(State(state), Extension(auth), Path(id), headers).await
}

fn machine_manifest() -> Result<&'static MachineManifest, &'static str> {
    match &*MACHINE_MANIFEST {
        Ok(manifest) => Ok(manifest),
        Err(error) => Err(error.as_str()),
    }
}

fn openapi_document() -> Result<&'static Value, &'static str> {
    match &*OPENAPI_DOCUMENT {
        Ok(document) => Ok(document),
        Err(error) => Err(error.as_str()),
    }
}

fn openapi_body() -> Result<&'static str, &'static str> {
    match &*OPENAPI_BODY {
        Ok(body) => Ok(body.as_str()),
        Err(error) => Err(error.as_str()),
    }
}

fn machine_descriptor_body() -> Result<&'static str, &'static str> {
    match &*MACHINE_DESCRIPTOR_BODY {
        Ok(body) => Ok(body.as_str()),
        Err(error) => Err(error.as_str()),
    }
}

async fn machine_descriptor() -> Response {
    match machine_descriptor_body() {
        Ok(body) => json_static_response(body),
        Err(error) => {
            tracing::error!(%error, "failed to build machine descriptor");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

fn machine_descriptor_document(manifest: &MachineManifest, build: &BuildInfo) -> Value {
    json!({
        "protocol": manifest.contract.as_str(),
        "schema_version": manifest.schema_version,
        "build": {
            "version": build.version,
            "commit": build.commit,
            "build_timestamp": build.build_timestamp,
        },
        "api": {
            "canonical_base": "/api",
            "openapi": "/openapi.json",
            "operation_count": manifest.operations.len(),
            "compatibility_root_alias": true,
        },
        "capabilities": [
            "api-discovery",
            "openapi-3.1",
            "json-schema-2020-12",
            "safe-node-writes-v1",
            "conditional-node-mutations",
            "idempotent-node-create",
            "federation-v1"
        ],
        "schemas": {
            "domain": domain_schema_links(),
            "federation": federation_schema_links(),
        },
        "write_profiles": {
            "safe-node-writes-v1": {
                "create": {
                    "method": "POST",
                    "path": "/api/machine/v1/nodes",
                    "operation_id": "required canonical lowercase UUID"
                },
                "mutate": {
                    "methods": ["PATCH", "PUT", "DELETE"],
                    "path": "/api/machine/v1/nodes/{id}",
                    "precondition": "If-Match required"
                },
                "authentication": SESSION_COOKIE_NAME,
                "csrf": "same-origin session write rules apply",
                "audit": "existing node mutation audit and actor binding apply"
            }
        },
        "federation": {
            "protocol": "wg-federation/1",
            "descriptor": "/federation/v1/cell",
            "events": "/federation/v1/events",
            "objects": "/federation/v1/objects"
        }
    })
}

fn domain_schema_links() -> Value {
    Value::Object(
        DOMAIN_SCHEMAS
            .iter()
            .map(|(name, _)| {
                (
                    (*name).to_string(),
                    Value::String(format!("/schemas/domain/{name}")),
                )
            })
            .collect(),
    )
}

fn federation_schema_links() -> Value {
    Value::Object(
        FEDERATION_SCHEMAS
            .iter()
            .map(|(name, descriptor_key, _)| {
                (
                    (*descriptor_key).to_string(),
                    Value::String(format!("/schemas/federation/v1/{name}")),
                )
            })
            .collect(),
    )
}

async fn openapi() -> Response {
    match openapi_body() {
        Ok(body) => json_static_response(body),
        Err(error) => {
            tracing::error!(%error, "failed to build OpenAPI machine contract");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

fn build_openapi_document(manifest: &MachineManifest) -> Result<Value, String> {
    let mut paths = Map::new();

    for operation in &manifest.operations {
        insert_openapi_operation(&mut paths, operation)?;
    }

    Ok(json!({
        "openapi": "3.1.0",
        "info": {
            "title": "commonThing Machine Surface",
            "version": manifest.schema_version.to_string(),
            "description": "Machine-discoverable operation surface. Handler-specific payloads are explicitly marked instead of guessed; canonical JSON Schemas are linked separately."
        },
        "servers": [{ "url": "/" }],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "sessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": SESSION_COOKIE_NAME
                }
            },
            "schemas": {
                "Account": { "$ref": "/schemas/domain/account" },
                "Conversation": { "$ref": "/schemas/domain/conversation" },
                "Edge": { "$ref": "/schemas/domain/edge" },
                "Message": { "$ref": "/schemas/domain/message" },
                "Node": { "$ref": "/schemas/domain/node" },
                "Role": { "$ref": "/schemas/domain/role" },
                "FederationCell": {
                    "$ref": "/schemas/federation/v1/cell-descriptor"
                },
                "FederationEvent": {
                    "$ref": "/schemas/federation/v1/event"
                }
            }
        },
        "x-weltgewebe-contract": manifest.contract.as_str(),
        "x-weltgewebe-completeness": "operation-surface-plus-core-schemas",
        "x-weltgewebe-discovery": "/.well-known/weltgewebe"
    }))
}

fn insert_openapi_operation(
    paths: &mut Map<String, Value>,
    operation: &ApiOperation,
) -> Result<(), String> {
    let item = paths
        .entry(operation.path.clone())
        .or_insert_with(|| Value::Object(Map::new()));
    let Some(item) = item.as_object_mut() else {
        return Err(format!(
            "OpenAPI path {} was not represented as an object",
            operation.path
        ));
    };
    item.insert(
        operation.method.to_ascii_lowercase(),
        openapi_operation(operation),
    );
    Ok(())
}

fn openapi_operation(operation: &ApiOperation) -> Value {
    let mut object = Map::new();
    object.insert(
        "operationId".to_string(),
        Value::String(operation_id(&operation.method, &operation.path)),
    );
    object.insert(
        "summary".to_string(),
        Value::String(format!("{} {}", operation.method, operation.path)),
    );
    object.insert(
        "responses".to_string(),
        json!({
            "200": { "description": "Successful response" },
            "default": { "description": "Error response" }
        }),
    );
    object.insert(
        "x-weltgewebe-auth".to_string(),
        Value::String(operation.auth.clone()),
    );
    object.insert(
        "x-weltgewebe-write-safety".to_string(),
        Value::String(operation.write_safety.clone()),
    );

    let mut parameters = path_parameters(&operation.path);
    if operation.write_safety == "if_match_required" {
        parameters.push(json!({
            "name": "If-Match",
            "in": "header",
            "required": true,
            "description": "Current node ETag. Prevents lost updates.",
            "schema": { "type": "string" }
        }));
    }
    if !parameters.is_empty() {
        object.insert("parameters".to_string(), Value::Array(parameters));
    }

    if matches!(
        operation.auth.as_str(),
        "session" | "admin" | "write_role" | "handler_enforced"
    ) {
        object.insert("security".to_string(), json!([{ "sessionCookie": [] }]));
    }

    if operation.method == "POST" && operation.write_safety == "operation_id_required" {
        object.insert(
            "requestBody".to_string(),
            json!({
                "required": true,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["operation_id"],
                            "properties": {
                                "operation_id": {
                                    "type": "string",
                                    "format": "uuid"
                                }
                            },
                            "additionalProperties": true
                        }
                    }
                }
            }),
        );
    }

    if operation.method == "POST" && operation.path == "/federation/v1/events" {
        object.insert(
            "requestBody".to_string(),
            json!({
                "required": true,
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "/schemas/federation/v1/event"
                        }
                    }
                }
            }),
        );
    }

    Value::Object(object)
}

fn operation_id(method: &str, path: &str) -> String {
    let normalized: String = path
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '_'
            }
        })
        .collect();

    format!(
        "{}_{}",
        method.to_ascii_lowercase(),
        normalized.trim_matches('_')
    )
}

fn path_parameters(path: &str) -> Vec<Value> {
    path.split('/')
        .filter_map(|segment| segment.strip_prefix('{')?.strip_suffix('}'))
        .map(|name| {
            json!({
                "name": name,
                "in": "path",
                "required": true,
                "schema": { "type": "string" }
            })
        })
        .collect()
}

async fn domain_schema(Path(name): Path<String>) -> Response {
    schema_response(domain_schema_text(&name))
}

async fn federation_schema(Path(name): Path<String>) -> Response {
    schema_response(federation_schema_text(&name))
}

fn json_static_response(body: &'static str) -> Response {
    let mut response = body.into_response();
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    response
}

fn schema_response(schema: Option<&'static str>) -> Response {
    match schema {
        Some(schema) => json_static_response(schema),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

fn domain_schema_text(name: &str) -> Option<&'static str> {
    DOMAIN_SCHEMAS
        .iter()
        .find_map(|(schema_name, schema)| (*schema_name == name).then_some(*schema))
}

fn federation_schema_text(name: &str) -> Option<&'static str> {
    FEDERATION_SCHEMAS
        .iter()
        .find_map(|(schema_name, _, schema)| (*schema_name == name).then_some(*schema))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use serde_json::Value;

    use super::{
        machine_manifest, openapi_document, DOMAIN_SCHEMAS, FEDERATION_SCHEMAS, MACHINE_PROTOCOL,
    };

    /// Best-effort source smoke guard for literal router path declarations.
    /// It is intentionally not treated as a complete Rust/Axum parser; runtime
    /// security tests independently cover all mutating routes.
    fn route_paths(source: &str) -> BTreeSet<String> {
        let mut paths = BTreeSet::new();
        let mut rest = source;
        let marker = concat!(".rou", "te(");

        while let Some(index) = rest.find(marker) {
            rest = &rest[index + marker.len()..];
            let trimmed = rest.trim_start();
            let Some(quoted) = trimmed.strip_prefix('"') else {
                continue;
            };
            let Some(end) = quoted.find('"') else {
                break;
            };
            let candidate = &quoted[..end];
            if candidate.starts_with('/') {
                paths.insert(candidate.to_string());
            }
            rest = &quoted[end + 1..];
        }

        paths
    }

    fn openapi_paths(document: &Value) -> BTreeSet<String> {
        document["paths"]
            .as_object()
            .expect("OpenAPI paths must be an object")
            .keys()
            .cloned()
            .collect()
    }

    #[test]
    fn operation_manifest_is_versioned_and_unique() {
        let manifest = machine_manifest().expect("machine manifest must parse");
        assert_eq!(manifest.schema_version, 1);
        assert_eq!(manifest.contract, MACHINE_PROTOCOL);

        let unique: BTreeSet<_> = manifest
            .operations
            .iter()
            .map(|operation| (&operation.method, &operation.path))
            .collect();
        assert_eq!(unique.len(), manifest.operations.len());
    }

    #[test]
    fn literal_router_paths_are_covered_by_operation_manifest() {
        let manifest = machine_manifest().expect("machine manifest must parse");
        let documented: BTreeSet<_> = manifest
            .operations
            .iter()
            .map(|operation| operation.path.as_str())
            .collect();

        for path in route_paths(include_str!("mod.rs"))
            .into_iter()
            .filter(|path| !path.contains("/testing/"))
        {
            assert!(
                documented.contains(format!("/api{path}").as_str()),
                "API router path {path} is missing from machine manifest"
            );
        }

        for path in route_paths(include_str!("machine.rs")) {
            let documented_path = if path.starts_with("/machine/") {
                format!("/api{path}")
            } else {
                path.clone()
            };
            assert!(
                documented.contains(documented_path.as_str()),
                "machine router path {path} is missing from machine manifest"
            );
        }
    }

    #[test]
    fn openapi_contains_every_manifest_operation() {
        let manifest = machine_manifest().expect("machine manifest must parse");
        let document = openapi_document().expect("OpenAPI contract must build");
        let paths = document["paths"]
            .as_object()
            .expect("OpenAPI paths must be an object");

        for operation in &manifest.operations {
            let item = paths
                .get(&operation.path)
                .expect("manifest path missing from OpenAPI");
            let method = operation.method.to_ascii_lowercase();
            assert!(
                item.get(&method).is_some(),
                "{} {} is missing from OpenAPI",
                operation.method,
                operation.path
            );
        }
    }

    #[test]
    fn machine_mutations_have_explicit_safety_contracts() {
        let document = openapi_document().expect("OpenAPI contract must build");
        let create = &document["paths"]["/api/machine/v1/nodes"]["post"];
        assert_eq!(create["x-weltgewebe-write-safety"], "operation_id_required");

        let node = &document["paths"]["/api/machine/v1/nodes/{id}"];
        for method in ["patch", "put", "delete"] {
            assert_eq!(
                node[method]["x-weltgewebe-write-safety"],
                "if_match_required"
            );
            let parameters = node[method]["parameters"]
                .as_array()
                .expect("machine mutation must describe If-Match");
            assert!(parameters.iter().any(|parameter| {
                parameter["name"] == "If-Match"
                    && parameter["in"] == "header"
                    && parameter["required"] == true
            }));
        }
    }

    #[test]
    fn embedded_schemas_are_valid_json() {
        for (name, schema) in DOMAIN_SCHEMAS {
            serde_json::from_str::<Value>(schema)
                .unwrap_or_else(|error| panic!("domain schema {name} is invalid JSON: {error}"));
        }
        for (name, _, schema) in FEDERATION_SCHEMAS {
            serde_json::from_str::<Value>(schema).unwrap_or_else(|error| {
                panic!("federation schema {name} is invalid JSON: {error}")
            });
        }
    }

    #[test]
    fn openapi_contract_is_versioned() {
        let document = openapi_document().expect("OpenAPI contract must build");
        assert_eq!(document["openapi"], "3.1.0");
        assert_eq!(document["x-weltgewebe-contract"], MACHINE_PROTOCOL);
    }

    #[test]
    fn openapi_paths_are_nonempty() {
        let document = openapi_document().expect("OpenAPI contract must build");
        assert!(!openapi_paths(document).is_empty());
    }
}
