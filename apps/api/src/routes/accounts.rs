use super::{
    domain_write_guard::reject_account_create_unless_writable,
    edges::edge_is_active_at,
    nodes::{node_readable_to, reader_account_id},
    query::{
        cursor_page, parse_cursor_params, parse_usize_param, validate_cursor_limit, ListResponse,
        MAX_PAGE_SIZE,
    },
};
use crate::auth::{accounts::AccountStore, role::Role};
use crate::config::{DomainAccountWriteSource, DomainReadSource};
use crate::domain_db::{
    insert_account_from_jsonl_record, load_account_profile_from_postgres,
    update_account_profile_in_postgres, AccountProfileUpdate, AccountProfileUpdateError,
    AccountWriteError,
};
use crate::middleware::auth::AuthContext;
use crate::state::ApiState;
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    Extension, Json,
};
use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    env,
    path::PathBuf,
};
use tokio::{
    fs::{File, OpenOptions},
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
};
use uuid::Uuid;

const EARTH_RADIUS_M: f64 = 6_371_008.8;
const LOCATION_MATCH_EPSILON_DEGREES: f64 = 1e-9;
const POLE_COSINE_EPSILON: f64 = 1e-12;
const RADIUS_DISTANCE_EPSILON_M: f64 = 0.05;
const MIN_RADIUS_PUBLIC_OFFSET_M: f64 = 0.01;
const UNIT_F64_SHIFT: u32 = 11;
const UNIT_F64_SCALE: f64 = 9_007_199_254_740_992.0; // 2^53
pub(crate) const MIN_RADIUS_M: u32 = 50;
pub(crate) const MAX_RADIUS_M: u32 = 5_000;
const RADIUS_PROJECTION_VERSION: u8 = 1;
pub(crate) const RADIUS_PROJECTION_KEY: &str = "radius_projection";

