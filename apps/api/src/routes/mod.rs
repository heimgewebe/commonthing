pub mod accounts;
pub mod auth;
mod collective_write_guard;
pub(crate) use crate::state::{postgres_domain_connection_budget, POSTGRES_DOMAIN_OPERATION_UNITS};
pub mod conversations;
mod domain_write_guard;
pub mod edges;
pub mod governance;
pub mod health;
pub mod machine;
pub mod meta;
pub mod nodes;
mod query;
pub mod search;
pub mod webgemeindezentren;

use std::collections::HashMap;

use axum::{
    extract::{Query, State},
    middleware::from_fn,
    routing::{get, post},
    Router,
};

use crate::middleware::authz::{require_admin, require_authenticated, require_write};
use crate::notifications::{
    delete_push_subscription, disable_push_subscription_by_id, get_notification_preferences,
    get_push_config, list_push_subscriptions, register_push_subscription,
    update_notification_preferences,
};
use crate::state::ApiState;

use self::{
    accounts::{
        create_account, get_account, get_own_garnrolle_profile, list_accounts,
        update_own_garnrolle_profile,
    },
    auth::{
        consume_login_get, consume_login_post, consume_step_up, dev_login, list_dev_accounts,
        list_devices, logout, logout_all, me, passkey_auth_options, passkey_auth_verify,
        passkey_register_options, passkey_register_verify, remove_device, request_login,
        request_step_up, session, session_refresh, update_email,
    },
    collective_write_guard::{
        create_node_serialized, delete_node_serialized, patch_node_serialized,
        replace_node_serialized,
    },
    conversations::{
        block_direct_conversation, create_direct_conversation, create_message, delete_message,
        get_conversation, get_node_conversation, list_direct_conversations, list_messages,
        mark_direct_conversation_read, unblock_direct_conversation, update_message,
    },
    edges::{get_edge, list_edges},
    governance::{
        create_proposal, exit_own_account, get_proposal, list_proposal_messages, list_proposals,
        post_proposal_message, request_repeal, veto_proposal, vote_proposal, withdraw_proposal,
    },
    nodes::{get_node, list_nodes},
    search::search_nodes,
    webgemeindezentren::{
        get_ortsweberei, get_webgemeindezentrum, list_ortswebereien, list_webgemeindezentren,
    },
};

// This endpoint is intentionally absent from earlier API revisions.
// The new public /commons page must get a 404 from an older backend, rather
// than accidentally receive the legacy unfiltered GET /nodes payload.
async fn list_public_nodes(
    state: State<ApiState>,
    Query(mut params): Query<HashMap<String, String>>,
) -> impl axum::response::IntoResponse {
    // Ignore caller-supplied public_only=false: the route itself is public-only.
    params.insert("public_only".to_owned(), "true".to_owned());
    list_nodes(state, Query(params)).await
}

