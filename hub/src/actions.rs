use crate::{config::valid_entity, knowledge::Knowledge};
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    time::{SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
pub enum Kind {
    #[serde(rename = "home.power")]
    HomePower,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PowerPayload {
    pub entity_id: String,
    pub on: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub kind: Kind,
    pub payload: PowerPayload,
    pub idempotency_key: String,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Approve,
    Reject,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Decide {
    pub decision: Decision,
    pub argument_hash: String,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RequestedState {
    On,
    Off,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ObservedState {
    On,
    Off,
    Unknown,
    Unavailable,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CommandStatus {
    Accepted,
    Rejected,
    Unknown,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub entity_id: String,
    pub requested_state: RequestedState,
    pub observed_state: ObservedState,
    pub command_status: CommandStatus,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ResultStatus {
    Verified,
    Failed,
    Unknown,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Completion {
    pub status: ResultStatus,
    pub evidence: Evidence,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    AwaitingApproval,
    Approved,
    Rejected,
    Executing,
    Verified,
    Failed,
    Unknown,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Action {
    pub id: String,
    pub kind: Kind,
    pub payload: PowerPayload,
    pub argument_hash: String,
    pub summary: String,
    pub created_at: i64,
    pub expires_at: i64,
    pub status: Status,
    pub evidence: Option<Evidence>,
    pub recovery_note: Option<String>,
    #[serde(skip)]
    pub decision: Option<Decision>,
}
pub enum Error {
    BadRequest(&'static str),
    Forbidden,
    NotFound,
    Conflict(&'static str),
    Storage,
}
impl From<rusqlite::Error> for Error {
    fn from(_: rusqlite::Error) -> Self {
        Self::Storage
    }
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .try_into()
        .unwrap_or(i64::MAX - 300)
}
fn encoded<T: Serialize>(value: &T) -> Result<String, Error> {
    serde_json::to_string(value).map_err(|_| Error::Storage)
}
fn text<T: Serialize>(value: &T) -> Result<String, Error> {
    Ok(encoded(value)?.trim_matches('"').to_owned())
}

pub fn initialize(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch("BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,argument_hash TEXT NOT NULL,summary TEXT NOT NULL,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,status TEXT NOT NULL,decision TEXT,evidence TEXT,recovery_note TEXT,idempotency_key TEXT NOT NULL,UNIQUE(principal_id,idempotency_key));
        CREATE INDEX IF NOT EXISTS actions_owner ON actions(principal_id,created_at);
        UPDATE actions SET status='unknown',recovery_note='Execution interrupted; outcome unverified. Reconcile before proposing another action.' WHERE status='executing';
        COMMIT;")
}
const COLUMNS:&str="id,kind,payload,argument_hash,summary,created_at,expires_at,status,decision,evidence,recovery_note";
fn decode<T: serde::de::DeserializeOwned>(s: String, index: usize) -> rusqlite::Result<T> {
    serde_json::from_str(&s).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(index, rusqlite::types::Type::Text, Box::new(e))
    })
}
fn row(r: &Row<'_>) -> rusqlite::Result<Action> {
    let decision: Option<String> = r.get(8)?;
    let evidence: Option<String> = r.get(9)?;
    Ok(Action {
        id: r.get(0)?,
        kind: decode(format!("\"{}\"", r.get::<_, String>(1)?), 1)?,
        payload: decode(r.get(2)?, 2)?,
        argument_hash: r.get(3)?,
        summary: r.get(4)?,
        created_at: r.get(5)?,
        expires_at: r.get(6)?,
        status: decode(format!("\"{}\"", r.get::<_, String>(7)?), 7)?,
        decision: decision
            .map(|s| decode(format!("\"{s}\""), 8))
            .transpose()?,
        evidence: evidence.map(|s| decode(s, 9)).transpose()?,
        recovery_note: r.get(10)?,
    })
}
fn read(db: &Connection, owner: &str, id: &str) -> Result<Action, Error> {
    db.query_row(
        &format!("SELECT {COLUMNS} FROM actions WHERE id=?1 AND principal_id=?2"),
        params![id, owner],
        row,
    )
    .optional()?
    .ok_or(Error::NotFound)
}
impl Knowledge {
    pub fn action(&self, owner: &str, id: &str) -> Result<Action, Error> {
        read(&self.db, owner, id)
    }
    pub fn list_actions(&self, owner: &str) -> Result<Vec<Action>, Error> {
        let mut stmt=self.db.prepare(&format!("SELECT {COLUMNS} FROM actions WHERE principal_id=?1 ORDER BY created_at DESC,id DESC LIMIT 1000"))?;
        let rows = stmt.query_map([owner], row)?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn propose(
        &mut self,
        owner: &str,
        input: Proposal,
        allowed: &HashSet<String>,
    ) -> Result<(Action, bool), Error> {
        if input.idempotency_key.is_empty()
            || input.idempotency_key.len() > 128
            || !input
                .idempotency_key
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return Err(Error::BadRequest(
                "Idempotency key must contain one to 128 letters, digits, underscores or hyphens",
            ));
        }
        if !valid_entity(&input.payload.entity_id) || !allowed.contains(&input.payload.entity_id) {
            return Err(Error::Forbidden);
        }
        let canonical = serde_json::json!({"kind":input.kind,"payload":input.payload});
        let hash = format!("{:x}", Sha256::digest(encoded(&canonical)?.as_bytes()));
        let transaction = self.db.transaction()?;
        let existing = transaction
            .query_row(
                &format!(
                    "SELECT {COLUMNS} FROM actions WHERE principal_id=?1 AND idempotency_key=?2"
                ),
                params![owner, input.idempotency_key],
                row,
            )
            .optional()?;
        if let Some(action) = existing {
            if action.argument_hash != hash {
                return Err(Error::Conflict(
                    "Idempotency key was already used for different arguments",
                ));
            }
            return Ok((action, false));
        }
        let created_at = now();
        let id = Uuid::new_v4().to_string();
        let summary = format!(
            "Turn {} {}",
            input.payload.entity_id,
            if input.payload.on { "on" } else { "off" }
        );
        transaction.execute("INSERT INTO actions(id,principal_id,kind,payload,argument_hash,summary,created_at,expires_at,status,idempotency_key) VALUES(?1,?2,'home.power',?3,?4,?5,?6,?7,'awaiting_approval',?8)",params![id,owner,encoded(&input.payload)?,hash,summary,created_at,created_at+300,input.idempotency_key])?;
        let action = read(&transaction, owner, &id)?;
        transaction.commit()?;
        Ok((action, true))
    }
    pub fn decide(
        &mut self,
        owner: &str,
        id: &str,
        input: Decide,
        allowed: &HashSet<String>,
    ) -> Result<Action, Error> {
        let transaction = self.db.transaction()?;
        let action = read(&transaction, owner, id)?;
        if input.argument_hash != action.argument_hash {
            return Err(Error::Conflict(
                "Approval arguments do not match the prepared action",
            ));
        }
        if let Some(decision) = action.decision {
            if decision == input.decision {
                return Ok(action);
            }
            return Err(Error::Conflict(
                "An existing approval decision cannot be changed",
            ));
        }
        if !allowed.contains(&action.payload.entity_id) {
            return Err(Error::Forbidden);
        }
        if action.status != Status::AwaitingApproval || now() >= action.expires_at {
            return Err(Error::Conflict(
                "Action is expired or no longer awaiting approval",
            ));
        }
        let status = if input.decision == Decision::Approve {
            "approved"
        } else {
            "rejected"
        };
        transaction.execute(
            "UPDATE actions SET status=?1,decision=?2 WHERE id=?3 AND principal_id=?4",
            params![status, text(&input.decision)?, id, owner],
        )?;
        let action = read(&transaction, owner, id)?;
        transaction.commit()?;
        Ok(action)
    }
    pub fn claim(
        &mut self,
        owner: &str,
        id: &str,
        allowed: &HashSet<String>,
    ) -> Result<Action, Error> {
        let transaction = self.db.transaction()?;
        let action = read(&transaction, owner, id)?;
        if !allowed.contains(&action.payload.entity_id) {
            return Err(Error::Forbidden);
        }
        if action.status != Status::Approved || now() >= action.expires_at {
            return Err(Error::Conflict("Action is not a fresh, unclaimed approval"));
        }
        let changed=transaction.execute("UPDATE actions SET status='executing' WHERE id=?1 AND principal_id=?2 AND status='approved' AND expires_at>?3",params![id,owner,now()])?;
        if changed != 1 {
            return Err(Error::Conflict("Action execution was already claimed"));
        }
        let action = read(&transaction, owner, id)?;
        transaction.commit()?;
        Ok(action)
    }
    pub fn finish(&mut self, owner: &str, id: &str, input: Completion) -> Result<Action, Error> {
        let transaction = self.db.transaction()?;
        let action = read(&transaction, owner, id)?;
        let status = match input.status {
            ResultStatus::Verified => Status::Verified,
            ResultStatus::Failed => Status::Failed,
            ResultStatus::Unknown => Status::Unknown,
        };
        if action.status != Status::Executing {
            if action.status == status && action.evidence.as_ref() == Some(&input.evidence) {
                return Ok(action);
            }
            return Err(Error::Conflict(
                "Action is not executing, or a different result is already recorded",
            ));
        }
        let requested = if action.payload.on {
            RequestedState::On
        } else {
            RequestedState::Off
        };
        if input.evidence.entity_id != action.payload.entity_id
            || input.evidence.requested_state != requested
        {
            return Err(Error::BadRequest(
                "Evidence does not match the prepared action",
            ));
        }
        if status == Status::Verified {
            let expected = if action.payload.on {
                ObservedState::On
            } else {
                ObservedState::Off
            };
            if input.evidence.observed_state != expected
                || input.evidence.command_status == CommandStatus::Rejected
            {
                return Err(Error::BadRequest(
                    "Verified requires the requested observed state and a non-rejected command",
                ));
            }
        }
        transaction.execute("UPDATE actions SET status=?1,evidence=?2 WHERE id=?3 AND principal_id=?4 AND status='executing'",params![text(&status)?,encoded(&input.evidence)?,id,owner])?;
        let action = read(&transaction, owner, id)?;
        transaction.commit()?;
        Ok(action)
    }
}