fn in_dir() -> PathBuf {
    env::var("GEWEBE_IN_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(".gewebe/in"))
}

fn accounts_path() -> PathBuf {
    in_dir().join("demo.accounts.jsonl")
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Location {
    pub lat: f64,
    pub lon: f64,
}

/// Public view of an Account.
/// STRICTLY does not contain the internal exact `location` (residence).
/// `map_state` and `public_pos` express visibility without creating a second
/// identity type. Removed identity and visibility fields are rejected.
#[derive(Serialize, Deserialize, Debug, PartialEq, Clone, Copy)]
#[serde(rename_all = "snake_case")]
pub enum GarnrolleMapState {
    NotOnMap,
    Exact,
    Radius,
}
#[derive(Serialize, Clone, Debug)]
pub struct AccountPublic {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,

    // Privacy: 'location' field is intentionally omitted.
    // 'public_pos' is the only projected location for public consumption.
    #[serde(skip_serializing_if = "Option::is_none", rename = "public_pos")]
    pub public_pos: Option<Location>,

    pub map_state: GarnrolleMapState,
    pub radius_m: u32,

    #[serde(default, skip_serializing)]
    pub disabled: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub tags: Vec<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct AccountNodeRelation {
    pub node_id: String,
    pub node_title: String,
    pub node_kind: String,
    pub edge_kind: String,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct AccountActivity {
    pub date: String,
    pub event: String,
}

#[derive(Serialize, Clone, Debug)]
pub struct AccountDetails {
    #[serde(flatten)]
    pub account: AccountPublic,
    pub nodes: Vec<AccountNodeRelation>,
    pub activity: Vec<AccountActivity>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct OwnGarnrolleProfile {
    pub id: String,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    pub tags: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub address: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location: Option<Location>,
    pub map_state: GarnrolleMapState,
    pub radius_m: u32,
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct UpdateOwnGarnrolleRequest {
    pub title: String,
    #[serde(default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub address: Option<String>,
    #[serde(default)]
    pub clear_address: bool,
    pub map_state: GarnrolleMapState,
    #[serde(default)]
    pub radius_m: Option<u32>,
    #[serde(default)]
    pub location: Option<Location>,
    #[serde(default)]
    pub clear_location: bool,
}

#[derive(Clone, Debug)]
pub struct AccountInternal {
    pub public: AccountPublic,
    pub role: Role,
    pub email: Option<String>,
    /// Dedicated WebAuthn user identity for this account.
    /// This is NOT derived from `account_id` — it is an independent, opaque handle
    /// used exclusively by the WebAuthn protocol to identify the user.
    ///
    /// **Persistence:** read from the account data source when present. When absent
    /// (e.g. existing accounts loaded before this field was introduced), a fresh
    /// UUID v4 is generated at load time as a lazy backfill. This generated value
    /// is stable for the lifetime of the running process only. It becomes durable
    /// once written back to the data source — a prerequisite for `register/verify`
    /// that is not yet implemented.
    pub webauthn_user_id: Uuid,
}

/// Private, persisted binding for one radius projection.
///
/// The binding lives only in JSONL's private record or PostgreSQL
/// `private_payload`. It is never part of [`AccountPublic`]. The exact anchor is
/// duplicated deliberately: it lets every API instance verify that a stored
/// projection still belongs to the current private location and radius before
/// exposing the public point.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct RadiusProjectionBinding {
    version: u8,
    anchor: Location,
    radius_m: u32,
    public_pos: Location,
}

fn location_is_valid(location: &Location) -> bool {
    location.lat.is_finite()
        && location.lon.is_finite()
        && (-90.0..=90.0).contains(&location.lat)
        && (-180.0..=180.0).contains(&location.lon)
}

fn longitude_delta_degrees(a: f64, b: f64) -> f64 {
    let delta = (a - b).abs().rem_euclid(360.0);
    delta.min(360.0 - delta)
}

fn locations_match(a: &Location, b: &Location) -> bool {
    (a.lat - b.lat).abs() <= LOCATION_MATCH_EPSILON_DEGREES
        && longitude_delta_degrees(a.lon, b.lon) <= LOCATION_MATCH_EPSILON_DEGREES
}

fn great_circle_distance_m(a: &Location, b: &Location) -> f64 {
    let lat1 = a.lat.to_radians();
    let lat2 = b.lat.to_radians();
    let delta_lat = lat2 - lat1;
    let delta_lon = (b.lon - a.lon).to_radians();
    let haversine =
        (delta_lat / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * (delta_lon / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_M * haversine.clamp(0.0, 1.0).sqrt().asin()
}

fn destination_point(anchor: &Location, distance_m: f64, bearing_rad: f64) -> Location {
    let bearing_rad = bearing_rad.rem_euclid(std::f64::consts::TAU);
    let angular_distance = distance_m / EARTH_RADIUS_M;
    let lat1 = anchor.lat.to_radians();
    let lon1 = anchor.lon.to_radians();
    let sin_lat1 = lat1.sin();
    let cos_lat1 = lat1.cos();
    let sin_angular = angular_distance.sin();
    let cos_angular = angular_distance.cos();

    // The usual initial-bearing formula becomes 0/0 at the exact poles.
    // Handle those coordinates explicitly so longitude still carries the
    // random angular component instead of collapsing to the anchor meridian.
    if cos_lat1.abs() < POLE_COSINE_EPSILON {
        let lat2 = if anchor.lat.is_sign_positive() {
            std::f64::consts::FRAC_PI_2 - angular_distance
        } else {
            -std::f64::consts::FRAC_PI_2 + angular_distance
        };
        let lon2 = lon1 + bearing_rad;
        let lon_degrees = (lon2.to_degrees() + 180.0).rem_euclid(360.0) - 180.0;
        return Location {
            lat: lat2.to_degrees().clamp(-90.0, 90.0),
            lon: lon_degrees,
        };
    }

    let lat2 = (sin_lat1 * cos_angular + cos_lat1 * sin_angular * bearing_rad.cos())
        .clamp(-1.0, 1.0)
        .asin();
    let lon2 = lon1
        + (bearing_rad.sin() * sin_angular * cos_lat1).atan2(cos_angular - sin_lat1 * lat2.sin());
    let lon_degrees = (lon2.to_degrees() + 180.0).rem_euclid(360.0) - 180.0;

    Location {
        lat: lat2.to_degrees().clamp(-90.0, 90.0),
        lon: lon_degrees,
    }
}

fn cryptographic_unit_pair() -> (f64, f64) {
    // `Uuid::new_v4` is backed by the operating-system CSPRNG. Hashing removes
    // UUID version/variant bit structure before the bytes are mapped to two
    // independent unit-interval values.
    let digest = Sha256::digest(Uuid::new_v4().as_bytes());
    let mut first = [0_u8; 8];
    let mut second = [0_u8; 8];
    first.copy_from_slice(&digest[..8]);
    second.copy_from_slice(&digest[8..16]);
    // IEEE-754 f64 has 53 bits of integer precision. Taking the upper
    // 53 bits avoids rounding a u64 to 1.0 or introducing uneven buckets.
    let u =
        ((u64::from_be_bytes(first) >> UNIT_F64_SHIFT) as f64 / UNIT_F64_SCALE).max(f64::EPSILON);
    let v = (u64::from_be_bytes(second) >> UNIT_F64_SHIFT) as f64 / UNIT_F64_SCALE;
    (u, v)
}

fn generate_radius_projection(location: &Location, radius_m: u32) -> RadiusProjectionBinding {
    let (radial_sample, angular_sample) = cryptographic_unit_pair();
    // sqrt(u) gives a uniform area distribution over the circle. The tiny
    // lower bound excludes the exact residence even after floating-point
    // rounding without materially changing the area distribution.
    let distance_m = (f64::from(radius_m) * radial_sample.sqrt()).max(MIN_RADIUS_PUBLIC_OFFSET_M);
    let bearing_rad = std::f64::consts::TAU * angular_sample;
    RadiusProjectionBinding {
        version: RADIUS_PROJECTION_VERSION,
        anchor: location.clone(),
        radius_m,
        public_pos: destination_point(location, distance_m, bearing_rad),
    }
}

fn parse_radius_projection(value: &Value) -> Option<RadiusProjectionBinding> {
    serde_json::from_value(value.clone()).ok()
}

fn validated_radius_projection(
    value: Option<&Value>,
    location: &Location,
    radius_m: u32,
) -> Option<RadiusProjectionBinding> {
    if !(MIN_RADIUS_M..=MAX_RADIUS_M).contains(&radius_m) || !location_is_valid(location) {
        return None;
    }
    let binding = parse_radius_projection(value?)?;
    let distance_m = great_circle_distance_m(location, &binding.public_pos);
    // Unknown binding versions remain fail-closed. A future format needs an
    // explicit migration or compatibility decoder, never a broad <= acceptance.
    if binding.version != RADIUS_PROJECTION_VERSION
        || binding.radius_m != radius_m
        || !location_is_valid(&binding.anchor)
        || !location_is_valid(&binding.public_pos)
        || !locations_match(&binding.anchor, location)
        || distance_m < MIN_RADIUS_PUBLIC_OFFSET_M
        || distance_m > f64::from(radius_m) + RADIUS_DISTANCE_EPSILON_M
    {
        return None;
    }
    Some(binding)
}

pub(crate) fn radius_projection_public_pos(
    value: Option<&Value>,
    location: &Location,
    radius_m: u32,
) -> Option<Location> {
    validated_radius_projection(value, location, radius_m).map(|binding| binding.public_pos)
}

pub(crate) fn radius_projection_matches_location(
    value: Option<&Value>,
    location: &Location,
) -> bool {
    let Some(binding) = value.and_then(parse_radius_projection) else {
        return false;
    };
    validated_radius_projection(value, location, binding.radius_m).is_some()
}

pub(crate) fn ensure_radius_projection_value(
    existing: Option<&Value>,
    location: &Location,
    radius_m: u32,
) -> Value {
    let binding = validated_radius_projection(existing, location, radius_m)
        .unwrap_or_else(|| generate_radius_projection(location, radius_m));
    serde_json::to_value(binding).expect("radius projection serialization is infallible")
}

pub(crate) fn map_json_to_public_account(v: &Value) -> Option<AccountPublic> {
    let id = match v.get("id").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => {
            tracing::debug!("Skipping account with missing or invalid id");
            return None;
        }
    };

    let kind = match v.get("type").and_then(|value| value.as_str()) {
        Some("garnrolle") => "garnrolle",
        Some(other) => {
            tracing::warn!(%id, account_type = %other, "rejecting non-canonical account type");
            return None;
        }
        None => {
            tracing::warn!(%id, "rejecting account without canonical type");
            return None;
        }
    };

    for forbidden in ["mode", "ron_flag", "visibility", "suppress_public_pos"] {
        if v.get(forbidden).is_some() {
            tracing::warn!(%id, field = forbidden, "rejecting removed account field");
            return None;
        }
    }

    let title = v
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or("Untitled")
        .to_string();

    let summary = v
        .get("summary")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let mut lat = None;
    let mut lon = None;

    if let Some(location_obj) = v.get("location") {
        lon = location_obj.get("lon").and_then(|val| {
            val.as_f64()
                .or_else(|| val.as_str().and_then(|s| s.parse().ok()))
        });
        lat = location_obj.get("lat").and_then(|val| {
            val.as_f64()
                .or_else(|| val.as_str().and_then(|s| s.parse().ok()))
        });
    }

    let radius_raw = v.get("radius_m").and_then(|v| v.as_u64()).unwrap_or(0);
    let mut radius_m = match u32::try_from(radius_raw) {
        Ok(radius_m) => radius_m,
        Err(_) => {
            tracing::warn!(%id, radius_raw, "suppressing account with unsupported radius_m");
            0
        }
    };
    let invalid_radius = radius_raw > 0 && !(MIN_RADIUS_M..=MAX_RADIUS_M).contains(&radius_m);

    let explicit_map_state = match v.get("map_state").and_then(|value| value.as_str()) {
        Some(state @ ("not_on_map" | "exact" | "radius")) => state,
        Some(state) => {
            tracing::warn!(%id, %state, "rejecting account with invalid map_state");
            return None;
        }
        None => {
            tracing::warn!(%id, "rejecting account without map_state");
            return None;
        }
    };

    let internal_location = match (lat, lon) {
        (Some(lat), Some(lon)) => Some(Location { lat, lon }),
        _ => None,
    };
    let radius_requested = invalid_radius || explicit_map_state == "radius" || radius_m > 0;
    let radius_public_pos = if radius_requested && !invalid_radius {
        internal_location.as_ref().and_then(|location| {
            radius_projection_public_pos(v.get(RADIUS_PROJECTION_KEY), location, radius_m)
        })
    } else {
        None
    };

    let map_state = if explicit_map_state == "not_on_map" {
        GarnrolleMapState::NotOnMap
    } else if internal_location.is_none() {
        tracing::warn!(account_id = %id, "suppressing mapped account without an internal location");
        GarnrolleMapState::NotOnMap
    } else if radius_requested {
        if radius_public_pos.is_some() {
            GarnrolleMapState::Radius
        } else {
            tracing::warn!(
                account_id = %id,
                "suppressing radius account without a valid private projection binding"
            );
            GarnrolleMapState::NotOnMap
        }
    } else {
        GarnrolleMapState::Exact
    };

    if map_state == GarnrolleMapState::NotOnMap {
        radius_m = 0;
    }

    let disabled = v.get("disabled").and_then(|v| v.as_bool()).unwrap_or(false);

    let tags = v
        .get("tags")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let public_pos = match map_state {
        GarnrolleMapState::Exact => internal_location,
        GarnrolleMapState::Radius => radius_public_pos,
        GarnrolleMapState::NotOnMap => None,
    };

    Some(AccountPublic {
        id,
        kind: kind.to_string(),
        title,
        summary,
        public_pos,
        map_state,
        radius_m,
        disabled,
        tags,
    })
}

pub async fn load_all_accounts() -> std::io::Result<AccountStore> {
    let mut store = AccountStore::new();
    let path = accounts_path();

    let file = match File::open(&path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(store),
        Err(error) => return Err(error),
    };

    let mut lines = BufReader::new(file).lines();
    let mut line_number = 0usize;
    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let (value, public) = parse_jsonl_account_record(&path, line_number, &line)?;

        let role = value
            .get("role")
            .and_then(|value| value.as_str())
            .map(Role::from_str_lossy)
            .unwrap_or(Role::Gast);
        let email = value
            .get("email")
            .and_then(|value| value.as_str())
            .map(ToOwned::to_owned);
        let webauthn_user_id = value
            .get("webauthn_user_id")
            .and_then(|value| value.as_str())
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap_or_else(Uuid::new_v4);

        store.insert_unindexed(AccountInternal {
            public,
            role,
            email,
            webauthn_user_id,
        });
    }
    store.rebuild_email_index();
    Ok(store)
}

pub async fn list_accounts(
    State(state): State<ApiState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<ListResponse<AccountPublic>>, StatusCode> {
    let limit: usize = parse_usize_param(&params, "limit", 100)?.min(MAX_PAGE_SIZE);
    let (cursor_mode, after_id) = parse_cursor_params(&params)?;
    validate_cursor_limit(cursor_mode, limit)?;

    let accounts = state.accounts.read().await;

    if cursor_mode {
        // The AccountStore is a BTreeMap (already id-ascending); cursor_page
        // re-affirms the stable id-ascending contract shared by all cursor
        // endpoints and projects each webende Garnrolle to its public view.
        // Every authenticated account owns a Garnrolle. Public visibility remains
        // controlled by the profile map-state and disabled flag.
        let refs: Vec<&AccountInternal> = accounts
            .iter()
            .map(|(_id, internal)| internal)
            .filter(|internal| !internal.public.disabled)
            .collect();
        let page = cursor_page(
            refs,
            limit,
            after_id.as_deref(),
            |internal: &AccountInternal| internal.public.id.as_str(),
            |internal: &AccountInternal| internal.public.clone(),
        );
        Ok(Json(ListResponse::Cursor(page)))
    } else {
        let offset: usize = parse_usize_param(&params, "offset", 0)?;
        // BTreeMap iterates in ascending key order, so output is deterministic by account id.
        let accounts: Vec<AccountPublic> = accounts
            .iter()
            .filter(|(_id, internal)| !internal.public.disabled)
            .skip(offset)
            .take(limit)
            .map(|(_id, internal)| internal.public.clone())
            .collect();

        Ok(Json(ListResponse::Legacy(accounts)))
    }
}

async fn load_account_postgres_activity(
    state: &ApiState,
    account_id: &str,
    viewer_account_id: Option<&str>,
) -> Result<Vec<AccountActivity>, StatusCode> {
    let pool = match state.config.domain_read_source {
        DomainReadSource::Jsonl => return Ok(Vec::new()),
        DomainReadSource::Postgres => state.db_pool.as_ref().ok_or_else(|| {
            tracing::error!(
                account_id,
                "PostgreSQL is the canonical domain read source but no pool is available for account activity"
            );
            StatusCode::INTERNAL_SERVER_ERROR
        })?,
    };

    // Knoten creation is durable domain history while the creator identity is
    // still linked. Only the original created_at timestamp may date a creation;
    // updated_at is mutation history and must never be promoted into an invented
    // creation event. The live payload binding is authoritative. A narrow
    // timestamp-bound origin-Faden fallback also covers older rows that have no
    // payload binding. It deliberately requires the Faden to remain present:
    // guest exit deletes those Fäden, so a later account reusing the same text
    // id cannot inherit the departed person's history through create_actor_id.
    let node_rows = sqlx::query_as::<_, (DateTime<Utc>, String)>(
        "SELECT node.created_at, node.title \
         FROM domain_nodes AS node \
         WHERE node.created_at IS NOT NULL \
           AND (node.search_visibility = 'public' OR \
                (node.search_visibility IN ('private','hidden') \
                 AND $2::text IS NOT NULL \
                 AND weltgewebe_search_node_owner_account_id(node.payload) = $2::text)) \
           AND ( \
                NULLIF(BTRIM(node.payload ->> 'created_by_account_id'), '') = $1 \
                OR ( \
                NULLIF(BTRIM(node.payload ->> 'created_by_account_id'), '') IS NULL \
                AND ( \
                    NULLIF(BTRIM(node.create_actor_id), '') IS NULL \
                    OR NULLIF(BTRIM(node.create_actor_id), '') = $1 \
                ) \
                AND EXISTS ( \
                    SELECT 1 \
                    FROM domain_edges AS origin \
                    WHERE origin.source_id = $1 \
                      AND origin.target_id = node.id \
                      AND NULLIF(BTRIM(origin.payload ->> 'source_type'), '') = 'account' \
                      AND NULLIF(BTRIM(origin.payload ->> 'target_type'), '') = 'node' \
                      AND origin.created_at IS NOT NULL \
                      AND origin.created_at BETWEEN \
                          node.created_at - INTERVAL '1 second' \
                          AND node.created_at + INTERVAL '1 second' \
                      AND NOT EXISTS ( \
                          SELECT 1 \
                          FROM domain_conversations AS origin_conversation \
                          JOIN domain_messages AS origin_message \
                            ON origin_message.conversation_id = origin_conversation.id \
                          WHERE origin_conversation.node_id = node.id \
                            AND origin_message.author_account_id = $1 \
                            AND origin_message.created_at BETWEEN \
                                origin.created_at - INTERVAL '1 second' \
                                AND origin.created_at + INTERVAL '1 second' \
                      ) \
                      AND NOT EXISTS ( \
                          SELECT 1 \
                          FROM domain_edges AS earlier \
                          WHERE earlier.target_id = node.id \
                            AND NULLIF(BTRIM(earlier.payload ->> 'source_type'), '') = 'account' \
                            AND NULLIF(BTRIM(earlier.payload ->> 'target_type'), '') = 'node' \
                            AND earlier.created_at IS NOT NULL \
                            AND (earlier.created_at, earlier.id) < \
                                (origin.created_at, origin.id) \
                      ) \
                ) \
            ) \
           ) \
         ORDER BY node.created_at DESC, node.id DESC",
    )
    .bind(account_id)
    .bind(viewer_account_id)
    .fetch_all(pool)
    .await
    .map_err(|error| {
        tracing::error!(
            %error,
            account_id,
            "failed to load durable node-creation activity for account"
        );
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    // Individual Fäden are temporary hotspot projections. Conversation
    // messages are the durable action records, so aggregate same-day
    // contributions to avoid a wall of visually identical timeline entries.
    let contribution_rows = sqlx::query_as::<_, (DateTime<Utc>, String, i64)>(
        "SELECT MAX(message.created_at), \
                COALESCE(node.title, conversation.node_title_snapshot), \
                COUNT(*)::bigint \
         FROM domain_messages AS message \
         JOIN domain_conversations AS conversation \
           ON conversation.id = message.conversation_id \
         LEFT JOIN domain_nodes AS node ON node.id = conversation.node_id \
         WHERE message.author_account_id = $1 \
           AND conversation.deleted_at IS NULL \
           AND ( \
                (node.id IS NOT NULL AND \
                 (node.search_visibility = 'public' OR \
                  (node.search_visibility IN ('private','hidden') \
                   AND $2::text IS NOT NULL \
                   AND weltgewebe_search_node_owner_account_id(node.payload) = $2::text))) \
                OR (node.id IS NULL AND conversation.conversation_type = 'node' \
                    AND conversation.archived_node_visibility = 'public') \
               ) \
           AND COALESCE(node.title, conversation.node_title_snapshot) IS NOT NULL \
         GROUP BY conversation.id, \
                  COALESCE(node.title, conversation.node_title_snapshot), \
                  (message.created_at AT TIME ZONE 'UTC')::date \
         ORDER BY MAX(message.created_at) DESC, conversation.id DESC",
    )
    .bind(account_id)
    .bind(viewer_account_id)
    .fetch_all(pool)
    .await
    .map_err(|error| {
        tracing::error!(
            %error,
            account_id,
            "failed to load durable conversation activity for account"
        );
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let mut activity = Vec::with_capacity(node_rows.len() + contribution_rows.len());
    activity.extend(
        node_rows
            .into_iter()
            .map(|(activity_at, node_title)| AccountActivity {
                date: activity_at.to_rfc3339_opts(SecondsFormat::Micros, true),
                event: format!("Hat den Knoten \"{}\" geknüpft.", node_title),
            }),
    );
    activity.extend(contribution_rows.into_iter().map(
        |(latest_at, node_title, contribution_count)| AccountActivity {
            date: latest_at.to_rfc3339_opts(SecondsFormat::Micros, true),
            event: if contribution_count == 1 {
                format!(
                    "Hat einen Beitrag zum Gespräch über den Knoten \"{}\" geschrieben.",
                    node_title
                )
            } else {
                format!(
                    "Hat {} Beiträge zum Gespräch über den Knoten \"{}\" geschrieben.",
                    contribution_count, node_title
                )
            },
        },
    ));
    Ok(activity)
}

pub async fn get_account(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    auth: Option<Extension<AuthContext>>,
) -> Result<Json<AccountDetails>, StatusCode> {
    let viewer = auth.as_ref().map(|extension| &extension.0);
    let viewer_account_id = reader_account_id(viewer);
    // Clone the public profile before reading the relationship caches so this
    // endpoint never holds several state locks at once.
    let account = {
        let accounts = state.accounts.read().await;
        accounts
            .get(&id)
            .filter(|internal| !internal.public.disabled)
            .map(|internal| internal.public.clone())
            .ok_or(StatusCode::NOT_FOUND)?
    };

    // Active Fäden remain the source of truth for the current Knoten tab. They
    // are deliberately not an activity history: each Faden expires after 168
    // hours and several distinct actions may create parallel projections.
    let related_edges = {
        let now = Utc::now();
        let edges = state.edges.read().await;
        edges
            .iter_in_order()
            .filter(|edge| {
                ((edge.source_id == id
                    && matches!(edge.source_type.as_deref(), Some("account") | None))
                    || (edge.target_id == id
                        && matches!(edge.target_type.as_deref(), Some("account") | None)))
                    && edge_is_active_at(edge, now)
            })
            .cloned()
            .collect::<Vec<_>>()
    };

    // Build the current Knoten projection while holding only the node lock.
    // JSONL has no durable conversation store, but explicit node creator
    // metadata still provides a correct creation history.
    let (mut nodes, mut activity) = {
        let node_cache = state.nodes.read().await;
        let mut seen_nodes = HashSet::new();
        let mut nodes = Vec::new();

        for edge in related_edges {
            let (related_id, related_type) = if edge.source_id == id {
                (edge.target_id.as_str(), edge.target_type.as_deref())
            } else {
                (edge.source_id.as_str(), edge.source_type.as_deref())
            };
            if !matches!(related_type, Some("node") | None) {
                continue;
            }
            let Some(node) = node_cache.get(related_id) else {
                continue;
            };
            if !node_readable_to(node, viewer) {
                continue;
            }

            if seen_nodes.insert(node.id.clone()) {
                nodes.push(AccountNodeRelation {
                    node_id: node.id.clone(),
                    node_title: node.title.clone(),
                    node_kind: node.kind.clone(),
                    edge_kind: edge.edge_kind.clone(),
                });
            }
        }

        let activity = match state.config.domain_read_source {
            DomainReadSource::Jsonl => node_cache
                .iter_in_order()
                .filter(|node| {
                    node.has_authoritative_created_at
                        && node.created_by_account_id.as_deref() == Some(id.as_str())
                        && node_readable_to(node, viewer)
                })
                .map(|node| AccountActivity {
                    date: node.created_at.clone(),
                    event: format!("Hat den Knoten \"{}\" geknüpft.", node.title),
                })
                .collect(),
            DomainReadSource::Postgres => Vec::new(),
        };

        (nodes, activity)
    };

    // PostgreSQL can reconstruct the complete durable history, including
    // faded origin Fäden and correctly classified conversation contributions.
    activity.extend(load_account_postgres_activity(&state, &id, viewer_account_id).await?);

    nodes.sort_by(|left, right| {
        left.node_title
            .cmp(&right.node_title)
            .then_with(|| left.node_id.cmp(&right.node_id))
    });
    activity.sort_by(|left, right| {
        right
            .date
            .cmp(&left.date)
            .then_with(|| left.event.cmp(&right.event))
    });

    Ok(Json(AccountDetails {
        account,
        nodes,
        activity,
    }))
}

/// Parse a JSON value as f64, accepting either a number or a numeric string.
fn json_f64(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

/// Append a single account record as a JSONL line. Durability via fsync.
/// Callers MUST hold `state.accounts_persist` to serialize writes.
///
/// `pub(crate)` so the auth auto-provisioning path (`routes::auth`) can persist
/// through the exact same durable append used by the operator-facing
/// `POST /accounts` create path — no second, divergent JSONL writer.
pub(crate) async fn append_account_line(record: &Value) -> std::io::Result<()> {
    let path = accounts_path();
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let line = serde_json::to_string(record)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .await?;
    file.write_all(line.as_bytes()).await?;
    file.write_all(b"\n").await?;
    file.flush().await?;
    file.sync_all().await?;
    Ok(())
}

const MAX_ACCOUNT_TITLE_LEN: usize = 200;
const MAX_PROFILE_SUMMARY_LEN: usize = 500;
const MAX_PROFILE_ADDRESS_LEN: usize = 500;
const MAX_PROFILE_TAGS: usize = 64;
const MAX_PROFILE_TAG_LEN: usize = 64;

fn normalise_profile_title(raw: &str) -> Result<String, &'static str> {
    let title = raw.trim();
    if title.is_empty() || title.chars().count() > MAX_ACCOUNT_TITLE_LEN {
        return Err("title must contain between 1 and 200 characters");
    }
    Ok(title.to_string())
}

fn normalise_profile_summary(raw: Option<&str>) -> Result<Option<String>, &'static str> {
    let summary = raw
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    if summary
        .as_ref()
        .is_some_and(|value| value.chars().count() > MAX_PROFILE_SUMMARY_LEN)
    {
        return Err("summary is too long");
    }
    Ok(summary)
}

fn parse_optional_profile_summary(payload: &Value) -> Result<Option<String>, &'static str> {
    match payload.get("summary") {
        None => Ok(None),
        Some(Value::String(summary)) => normalise_profile_summary(Some(summary)),
        Some(_) => Err("summary must be a string"),
    }
}

fn normalise_profile_tags<'a>(
    raw_tags: impl IntoIterator<Item = &'a str>,
    required_tags: &[&str],
) -> Result<Vec<String>, &'static str> {
    let mut tags = Vec::new();
    for raw in raw_tags {
        let tag = raw.trim();
        if tag.is_empty() {
            continue;
        }
        if tag.chars().count() > MAX_PROFILE_TAG_LEN {
            return Err("a tag is too long");
        }
        if !tags.iter().any(|existing| existing == tag) {
            tags.push(tag.to_string());
            if tags.len() > MAX_PROFILE_TAGS {
                return Err("too many tags");
            }
        }
    }
    for required in required_tags {
        if !tags.iter().any(|tag| tag == required) {
            tags.push((*required).to_string());
        }
    }
    if tags.len() > MAX_PROFILE_TAGS {
        return Err("too many tags");
    }
    Ok(tags)
}

fn parse_optional_profile_tags(payload: &Value) -> Result<Vec<String>, &'static str> {
    match payload.get("tags") {
        None => Ok(Vec::new()),
        Some(Value::Array(tags)) => {
            if !tags.iter().all(Value::is_string) {
                return Err("tags must contain only strings");
            }
            normalise_profile_tags(tags.iter().filter_map(Value::as_str), &[])
        }
        Some(_) => Err("tags must be an array of strings"),
    }
}

fn profile_response(
    account: &AccountInternal,
    address: Option<String>,
    location: Option<Location>,
) -> OwnGarnrolleProfile {
    OwnGarnrolleProfile {
        id: account.public.id.clone(),
        title: account.public.title.clone(),
        summary: account.public.summary.clone(),
        tags: account.public.tags.clone(),
        address,
        location,
        map_state: account.public.map_state,
        radius_m: account.public.radius_m,
    }
}

fn parse_jsonl_account_record(
    path: &std::path::Path,
    line_number: usize,
    line: &str,
) -> std::io::Result<(Value, AccountPublic)> {
    let value: Value = serde_json::from_str(line).map_err(|error| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("invalid JSONL account record at {path:?} line {line_number}: {error}"),
        )
    })?;
    let public = map_json_to_public_account(&value).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!(
                "account record at {path:?} line {line_number} violates the canonical Garnrolle contract"
            ),
        )
    })?;
    Ok((value, public))
}