pub fn api_router() -> Router<ApiState> {
    let router = Router::new()
        .merge(machine::api_routes())
        .route("/search", get(search_nodes))
        // Compatibility alias for node-oriented clients; `/search` is canonical.
        .route("/nodes/search", get(search_nodes))
        .route("/public/nodes", get(list_public_nodes))
        .route(
            "/nodes",
            get(list_nodes)
                .merge(post(create_node_serialized).route_layer(from_fn(require_authenticated))),
        )
        .route(
            "/nodes/{id}",
            get(get_node)
                .merge(
                    axum::routing::patch(patch_node_serialized)
                        .put(replace_node_serialized)
                        .route_layer(from_fn(require_authenticated)),
                )
                .merge(
                    axum::routing::delete(delete_node_serialized)
                        .route_layer(from_fn(require_authenticated)),
                ),
        )
        .route(
            "/direct-conversations",
            get(list_direct_conversations)
                .merge(post(create_direct_conversation))
                .route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/notifications/preferences",
            get(get_notification_preferences)
                .put(update_notification_preferences)
                .route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/push/config",
            get(get_push_config).route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/push/subscriptions",
            get(list_push_subscriptions)
                .merge(post(register_push_subscription))
                .delete(delete_push_subscription)
                .route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/push/subscriptions/{id}",
            axum::routing::delete(disable_push_subscription_by_id)
                .route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/direct-conversations/{id}/read",
            post(mark_direct_conversation_read).route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/direct-conversations/{id}/block",
            axum::routing::put(block_direct_conversation)
                .delete(unblock_direct_conversation)
                .route_layer(from_fn(require_authenticated)),
        )
        .route("/nodes/{id}/conversation", get(get_node_conversation))
        .route("/conversations/{id}", get(get_conversation))
        .route(
            "/conversations/{id}/messages",
            get(list_messages)
                .merge(post(create_message).route_layer(from_fn(require_authenticated))),
        )
        .route(
            "/conversations/{conversation_id}/messages/{message_id}",
            axum::routing::patch(update_message)
                .delete(delete_message)
                .route_layer(from_fn(require_authenticated)),
        )
        .route("/edges", get(list_edges))
        .route("/edges/{id}", get(get_edge))
        .route("/ortswebereien", get(list_ortswebereien))
        .route("/ortswebereien/{id}", get(get_ortsweberei))
        .route("/webgemeindezentren", get(list_webgemeindezentren))
        .route("/webgemeindezentren/{id}", get(get_webgemeindezentrum))
        .route(
            "/accounts",
            get(list_accounts)
                .post(create_account)
                .route_layer(from_fn(require_admin)),
        )
        .route(
            "/accounts/me/profile",
            get(get_own_garnrolle_profile)
                .patch(update_own_garnrolle_profile)
                .route_layer(from_fn(require_authenticated)),
        )
        .route("/accounts/{id}", get(get_account))
        // Antragssystem (docs/specs/governance-antraege.md). `POST /proposals`
        // und `POST /accounts/me/exit` sind bewusst nicht über eine allgemeine
        // Rollen-Middleware geschützt: der eigene Weberantrag und der eigene
        // Austritt sind eng begrenzte Gast-Sonderwege; die Handler prüfen die
        // Authentifizierung selbst. Gesprächsbeiträge sind für alle angemeldeten
        // Accounts offen; formale Vetos und Stimmen bleiben Weber/Admin vorbehalten.
        .route("/proposals", get(list_proposals).post(create_proposal))
        .route("/proposals/{id}", get(get_proposal))
        .route(
            "/proposals/{id}/withdraw",
            post(withdraw_proposal).route_layer(from_fn(require_authenticated)),
        )
        .route(
            "/proposals/{id}/repeal",
            post(request_repeal).route_layer(from_fn(require_write)),
        )
        .route(
            "/proposals/{id}/veto",
            post(veto_proposal).route_layer(from_fn(require_write)),
        )
        .route(
            "/proposals/{id}/vote",
            axum::routing::put(vote_proposal).route_layer(from_fn(require_write)),
        )
        .route(
            "/proposals/{id}/messages",
            get(list_proposal_messages)
                .merge(post(post_proposal_message).route_layer(from_fn(require_authenticated))),
        )
        .route("/accounts/me/exit", post(exit_own_account))
        .route("/auth/dev/accounts", get(list_dev_accounts))
        .route("/auth/dev/login", post(dev_login))
        .route("/auth/magic-link/request", post(request_login))
        .route(
            "/auth/magic-link/consume",
            get(consume_login_get).post(consume_login_post),
        )
        .route("/auth/logout", post(logout))
        .route("/auth/logout-all", post(logout_all))
        .route("/auth/devices", get(list_devices))
        .route("/auth/devices/{id}", axum::routing::delete(remove_device))
        .route("/auth/me", get(me))
        .route("/auth/me/email", axum::routing::put(update_email))
        .route("/auth/session", get(session))
        .route("/auth/session/refresh", post(session_refresh))
        .route("/auth/step-up/magic-link/request", post(request_step_up))
        .route("/auth/step-up/magic-link/consume", post(consume_step_up))
        .route(
            "/auth/passkeys/register/options",
            post(passkey_register_options),
        )
        .route(
            "/auth/passkeys/register/verify",
            post(passkey_register_verify),
        )
        .route("/auth/passkeys/auth/options", post(passkey_auth_options))
        .route("/auth/passkeys/auth/verify", post(passkey_auth_verify));

    #[cfg(feature = "integration-testing")]
    let router = router
        .route(
            "/auth/testing/governance/bootstrap-session",
            post(auth::governance_testing_bootstrap_session),
        )
        .route(
            "/governance/testing/proposals/{id}/advance",
            post(governance::governance_testing_advance_proposal),
        );

    #[cfg(feature = "integration-testing")]
    let router = router.route(
        "/auth/testing/passkeys/bootstrap-session",
        post(auth::passkey_testing_bootstrap_session),
    );

    #[cfg(feature = "integration-testing")]
    let router = router.route(
        "/auth/testing/passkeys/register/grant",
        post(auth::passkey_testing_issue_grant),
    );

    #[cfg(feature = "integration-testing")]
    let router = router.route(
        "/auth/testing/passkeys",
        get(auth::passkey_testing_list_credentials),
    );

    router
}
