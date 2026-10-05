use std::{
    collections::{BTreeMap, HashMap},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::OnceLock,
};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Postgres, Transaction};
use tokio::{
    fs::{File, OpenOptions},
    io::{AsyncBufReadExt, BufReader},
    sync::Mutex,
};
use uuid::Uuid;

use crate::{
    routes::nodes::{map_json_to_node, Node},
    utils::nodes_path,
};

const AUDIT_SCHEMA_VERSION: u8 = 1;
const AUDIT_DIRECTORY: &str = ".node-mutation-audit";
const AUDIT_FILE: &str = "events.jsonl";
const NODE_HASH_DOMAIN: &[u8] = b"weltgewebe-node-mutation-node-v1\0";
const ACTOR_HASH_DOMAIN: &[u8] = b"weltgewebe-node-mutation-actor-v1\0";
static JSONL_AUDIT_APPEND_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeMutationOperation {
    Patch,
    Replace,
    Delete,
}

impl NodeMutationOperation {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Patch => "patch",
            Self::Replace => "replace",
            Self::Delete => "delete",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeMutationAudit {
    pub operation_id: String,
    pub operation: NodeMutationOperation,
    pub node_id: String,
    pub actor_subject_hash: String,
    pub before_hash: String,
    pub after_hash: Option<String>,
    pub occurred_at: DateTime<Utc>,
}

impl NodeMutationAudit {
    pub fn new(
        operation: NodeMutationOperation,
        node_id: impl Into<String>,
        actor_account_id: &str,
        before: &Node,
        after: Option<&Node>,
    ) -> Result<Self, serde_json::Error> {
        Ok(Self {
            operation_id: Uuid::new_v4().to_string(),
            operation,
            node_id: node_id.into(),
            actor_subject_hash: actor_subject_hash(actor_account_id),
            before_hash: node_hash(before)?,
            after_hash: after.map(node_hash).transpose()?,
            occurred_at: Utc::now(),
        })
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum JsonlAuditState {
    Prepared,
    Committed,
    Aborted,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct JsonlAuditEvent {
    schema_version: u8,
    operation_id: String,
    state: JsonlAuditState,
    operation: NodeMutationOperation,
    node_id: String,
    actor_subject_hash: String,
    before_hash: String,
    after_hash: Option<String>,
    occurred_at: DateTime<Utc>,
    finalized_at: Option<DateTime<Utc>>,
    recovered: bool,
}

impl JsonlAuditEvent {
    fn from_audit(audit: &NodeMutationAudit, state: JsonlAuditState, recovered: bool) -> Self {
        Self {
            schema_version: AUDIT_SCHEMA_VERSION,
            operation_id: audit.operation_id.clone(),
            state,
            operation: audit.operation,
            node_id: audit.node_id.clone(),
            actor_subject_hash: audit.actor_subject_hash.clone(),
            before_hash: audit.before_hash.clone(),
            after_hash: audit.after_hash.clone(),
            occurred_at: audit.occurred_at,
            finalized_at: (state != JsonlAuditState::Prepared).then(Utc::now),
            recovered,
        }
    }

    fn to_audit(&self) -> NodeMutationAudit {
        NodeMutationAudit {
            operation_id: self.operation_id.clone(),
            operation: self.operation,
            node_id: self.node_id.clone(),
            actor_subject_hash: self.actor_subject_hash.clone(),
            before_hash: self.before_hash.clone(),
            after_hash: self.after_hash.clone(),
            occurred_at: self.occurred_at,
        }
    }

    fn validate(&self) -> std::io::Result<()> {
        if self.schema_version != AUDIT_SCHEMA_VERSION {
            return Err(invalid_data("unsupported node mutation audit schema"));
        }
        Uuid::parse_str(&self.operation_id)
            .map_err(|_| invalid_data("invalid node mutation operation id"))?;
        for (label, digest) in [
            ("actor", self.actor_subject_hash.as_str()),
            ("before", self.before_hash.as_str()),
        ] {
            if !is_sha256(digest) {
                return Err(invalid_data(format!("invalid {label} SHA-256")));
            }
        }
        if let Some(after_hash) = self.after_hash.as_deref() {
            if !is_sha256(after_hash) {
                return Err(invalid_data("invalid after SHA-256"));
            }
        }
        match self.operation {
            NodeMutationOperation::Patch | NodeMutationOperation::Replace
                if self.after_hash.is_none() =>
            {
                Err(invalid_data("updated node audit requires an after hash"))
            }
            NodeMutationOperation::Delete if self.after_hash.is_some() => Err(invalid_data(
                "deleted node audit must not have an after hash",
            )),
            _ => Ok(()),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct JsonlAuditRecoverySummary {
    pub committed: u64,
    pub aborted: u64,
    pub already_final: u64,
}

pub fn actor_subject_hash(account_id: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(ACTOR_HASH_DOMAIN);
    digest.update(account_id.as_bytes());
    hex::encode(digest.finalize())
}

pub fn node_hash(node: &Node) -> Result<String, serde_json::Error> {
    let encoded = serde_json::to_vec(node)?;
    let mut digest = Sha256::new();
    digest.update(NODE_HASH_DOMAIN);
    digest.update(encoded);
    Ok(hex::encode(digest.finalize()))
}

pub async fn insert_postgres_audit(
    transaction: &mut Transaction<'_, Postgres>,
    audit: &NodeMutationAudit,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO domain_node_mutation_audit (\
             operation_id, node_id, actor_subject_hash, operation, before_hash, after_hash, occurred_at\
         ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
    )
    .bind(&audit.operation_id)
    .bind(&audit.node_id)
    .bind(&audit.actor_subject_hash)
    .bind(audit.operation.as_str())
    .bind(&audit.before_hash)
    .bind(&audit.after_hash)
    .bind(audit.occurred_at)
    .execute(&mut **transaction)
    .await?;
    Ok(())
}

pub async fn persist_postgres_audit(
    pool: &PgPool,
    audit: &NodeMutationAudit,
) -> Result<(), sqlx::Error> {
    let mut transaction = pool.begin().await?;
    insert_postgres_audit(&mut transaction, audit).await?;
    transaction.commit().await?;
    Ok(())
}

pub async fn prepare_jsonl_audit(audit: &NodeMutationAudit) -> std::io::Result<()> {
    append_jsonl_event(&JsonlAuditEvent::from_audit(
        audit,
        JsonlAuditState::Prepared,
        false,
    ))
    .await
}

pub async fn commit_jsonl_audit(audit: &NodeMutationAudit) -> std::io::Result<()> {
    append_jsonl_event(&JsonlAuditEvent::from_audit(
        audit,
        JsonlAuditState::Committed,
        false,
    ))
    .await
}

pub async fn abort_jsonl_audit(audit: &NodeMutationAudit) -> std::io::Result<()> {
    append_jsonl_event(&JsonlAuditEvent::from_audit(
        audit,
        JsonlAuditState::Aborted,
        false,
    ))
    .await
}

pub async fn recover_jsonl_audit() -> std::io::Result<JsonlAuditRecoverySummary> {
    let Some(file) = open_existing_audit().await? else {
        return Ok(JsonlAuditRecoverySummary::default());
    };
    let mut latest: BTreeMap<String, JsonlAuditEvent> = BTreeMap::new();
    let mut lines = BufReader::new(file).lines();
    let mut line_number = 0usize;
    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let event: JsonlAuditEvent = serde_json::from_str(&line).map_err(|error| {
            invalid_data(format!(
                "invalid node mutation audit JSONL at line {line_number}: {error}"
            ))
        })?;
        event.validate()?;
        latest.insert(event.operation_id.clone(), event);
    }

    let nodes = load_current_node_hashes().await?;
    let mut summary = JsonlAuditRecoverySummary::default();
    for event in latest.values() {
        if event.state != JsonlAuditState::Prepared {
            summary.already_final += 1;
            continue;
        }
        let final_state = match event.operation {
            NodeMutationOperation::Patch | NodeMutationOperation::Replace => {
                let current = nodes.get(&event.node_id).ok_or_else(|| {
                    invalid_data(format!(
                        "prepared {} audit has no current node",
                        event.operation.as_str()
                    ))
                })?;
                if Some(current.as_str()) == event.after_hash.as_deref() {
                    JsonlAuditState::Committed
                } else if current == &event.before_hash {
                    JsonlAuditState::Aborted
                } else {
                    return Err(invalid_data(format!(
                        "prepared {} audit cannot be reconciled for node {}",
                        event.operation.as_str(),
                        event.node_id
                    )));
                }
            }
            NodeMutationOperation::Delete => match nodes.get(&event.node_id) {
                None => JsonlAuditState::Committed,
                Some(current) if current == &event.before_hash => JsonlAuditState::Aborted,
                Some(_) => {
                    return Err(invalid_data(format!(
                        "prepared delete audit cannot be reconciled for node {}",
                        event.node_id
                    )))
                }
            },
        };
        append_jsonl_event(&JsonlAuditEvent::from_audit(
            &event.to_audit(),
            final_state,
            true,
        ))
        .await?;
        match final_state {
            JsonlAuditState::Committed => summary.committed += 1,
            JsonlAuditState::Aborted => summary.aborted += 1,
            JsonlAuditState::Prepared => unreachable!("recovery always finalizes"),
        }
    }
    Ok(summary)
}

fn audit_path() -> std::io::Result<PathBuf> {
    let nodes = nodes_path();
    let parent = nodes
        .parent()
        .ok_or_else(|| invalid_input("nodes path has no parent directory"))?;
    Ok(parent.join(AUDIT_DIRECTORY).join(AUDIT_FILE))
}

async fn open_existing_audit() -> std::io::Result<Option<File>> {
    let path = audit_path()?;
    let directory = path
        .parent()
        .ok_or_else(|| invalid_input("node mutation audit path has no parent"))?;
    let directory_metadata = match tokio::fs::symlink_metadata(directory).await {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return Err(invalid_input(
            "node mutation audit directory must be a real directory",
        ));
    }
    if directory_metadata.permissions().mode() & 0o077 != 0 {
        return Err(invalid_input(
            "node mutation audit directory permissions are too broad",
        ));
    }
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)
        .await
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let metadata = file.metadata().await?;
    if !metadata.file_type().is_file() {
        return Err(invalid_input(
            "node mutation audit path is not a regular file",
        ));
    }
    if metadata.permissions().mode() & 0o077 != 0 {
        return Err(invalid_input(
            "node mutation audit file permissions are too broad",
        ));
    }
    Ok(Some(file))
}

async fn append_jsonl_event(event: &JsonlAuditEvent) -> std::io::Result<()> {
    event.validate()?;
    let append_guard = JSONL_AUDIT_APPEND_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .await;
    let path = audit_path()?;
    let directory = path
        .parent()
        .ok_or_else(|| invalid_input("node mutation audit path has no parent"))?;
    tokio::fs::create_dir_all(directory).await?;
    let directory_metadata = tokio::fs::symlink_metadata(directory).await?;
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return Err(invalid_input(
            "node mutation audit directory must be a real directory",
        ));
    }
    tokio::fs::set_permissions(directory, std::fs::Permissions::from_mode(0o700)).await?;
    let mut options = OpenOptions::new();
    options
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW);
    let file = options.open(&path).await?;
    let metadata = file.metadata().await?;
    if !metadata.file_type().is_file() {
        return Err(invalid_input(
            "node mutation audit path is not a regular file",
        ));
    }
    if metadata.permissions().mode() & 0o077 != 0 {
        return Err(invalid_input(
            "node mutation audit file permissions are too broad",
        ));
    }
    let mut line = serde_json::to_vec(event)
        .map_err(|error| invalid_data(format!("failed to encode mutation audit: {error}")))?;
    line.push(b'\n');
    let file = file.into_std().await;
    let directory = directory.to_path_buf();
    // The record is written as one append on a blocking task that owns the
    // append lock. Dropping the awaiting future (e.g. a startup aborted by
    // SIGTERM) can then no longer leave a torn line that a later recovery
    // rejects as invalid JSON.
    tokio::task::spawn_blocking(move || write_jsonl_line(append_guard, file, &line, &directory))
        .await
        .map_err(std::io::Error::other)?
}

fn write_jsonl_line(
    _append_guard: tokio::sync::MutexGuard<'static, ()>,
    mut file: std::fs::File,
    line: &[u8],
    directory: &Path,
) -> std::io::Result<()> {
    use std::io::Write;

    file.write_all(line)?;
    file.sync_all()?;
    sync_directory(directory)
}

async fn load_current_node_hashes() -> std::io::Result<HashMap<String, String>> {
    let file = File::open(nodes_path()).await?;
    let mut lines = BufReader::new(file).lines();
    let mut result = HashMap::new();
    let mut line_number = 0usize;
    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let value: serde_json::Value = serde_json::from_str(&line).map_err(|error| {
            invalid_data(format!("invalid node JSONL at line {line_number}: {error}"))
        })?;
        let node = map_json_to_node(&value).ok_or_else(|| {
            invalid_data(format!("invalid node projection at line {line_number}"))
        })?;
        let hash = node_hash(&node)
            .map_err(|error| invalid_data(format!("failed to hash node: {error}")))?;
        result.insert(node.id.clone(), hash);
    }
    Ok(result)
}

fn sync_directory(path: &Path) -> std::io::Result<()> {
    std::fs::File::open(path)?.sync_all()
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn invalid_data(message: impl Into<String>) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, message.into())
}

fn invalid_input(message: impl Into<String>) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        routes::nodes::{Location, SearchVisibility},
        test_helpers::EnvGuard,
    };
    use serial_test::serial;
    use tempfile::tempdir;

    fn node(id: &str, title: &str) -> Node {
        Node {
            id: id.to_string(),
            kind: "commons".to_string(),
            title: title.to_string(),
            created_at: "2026-07-31T00:00:00Z".to_string(),
            updated_at: "2026-07-31T00:00:00Z".to_string(),
            has_authoritative_created_at: true,
            created_by_account_id: Some("actor-a".to_string()),
            search_visibility: SearchVisibility::Public,
            summary: None,
            info: None,
            tags: vec![],
            address: None,
            location: Location { lat: 1.0, lon: 2.0 },
        }
    }

    #[test]
    fn actor_hash_is_domain_separated_and_stable() {
        let first = actor_subject_hash("account-a");
        assert_eq!(first, actor_subject_hash("account-a"));
        assert_ne!(first, actor_subject_hash("account-b"));
        assert_ne!(first, hex::encode(Sha256::digest(b"account-a")));
        assert!(is_sha256(&first));
    }

    #[test]
    fn node_hash_changes_with_public_state() {
        let first = node("node-a", "First");
        let second = node("node-a", "Second");
        assert_eq!(node_hash(&first).unwrap(), node_hash(&first).unwrap());
        assert_ne!(node_hash(&first).unwrap(), node_hash(&second).unwrap());
    }

    #[test]
    fn audit_shape_requires_operation_specific_after_hash() {
        let before = node("node-a", "First");
        let after = node("node-a", "Second");
        let replace = NodeMutationAudit::new(
            NodeMutationOperation::Replace,
            "node-a",
            "actor-a",
            &before,
            Some(&after),
        )
        .unwrap();
        JsonlAuditEvent::from_audit(&replace, JsonlAuditState::Prepared, false)
            .validate()
            .unwrap();
        let delete = NodeMutationAudit::new(
            NodeMutationOperation::Delete,
            "node-a",
            "actor-a",
            &before,
            None,
        )
        .unwrap();
        JsonlAuditEvent::from_audit(&delete, JsonlAuditState::Prepared, false)
            .validate()
            .unwrap();
    }

    fn write_nodes(directory: &Path, nodes: &[Node]) {
        std::fs::create_dir_all(directory).unwrap();
        let content = nodes
            .iter()
            .map(|node| serde_json::to_string(node).unwrap())
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(directory.join("demo.nodes.jsonl"), format!("{content}\n")).unwrap();
    }

    #[tokio::test]
    #[serial]
    async fn recovery_commits_prepared_replace_and_keeps_receipt_private() {
        let directory = tempdir().unwrap();
        let _env = EnvGuard::set("GEWEBE_IN_DIR", directory.path().to_str().unwrap());
        let before = node("node-a", "Before");
        let mut after = node("node-a", "After");
        after.updated_at = "2026-07-31T01:00:00Z".to_string();
        write_nodes(directory.path(), std::slice::from_ref(&after));
        let audit = NodeMutationAudit::new(
            NodeMutationOperation::Replace,
            "node-a",
            "raw-account-id-must-not-appear",
            &before,
            Some(&after),
        )
        .unwrap();
        prepare_jsonl_audit(&audit).await.unwrap();

        let summary = recover_jsonl_audit().await.unwrap();
        assert_eq!(summary.committed, 1);
        assert_eq!(summary.aborted, 0);

        let path = audit_path().unwrap();
        let content = std::fs::read_to_string(&path).unwrap();
        assert!(!content.contains("raw-account-id-must-not-appear"));
        assert!(content.contains("\"state\":\"committed\""));
        assert!(content.contains("\"recovered\":true"));
        assert_eq!(
            std::fs::metadata(path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[tokio::test]
    #[serial]
    async fn recovery_aborts_prepared_delete_when_node_is_unchanged() {
        let directory = tempdir().unwrap();
        let _env = EnvGuard::set("GEWEBE_IN_DIR", directory.path().to_str().unwrap());
        let before = node("node-a", "Before");
        write_nodes(directory.path(), std::slice::from_ref(&before));
        let audit = NodeMutationAudit::new(
            NodeMutationOperation::Delete,
            "node-a",
            "actor-a",
            &before,
            None,
        )
        .unwrap();
        prepare_jsonl_audit(&audit).await.unwrap();

        let summary = recover_jsonl_audit().await.unwrap();
        assert_eq!(summary.committed, 0);
        assert_eq!(summary.aborted, 1);
        let content = std::fs::read_to_string(audit_path().unwrap()).unwrap();
        assert!(content.contains("\"state\":\"aborted\""));
    }

    #[tokio::test]
    #[serial]
    async fn recovery_rejects_ambiguous_prepared_replace() {
        let directory = tempdir().unwrap();
        let _env = EnvGuard::set("GEWEBE_IN_DIR", directory.path().to_str().unwrap());
        let before = node("node-a", "Before");
        let mut expected = node("node-a", "Expected");
        expected.updated_at = "2026-07-31T01:00:00Z".to_string();
        let mut ambiguous = node("node-a", "Third state");
        ambiguous.updated_at = "2026-07-31T02:00:00Z".to_string();
        write_nodes(directory.path(), &[ambiguous]);
        let audit = NodeMutationAudit::new(
            NodeMutationOperation::Replace,
            "node-a",
            "actor-a",
            &before,
            Some(&expected),
        )
        .unwrap();
        prepare_jsonl_audit(&audit).await.unwrap();

        let error = recover_jsonl_audit().await.unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("cannot be reconciled"));
    }

    #[tokio::test]
    #[serial]
    async fn cancelled_audit_append_never_leaves_a_torn_line() {
        let directory = tempdir().unwrap();
        let _env = EnvGuard::set("GEWEBE_IN_DIR", directory.path().to_str().unwrap());
        let before = node("node-a", "Before");
        write_nodes(directory.path(), std::slice::from_ref(&before));
        let audit = NodeMutationAudit::new(
            NodeMutationOperation::Delete,
            "node-a",
            "actor-a",
            &before,
            None,
        )
        .unwrap();

        // Drop the append after a varying number of polls, the way an
        // aborted startup drops `recover_jsonl_audit`.
        for polls in 0..64 {
            let mut append = Box::pin(prepare_jsonl_audit(&audit));
            for _ in 0..polls {
                if futures_util::poll!(append.as_mut()).is_ready() {
                    break;
                }
                tokio::task::yield_now().await;
            }
            drop(append);
            // A write that already started finishes while holding the lock.
            drop(
                JSONL_AUDIT_APPEND_LOCK
                    .get_or_init(|| Mutex::new(()))
                    .lock()
                    .await,
            );
        }

        let content = std::fs::read_to_string(audit_path().unwrap()).unwrap();
        assert!(!content.is_empty());
        assert!(content.ends_with('\n'));
        for line in content.lines() {
            serde_json::from_str::<JsonlAuditEvent>(line).unwrap();
        }
        let summary = recover_jsonl_audit().await.unwrap();
        assert_eq!(summary.aborted, 1);
    }

    #[tokio::test]
    #[serial]
    async fn audit_append_rejects_symlink_target() {
        use std::os::unix::fs::symlink;

        let directory = tempdir().unwrap();
        let _env = EnvGuard::set("GEWEBE_IN_DIR", directory.path().to_str().unwrap());
        write_nodes(directory.path(), &[node("node-a", "Before")]);
        let audit_directory = directory.path().join(AUDIT_DIRECTORY);
        std::fs::create_dir_all(&audit_directory).unwrap();
        std::fs::set_permissions(&audit_directory, std::fs::Permissions::from_mode(0o700)).unwrap();
        let outside = directory.path().join("outside.jsonl");
        std::fs::write(&outside, "unchanged\n").unwrap();
        symlink(&outside, audit_directory.join(AUDIT_FILE)).unwrap();
        let before = node("node-a", "Before");
        let after = node("node-a", "After");
        let audit = NodeMutationAudit::new(
            NodeMutationOperation::Replace,
            "node-a",
            "actor-a",
            &before,
            Some(&after),
        )
        .unwrap();

        let recovery_error = recover_jsonl_audit().await.unwrap_err();
        assert!(matches!(
            recovery_error.raw_os_error(),
            Some(code) if code == libc::ELOOP
        ));
        let error = prepare_jsonl_audit(&audit).await.unwrap_err();
        assert!(matches!(
            error.raw_os_error(),
            Some(code) if code == libc::ELOOP
        ));
        assert_eq!(std::fs::read_to_string(outside).unwrap(), "unchanged\n");
    }
}