/// Return the latest append-only JSONL record for one account.
///
/// Shared with the step-up email update path so every successful JSONL-mode
/// account mutation is durable before the in-memory cache is changed. The
/// complete source is validated with the same strict contract as startup before
/// a caller may append another record.
pub(crate) async fn latest_jsonl_account_record(
    account_id: &str,
) -> std::io::Result<Option<Value>> {
    let path = accounts_path();
    let file = match File::open(&path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut lines = BufReader::new(file).lines();
    let mut line_number = 0usize;
    let mut latest = None;
    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let (value, public) = parse_jsonl_account_record(&path, line_number, &line)?;
        if public.id == account_id {
            latest = Some(value);
        }
    }
    Ok(latest)
}

fn private_profile_from_record(record: &Value) -> (Option<String>, Option<Location>) {
    let address = record
        .get("address")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let location = record.get("location").and_then(|location| {
        let lat = location.get("lat").and_then(json_f64)?;
        let lon = location.get("lon").and_then(json_f64)?;
        Some(Location { lat, lon })
    });
    (address, location)
}

fn validate_profile_update(
    payload: UpdateOwnGarnrolleRequest,
) -> Result<AccountProfileUpdate, (StatusCode, String)> {
    let bad = |message: &str| (StatusCode::BAD_REQUEST, message.to_string());
    let title = normalise_profile_title(&payload.title).map_err(bad)?;
    let summary = normalise_profile_summary(payload.summary.as_deref()).map_err(bad)?;
    let address_was_provided = payload.address.is_some();
    let address = payload
        .address
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if address
        .as_ref()
        .is_some_and(|value| value.len() > MAX_PROFILE_ADDRESS_LEN)
    {
        return Err(bad("address is too long"));
    }
    if payload.clear_address && address_was_provided {
        return Err(bad("address and clear_address cannot be combined"));
    }
    if payload.clear_location && payload.location.is_some() {
        return Err(bad("location and clear_location cannot be combined"));
    }
    if payload.clear_location && payload.map_state != GarnrolleMapState::NotOnMap {
        return Err(bad("clear_location requires map_state not_on_map"));
    }

    let tags = normalise_profile_tags(
        payload.tags.iter().map(String::as_str),
        &["account", "garnrolle"],
    )
    .map_err(bad)?;

    if let Some(location) = &payload.location {
        if !location.lat.is_finite()
            || !location.lon.is_finite()
            || !(-90.0..=90.0).contains(&location.lat)
            || !(-180.0..=180.0).contains(&location.lon)
        {
            return Err(bad(
                "location is outside the valid latitude/longitude range",
            ));
        }
    }

    let (radius_m, map_state) = match payload.map_state {
        GarnrolleMapState::NotOnMap => (0_i64, "not_on_map".to_string()),
        GarnrolleMapState::Exact => (0_i64, "exact".to_string()),
        GarnrolleMapState::Radius => {
            let radius = payload.radius_m.unwrap_or(250);
            if !(MIN_RADIUS_M..=MAX_RADIUS_M).contains(&radius) {
                return Err(bad("radius_m must be between 50 and 5000"));
            }
            (i64::from(radius), "radius".to_string())
        }
    };

    Ok(AccountProfileUpdate {
        title,
        summary,
        tags,
        address,
        clear_address: payload.clear_address,
        map_state,
        radius_m,
        location: payload.location,
        clear_location: payload.clear_location,
    })
}

fn update_jsonl_profile_record(
    mut record: Value,
    update: &AccountProfileUpdate,
) -> Result<(Value, Option<Location>), (StatusCode, String)> {
    let object = record.as_object_mut().ok_or_else(|| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "stored account record is invalid".to_string(),
        )
    })?;
    let existing_location = object.get("location").and_then(|value| {
        let lat = value.get("lat").and_then(json_f64)?;
        let lon = value.get("lon").and_then(json_f64)?;
        Some(Location { lat, lon })
    });
    let effective_location = if update.clear_location {
        None
    } else {
        update.location.clone().or(existing_location)
    };
    if update.map_state != "not_on_map" && effective_location.is_none() {
        return Err((
            StatusCode::BAD_REQUEST,
            "a map location is required for this visibility".to_string(),
        ));
    }
    if update.map_state != "radius" {
        let binding_matches_current_location =
            effective_location.as_ref().is_some_and(|location| {
                radius_projection_matches_location(object.get(RADIUS_PROJECTION_KEY), location)
            });
        if !binding_matches_current_location {
            object.remove(RADIUS_PROJECTION_KEY);
        }
    }
    if update.map_state == "radius" {
        let location = effective_location
            .as_ref()
            .expect("mapped location checked above");
        let radius_m = u32::try_from(update.radius_m).map_err(|_| {
            (
                StatusCode::BAD_REQUEST,
                "radius_m is outside the supported range".to_string(),
            )
        })?;
        let projection =
            ensure_radius_projection_value(object.get(RADIUS_PROJECTION_KEY), location, radius_m);
        object.insert(RADIUS_PROJECTION_KEY.to_string(), projection);
    }

    object.insert("type".to_string(), json!("garnrolle"));
    object.insert("title".to_string(), json!(update.title));
    object.insert("map_state".to_string(), json!(update.map_state));
    object.insert("radius_m".to_string(), json!(update.radius_m));
    match &update.summary {
        Some(summary) => {
            object.insert("summary".to_string(), json!(summary));
        }
        None => {
            object.remove("summary");
        }
    }
    if update.tags.is_empty() {
        object.remove("tags");
    } else {
        object.insert("tags".to_string(), json!(update.tags));
    }
    if update.clear_address {
        object.remove("address");
    } else if let Some(address) = &update.address {
        object.insert("address".to_string(), json!(address));
    }
    if update.clear_location {
        object.remove("location");
    } else if let Some(location) = &update.location {
        object.insert(
            "location".to_string(),
            json!({ "lat": location.lat, "lon": location.lon }),
        );
    }
    Ok((record, effective_location))
}

pub async fn get_own_garnrolle_profile(
    State(state): State<ApiState>,
    Extension(ctx): Extension<AuthContext>,
) -> Result<Json<OwnGarnrolleProfile>, (StatusCode, String)> {
    let account_id = ctx
        .account_id
        .as_deref()
        .filter(|_| ctx.authenticated)
        .ok_or((
            StatusCode::UNAUTHORIZED,
            "authentication required".to_string(),
        ))?;
    let _persist_guard = state.accounts_persist.lock().await;
    let account = state
        .accounts
        .read()
        .await
        .get(account_id)
        .cloned()
        .ok_or((StatusCode::NOT_FOUND, "account not found".to_string()))?;

    let (address, location) = match state.config.domain_account_write_source {
        DomainAccountWriteSource::Jsonl => {
            let record = latest_jsonl_account_record(account_id)
                .await
                .map_err(|error| {
                    tracing::error!(%error, "failed to read own JSONL account profile");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to load account profile".to_string(),
                    )
                })?
                .ok_or((
                    StatusCode::NOT_FOUND,
                    "account profile not found".to_string(),
                ))?;
            private_profile_from_record(&record)
        }
        DomainAccountWriteSource::Postgres => {
            let pool = state.db_pool.as_ref().ok_or((
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL pool unavailable for account profile".to_string(),
            ))?;
            load_account_profile_from_postgres(pool, account_id)
                .await
                .map_err(|error| {
                    tracing::error!(%error, "failed to read own PostgreSQL account profile");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to load account profile".to_string(),
                    )
                })?
                .ok_or((
                    StatusCode::NOT_FOUND,
                    "account profile not found".to_string(),
                ))?
        }
    };

    Ok(Json(profile_response(&account, address, location)))
}

pub async fn update_own_garnrolle_profile(
    State(state): State<ApiState>,
    Extension(ctx): Extension<AuthContext>,
    Json(payload): Json<UpdateOwnGarnrolleRequest>,
) -> Result<Json<OwnGarnrolleProfile>, (StatusCode, String)> {
    let account_id = ctx
        .account_id
        .clone()
        .filter(|_| ctx.authenticated)
        .ok_or((
            StatusCode::UNAUTHORIZED,
            "authentication required".to_string(),
        ))?;
    let update = validate_profile_update(payload)?;
    let _persist_guard = state.accounts_persist.lock().await;
    let existing = state
        .accounts
        .read()
        .await
        .get(&account_id)
        .cloned()
        .ok_or((StatusCode::NOT_FOUND, "account not found".to_string()))?;

    let stored = match state.config.domain_account_write_source {
        DomainAccountWriteSource::Jsonl => {
            let record = latest_jsonl_account_record(&account_id)
                .await
                .map_err(|error| {
                    tracing::error!(%error, "failed to read JSONL account before profile update");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to update account profile".to_string(),
                    )
                })?
                .ok_or((
                    StatusCode::NOT_FOUND,
                    "account profile not found".to_string(),
                ))?;
            let (record, _) = update_jsonl_profile_record(record, &update)?;
            let (address, location) = private_profile_from_record(&record);
            let public = map_json_to_public_account(&record).ok_or((
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to project updated account".to_string(),
            ))?;
            append_account_line(&record).await.map_err(|error| {
                tracing::error!(%error, "failed to append JSONL account profile update");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "failed to persist account profile".to_string(),
                )
            })?;
            let mut account = existing;
            account.public = public;
            crate::domain_db::StoredAccountProfile {
                account,
                address,
                location,
            }
        }
        DomainAccountWriteSource::Postgres => {
            let pool = state.db_pool.as_ref().ok_or((
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL pool unavailable for account profile".to_string(),
            ))?;
            update_account_profile_in_postgres(pool, &account_id, &update)
                .await
                .map_err(|error| match error {
                    AccountProfileUpdateError::NotFound => {
                        (StatusCode::NOT_FOUND, "account not found".to_string())
                    }
                    AccountProfileUpdateError::MissingLocation => (
                        StatusCode::BAD_REQUEST,
                        "a map location is required for this visibility".to_string(),
                    ),
                    other => {
                        tracing::error!(error = %other, "failed to update PostgreSQL account profile");
                        (
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "failed to persist account profile".to_string(),
                        )
                    }
                })?
        }
    };

    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(stored.account.clone());
    }
    tracing::info!(
        event = "account.profile.updated",
        account_id = %account_id,
        map_state = %update.map_state,
        write_source = ?state.config.domain_account_write_source,
        "Account updated its own Garnrolle profile"
    );
    Ok(Json(profile_response(
        &stored.account,
        stored.address,
        stored.location,
    )))
}

/// Create the first/next account as an operator (Admin-only; gated by
/// `require_admin` middleware). v0 creates a verortete Garnrolle with a public
/// position derived from `location` (+ optional `radius_m`, default 0 => exact).
///
/// ## radius_m semantics
///
/// - `radius_m=0` (default): `public_pos` equals `location` exactly.
/// - `radius_m>0`: a cryptographically random point inside the true radius is
///   persisted in the private account payload. The same private binding is
///   reused across reloads and temporary map hiding. It is rotated only when
///   the private location or radius changes. Records without a valid binding
///   fail closed as `not_on_map`.
///
/// ## role=admin in v0
///
/// The `role` field accepts `"weber"` (default) or `"admin"`. Allowing an
/// Admin to create another Admin account is **intentional in v0** — it enables
/// controlled power delegation by the initial operator. Only the first Admin
/// must be bootstrapped via `scripts/dev/bootstrap-first-account.sh`; all
/// subsequent Admins can be created through this endpoint by an existing Admin.
///
/// Persists to the configured account-create write source and inserts into the
/// in-memory store (immediate visibility for GET /accounts).
pub async fn create_account(
    State(state): State<ApiState>,
    Extension(ctx): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<AccountPublic>), (StatusCode, String)> {
    reject_account_create_unless_writable(&state)?;

    let bad = |msg: &str| (StatusCode::BAD_REQUEST, msg.to_string());

    // --- title (required, non-empty) ---
    let title = payload
        .get("title")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    if title.is_empty() || title.chars().count() > MAX_ACCOUNT_TITLE_LEN {
        return Err(bad("title must contain between 1 and 200 characters"));
    }

    // --- type (v0: garnrolle only) ---
    let kind = payload
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("garnrolle");
    if kind != "garnrolle" {
        return Err(bad("type must be 'garnrolle'"));
    }

    // --- role (allowlist weber|admin, default weber) ---
    let role_str = payload
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("weber");
    let role = match role_str {
        "weber" => Role::Weber,
        "admin" => Role::Admin,
        _ => return Err(bad("role must be 'weber' or 'admin'")),
    };

    // --- location (required) + range validation ---
    let location = payload
        .get("location")
        .ok_or_else(|| bad("location is required"))?;
    let (lat, lon) = match (
        location.get("lat").and_then(json_f64),
        location.get("lon").and_then(json_f64),
    ) {
        (Some(la), Some(lo)) => (la, lo),
        _ => return Err(bad("location.lat and location.lon are required numbers")),
    };
    if !(-90.0..=90.0).contains(&lat) {
        return Err(bad("location.lat must be in [-90, 90]"));
    }
    if !(-180.0..=180.0).contains(&lon) {
        return Err(bad("location.lon must be in [-180, 180]"));
    }

    // --- radius_m (optional, default 0) ---
    let radius_m: u32 = match payload.get("radius_m") {
        None | Some(Value::Null) => 0,
        Some(v) => match v.as_u64() {
            Some(0) => 0,
            Some(n) if n <= u64::from(MAX_RADIUS_M) && n >= u64::from(MIN_RADIUS_M) => n as u32,
            Some(_) => return Err(bad("radius_m must be 0 or between 50 and 5000")),
            None => return Err(bad("radius_m must be a non-negative integer")),
        },
    };

    // --- id (optional UUID, else generated) ---
    let id = match payload.get("id").and_then(|v| v.as_str()) {
        Some(s) => {
            if Uuid::parse_str(s).is_err() {
                return Err(bad("id must be a UUID"));
            }
            s.to_string()
        }
        None => Uuid::new_v4().to_string(),
    };

    // --- optional summary / tags / email ---
    let summary = parse_optional_profile_summary(&payload).map_err(bad)?;
    let tags = parse_optional_profile_tags(&payload).map_err(bad)?;
    let email = payload
        .get("email")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    let webauthn_user_id = Uuid::new_v4();

    // --- Build canonical JSONL record (matches contract after key projection;
    // role/email are operational fields read by the API) ---
    let mut record = serde_json::Map::new();
    record.insert("id".into(), json!(id));
    record.insert("type".into(), json!("garnrolle"));
    record.insert("title".into(), json!(title));
    if let Some(s) = &summary {
        record.insert("summary".into(), json!(s));
    }
    if !tags.is_empty() {
        record.insert("tags".into(), json!(tags));
    }
    record.insert("role".into(), json!(role_str));
    record.insert("location".into(), json!({ "lat": lat, "lon": lon }));
    record.insert(
        "map_state".into(),
        json!(if radius_m > 0 { "radius" } else { "exact" }),
    );
    record.insert("radius_m".into(), json!(radius_m));
    if radius_m > 0 {
        let private_location = Location { lat, lon };
        record.insert(
            RADIUS_PROJECTION_KEY.into(),
            ensure_radius_projection_value(None, &private_location, radius_m),
        );
    }
    if let Some(e) = &email {
        record.insert("email".into(), json!(e));
    }
    record.insert(
        "webauthn_user_id".into(),
        Value::String(webauthn_user_id.to_string()),
    );
    let record = Value::Object(record);

    let public = map_json_to_public_account(&record).ok_or_else(|| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to map account".to_string(),
        )
    })?;

    // --- Persist (serialize creates so check-then-write is atomic) ---
    let _persist_guard = state.accounts_persist.lock().await;

    {
        let accounts = state.accounts.read().await;
        if accounts.get(&id).is_some() {
            return Err((
                StatusCode::CONFLICT,
                "account id already exists".to_string(),
            ));
        }
        if let Some(e) = &email {
            if accounts.get_by_email(e).is_some() {
                return Err((StatusCode::CONFLICT, "email already exists".to_string()));
            }
        }
    }

    // Persist to the configured account-create write source. Only after a
    // successful durable write is the in-memory store mutated. A failed write
    // must never leave a phantom account in memory, and the two write sources
    // are mutually exclusive (no dual-write): JSONL mode never touches
    // PostgreSQL, PostgreSQL mode never appends JSONL.
    match state.config.domain_account_write_source {
        DomainAccountWriteSource::Jsonl => {
            if let Err(e) = append_account_line(&record).await {
                tracing::error!(error = %e, "failed to append account to JSONL");
                return Err((
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "failed to persist account".to_string(),
                ));
            }
        }
        DomainAccountWriteSource::Postgres => {
            // Startup validation guarantees a pool exists in this mode; treat a
            // missing pool as an internal error rather than silently degrading.
            let pool = state.db_pool.as_ref().ok_or_else(|| {
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "PostgreSQL pool unavailable for account write".to_string(),
                )
            })?;
            match insert_account_from_jsonl_record(pool, &record).await {
                Ok(()) => {}
                Err(AccountWriteError::DuplicateId) => {
                    return Err((
                        StatusCode::CONFLICT,
                        "account id already exists".to_string(),
                    ));
                }
                Err(AccountWriteError::DuplicateEmail) => {
                    // Same generic message as the in-memory precheck above, so the
                    // client sees a consistent 409 whether the conflict is caught
                    // in memory or by the PostgreSQL unique index. The offending
                    // email, account id and constraint name are never leaked.
                    return Err((StatusCode::CONFLICT, "email already exists".to_string()));
                }
                Err(e) => {
                    tracing::error!(error = %e, "failed to insert account into domain_accounts");
                    return Err((
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to persist account".to_string(),
                    ));
                }
            }
        }
    }

    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(AccountInternal {
            public: public.clone(),
            role,
            email,
            webauthn_user_id,
        });
    }

    tracing::info!(
        event = "account.created",
        account_id = %id,
        created_by = ctx.account_id.as_deref().unwrap_or("?"),
        write_source = ?state.config.domain_account_write_source,
        "Account created by operator"
    );

    Ok((StatusCode::CREATED, Json(public)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_guard_public_view_never_leaks_location() {
        let input = json!({
            "id": "test-leak-guard",
            "type": "garnrolle",
            "title": "Leak Test",
            "map_state": "exact",
            "location": { "lat": 53.5, "lon": 10.0 }
        });

        let account = map_json_to_public_account(&input).expect("Mapping failed");
        let output_value = serde_json::to_value(&account).expect("Serialization failed");

        // GUARD: The "location" field must NOT be present in the public JSON output.
        assert!(
            output_value.get("location").is_none(),
            "Public view MUST NOT contain 'location' field!"
        );

        // But public_pos MUST be present (as it is public)
        assert!(output_value.get("public_pos").is_some());
    }

    #[test]
    fn invalid_explicit_map_state_is_rejected() {
        let input = serde_json::json!({
            "id": "test-invalid-map-state",
            "type": "garnrolle",
            "title": "Invalid state",
            "map_state": "public-ish",
            "location": { "lat": 53.5, "lon": 10.0 }
        });
        assert!(map_json_to_public_account(&input).is_none());
    }

    #[test]
    fn missing_map_state_is_rejected() {
        let input = serde_json::json!({
            "id": "test-missing-map-state",
            "type": "garnrolle",
            "title": "Missing state"
        });
        assert!(map_json_to_public_account(&input).is_none());
    }

    #[test]
    fn explicit_not_on_map_ignores_internal_and_supplied_public_positions() {
        let input = serde_json::json!({
            "id": "test-explicit-hidden",
            "type": "garnrolle",
            "title": "Hidden Test",
            "map_state": "not_on_map",
            "location": { "lat": 53.5, "lon": 10.0 },
            "public_pos": { "lat": 1.0, "lon": 2.0 },
            "radius_m": 500
        });

        let account = map_json_to_public_account(&input).expect("Mapping failed");
        assert_eq!(account.map_state, GarnrolleMapState::NotOnMap);
        assert_eq!(account.radius_m, 0);
        assert!(account.public_pos.is_none());
        let output = serde_json::to_value(&account).expect("serialize public account");
        assert!(output.get("location").is_none());
        assert!(output.get("public_pos").is_none());
    }

    #[test]
    fn removed_identity_and_visibility_fields_are_rejected() {
        for (field, value) in [
            ("mode", json!("verortet")),
            ("ron_flag", json!(true)),
            ("visibility", json!("private")),
            ("suppress_public_pos", json!(true)),
        ] {
            let mut input = json!({
                "id": format!("removed-{field}"),
                "type": "garnrolle",
                "title": "Removed field",
                "map_state": "not_on_map",
                "radius_m": 0
            });
            input[field] = value;
            assert!(
                map_json_to_public_account(&input).is_none(),
                "removed field {field} must not be interpreted"
            );
        }
    }

    #[test]
    fn non_canonical_or_missing_account_type_is_rejected() {
        let non_canonical = json!({
            "id": "removed-account-type",
            "type": "ron",
            "title": "Removed type",
            "map_state": "not_on_map"
        });
        assert!(map_json_to_public_account(&non_canonical).is_none());

        let missing = json!({
            "id": "missing-account-type",
            "title": "Missing type",
            "map_state": "not_on_map"
        });
        assert!(map_json_to_public_account(&missing).is_none());
    }

    #[test]
    fn radius_without_private_binding_fails_closed() {
        let input = json!({
            "id": "radius-without-binding",
            "type": "garnrolle",
            "title": "Radius without binding",
            "location": { "lat": 53.5, "lon": 10.0 },
            "map_state": "radius",
            "radius_m": 500,
        });

        let account = map_json_to_public_account(&input).expect("valid account");
        assert_eq!(account.map_state, GarnrolleMapState::NotOnMap);
        assert_eq!(account.radius_m, 0);
        assert!(account.public_pos.is_none());
    }

    #[test]
    fn oversized_radius_fails_closed() {
        let input = json!({
            "id": "oversized-radius",
            "type": "garnrolle",
            "title": "Oversized radius",
            "map_state": "radius",
            "location": { "lat": 53.5, "lon": 10.0 },
            "radius_m": u64::MAX,
        });

        let account = map_json_to_public_account(&input).expect("valid account");
        assert_eq!(account.map_state, GarnrolleMapState::NotOnMap);
        assert_eq!(account.radius_m, 0);
        assert!(account.public_pos.is_none());
    }

    #[test]
    fn private_projection_is_reused_and_never_serialized_publicly() {
        let location = Location {
            lat: 53.5585,
            lon: 10.058,
        };
        let projection = ensure_radius_projection_value(None, &location, 500);
        let reused = ensure_radius_projection_value(Some(&projection), &location, 500);
        assert_eq!(projection, reused);

        let mut input = json!({
            "id": "safe-radius",
            "type": "garnrolle",
            "title": "Safe radius",
            "location": location,
            "map_state": "radius",
            "radius_m": 500,
        });
        input[RADIUS_PROJECTION_KEY] = projection;

        let account = map_json_to_public_account(&input).expect("valid account");
        assert_eq!(account.map_state, GarnrolleMapState::Radius);
        assert_eq!(account.radius_m, 500);
        assert!(account.public_pos.is_some());
        let output = serde_json::to_value(account).expect("public serialization");
        assert!(output.get("location").is_none());
        assert!(output.get(RADIUS_PROJECTION_KEY).is_none());
        assert!(output.get("anchor").is_none());
    }

    #[test]
    fn private_projection_rotates_when_location_or_radius_changes() {
        let first_location = Location {
            lat: 53.5585,
            lon: 10.058,
        };
        let moved_location = Location {
            lat: 53.5586,
            lon: 10.058,
        };
        let first = ensure_radius_projection_value(None, &first_location, 500);
        let moved = ensure_radius_projection_value(Some(&first), &moved_location, 500);
        let resized = ensure_radius_projection_value(Some(&first), &first_location, 750);

        assert_ne!(first, moved);
        assert_ne!(first, resized);
        assert!(radius_projection_public_pos(Some(&moved), &moved_location, 500).is_some());
        assert!(radius_projection_public_pos(Some(&resized), &first_location, 750).is_some());
        assert!(radius_projection_public_pos(Some(&first), &moved_location, 500).is_none());
        assert!(radius_projection_public_pos(Some(&first), &first_location, 750).is_none());
    }

    #[test]
    fn cryptographic_samples_use_half_open_unit_interval() {
        for _ in 0..1_024 {
            let (radial, angular) = cryptographic_unit_pair();
            assert!(radial > 0.0 && radial < 1.0);
            assert!((0.0..1.0).contains(&angular));
        }
    }

    #[test]
    fn radius_boundary_tolerance_is_small_and_geometrically_sufficient() {
        let anchors = [
            Location {
                lat: 53.5585,
                lon: 10.058,
            },
            Location {
                lat: 89.9999,
                lon: 179.9999,
            },
            Location {
                lat: 90.0,
                lon: 42.0,
            },
            Location {
                lat: 0.0,
                lon: 179.9999,
            },
        ];
        let radius_m = MAX_RADIUS_M;
        for anchor in anchors {
            let boundary = RadiusProjectionBinding {
                version: RADIUS_PROJECTION_VERSION,
                anchor: anchor.clone(),
                radius_m,
                public_pos: destination_point(&anchor, f64::from(radius_m), 1.2345),
            };
            let boundary_value = serde_json::to_value(boundary).expect("boundary binding");
            assert!(
                validated_radius_projection(Some(&boundary_value), &anchor, radius_m).is_some()
            );

            let outside = RadiusProjectionBinding {
                version: RADIUS_PROJECTION_VERSION,
                anchor: anchor.clone(),
                radius_m,
                public_pos: destination_point(
                    &anchor,
                    f64::from(radius_m) + RADIUS_DISTANCE_EPSILON_M + 0.10,
                    1.2345,
                ),
            };
            let outside_value = serde_json::to_value(outside).expect("outside binding");
            assert!(validated_radius_projection(Some(&outside_value), &anchor, radius_m).is_none());
        }
    }

    #[test]
    fn generated_projection_stays_inside_true_circle_at_poles_and_dateline() {
        let anchors = [
            Location {
                lat: 53.5585,
                lon: 10.058,
            },
            Location {
                lat: 89.9999,
                lon: 179.9999,
            },
            Location {
                lat: 90.0,
                lon: 42.0,
            },
            Location {
                lat: -89.9999,
                lon: -179.9999,
            },
            Location {
                lat: -90.0,
                lon: -42.0,
            },
            Location {
                lat: 0.0,
                lon: 179.9999,
            },
        ];
        let radius_m = 5_000;

        for anchor in anchors {
            for _ in 0..64 {
                let projection = ensure_radius_projection_value(None, &anchor, radius_m);
                let public = radius_projection_public_pos(Some(&projection), &anchor, radius_m)
                    .expect("generated projection validates");
                assert!(location_is_valid(&public));
                let distance_m = great_circle_distance_m(&anchor, &public);
                assert!(distance_m >= MIN_RADIUS_PUBLIC_OFFSET_M);
                assert!(distance_m <= f64::from(radius_m) + RADIUS_DISTANCE_EPSILON_M);
            }
        }
    }

    #[test]
    fn tampered_or_mismatched_binding_is_suppressed() {
        let location = Location {
            lat: 53.5585,
            lon: 10.058,
        };
        let mut projection = ensure_radius_projection_value(None, &location, 500);
        projection["public_pos"] = json!({ "lat": location.lat, "lon": location.lon });
        assert!(radius_projection_public_pos(Some(&projection), &location, 500).is_none());
        projection["public_pos"] = json!({ "lat": -20.0, "lon": -20.0 });
        assert!(radius_projection_public_pos(Some(&projection), &location, 500).is_none());

        let mut input = json!({
            "id": "tampered-radius",
            "type": "garnrolle",
            "title": "Tampered radius",
            "location": location,
            "map_state": "radius",
            "radius_m": 500,
        });
        input[RADIUS_PROJECTION_KEY] = projection;
        let account = map_json_to_public_account(&input).expect("valid account");
        assert_eq!(account.map_state, GarnrolleMapState::NotOnMap);
        assert!(account.public_pos.is_none());
    }
}

#[cfg(test)]
mod profile_update_tests {
    use super::*;
    use serde_json::json;

    fn request(
        map_state: GarnrolleMapState,
        address: Option<&str>,
        location: Option<Location>,
    ) -> UpdateOwnGarnrolleRequest {
        UpdateOwnGarnrolleRequest {
            title: "  Meine Garnrolle  ".to_string(),
            summary: Some("  Gemeinsame Dinge  ".to_string()),
            tags: vec![
                "skill:Kochen".to_string(),
                "skill:Kochen".to_string(),
                "interest:Commons".to_string(),
            ],
            address: address.map(str::to_string),
            clear_address: false,
            map_state,
            radius_m: Some(250),
            location,
            clear_location: false,
        }
    }

    #[test]
    fn profile_title_limit_uses_schema_characters() {
        let valid = "🐋".repeat(MAX_ACCOUNT_TITLE_LEN);
        let invalid = "🐋".repeat(MAX_ACCOUNT_TITLE_LEN + 1);

        assert_eq!(
            normalise_profile_title(&valid).expect("200 Unicode characters"),
            valid
        );
        assert_eq!(
            normalise_profile_title(&invalid),
            Err("title must contain between 1 and 200 characters")
        );
    }

    #[test]
    fn profile_summary_limit_uses_schema_characters() {
        let valid = "🐋".repeat(MAX_PROFILE_SUMMARY_LEN);
        let invalid = "🐋".repeat(MAX_PROFILE_SUMMARY_LEN + 1);

        assert_eq!(
            normalise_profile_summary(Some(&valid)).expect("500 Unicode characters"),
            Some(valid)
        );
        assert_eq!(
            normalise_profile_summary(Some(&invalid)),
            Err("summary is too long")
        );
    }

    #[test]
    fn profile_tag_normalisation_stops_after_the_first_excess_tag() {
        let raw = (0..MAX_PROFILE_TAGS + 2)
            .map(|index| format!("tag-{index}"))
            .collect::<Vec<_>>();
        let mut consumed = 0;
        let result = normalise_profile_tags(
            raw.iter().map(|value| {
                consumed += 1;
                value.as_str()
            }),
            &[],
        );

        assert_eq!(result, Err("too many tags"));
        assert_eq!(consumed, MAX_PROFILE_TAGS + 1);
    }

    #[test]
    fn mapped_profile_allows_no_address_but_rejects_invalid_location() {
        let no_address = validate_profile_update(request(
            GarnrolleMapState::Exact,
            None,
            Some(Location {
                lat: 53.5,
                lon: 10.0,
            }),
        ))
        .expect("mapped profile without address");
        assert!(no_address.address.is_none());
        assert_eq!(no_address.map_state, "exact");

        let record = json!({
            "id": "own-account",
            "type": "garnrolle",
            "title": "Alt",
            "role": "gast",
            "map_state": "not_on_map",
            "radius_m": 0
        });
        let (updated, effective_location) =
            update_jsonl_profile_record(record, &no_address).expect("mapped JSONL profile");
        assert!(updated.get("address").is_none());
        assert_eq!(updated["map_state"], "exact");
        assert_eq!(effective_location.expect("private location").lat, 53.5);

        let radius_without_address = validate_profile_update(request(
            GarnrolleMapState::Radius,
            None,
            Some(Location {
                lat: 53.5,
                lon: 10.0,
            }),
        ))
        .expect("radius profile without address");
        assert!(radius_without_address.address.is_none());
        assert_eq!(radius_without_address.map_state, "radius");

        let invalid_location = validate_profile_update(request(
            GarnrolleMapState::Exact,
            Some("Poelsweg 2, Hamburg"),
            Some(Location {
                lat: 91.0,
                lon: 10.0,
            }),
        ));
        assert_eq!(invalid_location.unwrap_err().0, StatusCode::BAD_REQUEST);
    }

    #[test]
    fn mapped_profile_without_new_or_existing_location_is_rejected() {
        for map_state in [GarnrolleMapState::Exact, GarnrolleMapState::Radius] {
            let update = validate_profile_update(request(map_state, None, None))
                .expect("profile validation leaves location resolution to persistence");
            let record = json!({
                "id": "own-account",
                "type": "garnrolle",
                "title": "Alt",
                "role": "gast",
                "map_state": "not_on_map",
                "radius_m": 0
            });

            let error = update_jsonl_profile_record(record, &update)
                .expect_err("mapped profile without any location must fail closed");
            assert_eq!(error.0, StatusCode::BAD_REQUEST);
        }
    }

    #[test]
    fn profile_validation_normalises_fields_and_keeps_required_tags() {
        let update = validate_profile_update(request(
            GarnrolleMapState::Radius,
            Some("  Poelsweg 2, Hamburg  "),
            Some(Location {
                lat: 53.5,
                lon: 10.0,
            }),
        ))
        .expect("valid profile");

        assert_eq!(update.title, "Meine Garnrolle");
        assert_eq!(update.summary.as_deref(), Some("Gemeinsame Dinge"));
        assert_eq!(update.address.as_deref(), Some("Poelsweg 2, Hamburg"));
        assert_eq!(update.map_state, "radius");
        assert_eq!(update.radius_m, 250);
        assert_eq!(
            update.tags,
            vec!["skill:Kochen", "interest:Commons", "account", "garnrolle"]
        );
    }

    #[test]
    fn jsonl_update_preserves_operational_identity_and_existing_location() {
        let record = json!({
            "id": "own-account",
            "type": "garnrolle",
            "title": "Alt",
            "role": "weber",
            "email": "private@example.test",
            "webauthn_user_id": "79e8d447-c7b3-46ff-bcc9-55a0d843f780",
            "map_state": "exact",
            "radius_m": 0,
            "location": {"lat": 53.5, "lon": 10.0},
            "address": "Alte Adresse"
        });
        let update = validate_profile_update(UpdateOwnGarnrolleRequest {
            title: "Neu".to_string(),
            summary: None,
            tags: vec![],
            address: Some("Neue Adresse".to_string()),
            clear_address: false,
            map_state: GarnrolleMapState::Exact,
            radius_m: None,
            location: None,
            clear_location: false,
        })
        .expect("valid profile");

        let (updated, effective_location) =
            update_jsonl_profile_record(record, &update).expect("update record");
        assert_eq!(updated["id"], "own-account");
        assert_eq!(updated["role"], "weber");
        assert_eq!(updated["email"], "private@example.test");
        assert_eq!(
            updated["webauthn_user_id"],
            "79e8d447-c7b3-46ff-bcc9-55a0d843f780"
        );
        assert_eq!(updated["title"], "Neu");
        assert_eq!(updated["address"], "Neue Adresse");
        assert_eq!(effective_location.unwrap().lat, 53.5);
        assert_eq!(updated["location"]["lat"], 53.5);
    }

    #[test]
    fn jsonl_update_preserves_omitted_private_fields() {
        let record = json!({
            "id": "own-account",
            "type": "garnrolle",
            "title": "Alt",
            "role": "weber",
            "map_state": "exact",
            "radius_m": 0,
            "address": "Private Adresse",
            "location": {"lat": 53.5, "lon": 10.0}
        });
        let update = validate_profile_update(request(GarnrolleMapState::Exact, None, None))
            .expect("omitted private fields");

        let (updated, effective_location) =
            update_jsonl_profile_record(record, &update).expect("preserved private fields");
        assert_eq!(updated["address"], "Private Adresse");
        assert_eq!(updated["location"]["lat"], 53.5);
        assert_eq!(effective_location.expect("preserved location").lon, 10.0);
    }

    #[test]
    fn jsonl_update_explicitly_clears_private_fields_and_binding() {
        let location = Location {
            lat: 53.5,
            lon: 10.0,
        };
        let projection = ensure_radius_projection_value(None, &location, 250);
        let mut record = json!({
            "id": "own-account",
            "type": "garnrolle",
            "title": "Alt",
            "role": "weber",
            "map_state": "not_on_map",
            "radius_m": 0,
            "address": "Private Adresse",
            "location": location
        });
        record[RADIUS_PROJECTION_KEY] = projection;
        let mut clear_request = request(GarnrolleMapState::NotOnMap, None, None);
        clear_request.clear_address = true;
        clear_request.clear_location = true;
        let update = validate_profile_update(clear_request).expect("valid explicit clear");

        let (updated, effective_location) =
            update_jsonl_profile_record(record, &update).expect("cleared private fields");
        assert!(updated.get("address").is_none());
        assert!(updated.get("location").is_none());
        assert!(updated.get(RADIUS_PROJECTION_KEY).is_none());
        assert!(effective_location.is_none());
    }

    #[test]
    fn profile_validation_rejects_conflicting_clear_signals() {
        let mut address_conflict =
            request(GarnrolleMapState::NotOnMap, Some("Private Adresse"), None);
        address_conflict.clear_address = true;
        assert_eq!(
            validate_profile_update(address_conflict).unwrap_err().0,
            StatusCode::BAD_REQUEST
        );

        let mut location_conflict = request(
            GarnrolleMapState::NotOnMap,
            None,
            Some(Location {
                lat: 53.5,
                lon: 10.0,
            }),
        );
        location_conflict.clear_location = true;
        assert_eq!(
            validate_profile_update(location_conflict).unwrap_err().0,
            StatusCode::BAD_REQUEST
        );

        let mut mapped_clear = request(GarnrolleMapState::Exact, None, None);
        mapped_clear.clear_location = true;
        assert_eq!(
            validate_profile_update(mapped_clear).unwrap_err().0,
            StatusCode::BAD_REQUEST
        );
    }

    #[test]
    fn jsonl_update_discards_binding_after_private_location_change() {
        let old_location = Location {
            lat: 53.5,
            lon: 10.0,
        };
        let projection = ensure_radius_projection_value(None, &old_location, 250);
        let mut record = json!({
            "id": "own-account",
            "type": "garnrolle",
            "title": "Alt",
            "role": "weber",
            "map_state": "not_on_map",
            "radius_m": 0,
            "location": old_location
        });
        record[RADIUS_PROJECTION_KEY] = projection;
        let update = validate_profile_update(request(
            GarnrolleMapState::NotOnMap,
            None,
            Some(Location {
                lat: 53.6,
                lon: 10.1,
            }),
        ))
        .expect("not-on-map profile with new private location");

        let (updated, _) = update_jsonl_profile_record(record, &update).expect("update record");
        assert!(updated.get(RADIUS_PROJECTION_KEY).is_none());
        assert_eq!(updated["location"]["lat"], 53.6);
        assert_eq!(updated["location"]["lon"], 10.1);
    }

    #[test]
    fn not_on_map_keeps_internal_location_but_has_no_public_projection() {
        let record = json!({
            "id": "own-account",
            "type": "garnrolle",
            "title": "Alt",
            "role": "weber",
            "map_state": "exact",
            "radius_m": 0,
            "location": {"lat": 53.5, "lon": 10.0}
        });
        let update = validate_profile_update(request(GarnrolleMapState::NotOnMap, None, None))
            .expect("not-on-map profile");
        let (updated, _) = update_jsonl_profile_record(record, &update).expect("update record");
        let public = map_json_to_public_account(&updated).expect("public account");
        assert_eq!(public.map_state, GarnrolleMapState::NotOnMap);
        assert!(public.public_pos.is_none());
        assert_eq!(updated["location"]["lat"], 53.5);
    }
}
