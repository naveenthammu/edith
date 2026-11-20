//! Durable, principal-scoped personal records. No provider or speech I/O.
use crate::{actions::Error, knowledge::Knowledge};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .try_into()
        .unwrap_or(i64::MAX - 31_622_400)
}
fn encoded<T: Serialize>(value: &T) -> Result<String, Error> {
    serde_json::to_string(value).map_err(|_| Error::Storage)
}
fn hash(value: &Value) -> Result<String, Error> {
    Ok(format!("{:x}", Sha256::digest(encoded(value)?.as_bytes())))
}
fn key(value: &str) -> Result<(), Error> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err(Error::BadRequest("Invalid idempotency key"));
    }
    Ok(())
}
fn bounded(value: &str, max: usize, multiline: bool) -> bool {
    !value.trim().is_empty()
        && value.chars().count() <= max
        && !value
            .chars()
            .any(|c| c.is_control() && !(multiline && matches!(c, '\n' | '\r' | '\t')))
}
fn decode<T: DeserializeOwned>(raw: String) -> Result<T, Error> {
    serde_json::from_str(&raw).map_err(|_| Error::Storage)
}
// Table names below are static internal constants; all caller values are bound parameters.
fn read<T: DeserializeOwned>(
    db: &Connection,
    table: &str,
    owner: &str,
    id: &str,
) -> Result<T, Error> {
    let raw: Option<String> = db
        .query_row(
            &format!("SELECT data FROM {table} WHERE owner_id=?1 AND id=?2"),
            params![owner, id],
            |r| r.get(0),
        )
        .optional()?;
    decode(raw.ok_or(Error::NotFound)?)
}
fn list<T: DeserializeOwned>(db: &Connection, table: &str, owner: &str) -> Result<Vec<T>, Error> {
    let mut statement=db.prepare(&format!("SELECT data FROM {table} WHERE owner_id=?1 ORDER BY json_extract(data,'$.created_at') DESC,id DESC LIMIT 1000"))?;
    let raws = statement
        .query_map([owner], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    raws.into_iter().map(decode).collect()
}
fn save<T: Serialize>(
    db: &Connection,
    table: &str,
    owner: &str,
    id: &str,
    value: &T,
) -> Result<(), Error> {
    if db.execute(
        &format!("UPDATE {table} SET data=?3 WHERE owner_id=?1 AND id=?2"),
        params![owner, id, encoded(value)?],
    )? != 1
    {
        return Err(Error::NotFound);
    }
    Ok(())
}
pub fn initialize(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch("BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,argument_hash TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),UNIQUE(owner_id,idempotency_key));
      CREATE INDEX IF NOT EXISTS reminders_due ON reminders(owner_id,json_extract(data,'$.status'),json_extract(data,'$.due_at'));
      CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)));
      CREATE INDEX IF NOT EXISTS contacts_owner ON contacts(owner_id);
      CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,argument_hash TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),UNIQUE(owner_id,idempotency_key));
      CREATE INDEX IF NOT EXISTS messages_owner ON messages(owner_id);
      UPDATE messages SET data=json_set(data,'$.status','unknown','$.recovery_note','Execution interrupted; provider outcome unverified. Reconcile before proposing another message.') WHERE json_extract(data,'$.status')='executing';
      UPDATE contacts SET data=json_insert(data,'$.phone_e164',NULL) WHERE json_type(data,'$.phone_e164') IS NULL;
      PRAGMA user_version=6; COMMIT;")
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewReminder {
    pub text: String,
    pub due_at: i64,
    pub idempotency_key: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Delivery {
    pub delivery_id: String,
    pub status: DeliveryStatus,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryStatus {
    Announced,
    Retry,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Reminder {
    pub id: String,
    pub text: String,
    pub due_at: i64,
    pub created_at: i64,
    pub updated_at: i64,
    pub status: String,
    pub announced_at: Option<i64>,
    pub acknowledged_at: Option<i64>,
    pub cancelled_at: Option<i64>,
    pub delivery_id: Option<String>,
    pub lease_expires_at: Option<i64>,
    last_delivery_id: Option<String>,
    last_delivery_status: Option<DeliveryStatus>,
}
impl Reminder {
    pub fn view(&self, lease: bool) -> Value {
        let mut value = json!(self);
        let object = value.as_object_mut().expect("record object");
        object.remove("last_delivery_id");
        object.remove("last_delivery_status");
        if !lease {
            object.remove("delivery_id");
            object.remove("lease_expires_at");
        }
        value
    }
}
impl Knowledge {
    pub fn reminders(&self, owner: &str) -> Result<Vec<Reminder>, Error> {
        list(&self.db, "reminders", owner)
    }
    pub fn create_reminder(
        &mut self,
        owner: &str,
        input: NewReminder,
        now: i64,
    ) -> Result<(Reminder, bool), Error> {
        key(&input.idempotency_key)?;
        if !bounded(&input.text, 400, false) {
            return Err(Error::BadRequest(
                "Reminder text must contain one to 400 characters",
            ));
        }
        let digest = hash(&json!({"text":input.text,"due_at":input.due_at}))?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior: Option<(String, String)> = tx
            .query_row(
                "SELECT data,argument_hash FROM reminders WHERE owner_id=?1 AND idempotency_key=?2",
                params![owner, input.idempotency_key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((data, old)) = prior {
            if old != digest {
                return Err(Error::Conflict(
                    "Idempotency key already binds different reminder arguments",
                ));
            }
            return Ok((decode(data)?, false));
        }
        if input.due_at < now.saturating_sub(60) || input.due_at > now.saturating_add(366 * 86400) {
            return Err(Error::BadRequest(
                "Reminder time must be within 60 seconds past and 366 days future",
            ));
        }
        let record = Reminder {
            id: Uuid::new_v4().to_string(),
            text: input.text,
            due_at: input.due_at,
            created_at: now,
            updated_at: now,
            status: "scheduled".into(),
            announced_at: None,
            acknowledged_at: None,
            cancelled_at: None,
            delivery_id: None,
            lease_expires_at: None,
            last_delivery_id: None,
            last_delivery_status: None,
        };
        tx.execute("INSERT INTO reminders(id,owner_id,idempotency_key,argument_hash,data) VALUES(?1,?2,?3,?4,?5)",params![record.id,owner,input.idempotency_key,digest,encoded(&record)?])?;
        tx.commit()?;
        Ok((record, true))
    }
    pub fn close_reminder(
        &mut self,
        owner: &str,
        id: &str,
        acknowledge: bool,
        now: i64,
    ) -> Result<Reminder, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut record: Reminder = read(&tx, "reminders", owner, id)?;
        let target = if acknowledge {
            "acknowledged"
        } else {
            "cancelled"
        };
        if record.status == target {
            return Ok(record);
        }
        if matches!(record.status.as_str(), "acknowledged" | "cancelled") {
            return Err(Error::Conflict(
                "Reminder already has a different terminal state",
            ));
        }
        record.status = target.into();
        record.updated_at = now;
        if acknowledge {
            record.acknowledged_at = Some(now);
        } else {
            record.cancelled_at = Some(now);
        }
        record.delivery_id = None;
        record.lease_expires_at = None;
        record.last_delivery_id = None;
        record.last_delivery_status = None;
        save(&tx, "reminders", owner, id, &record)?;
        tx.commit()?;
        Ok(record)
    }
    pub fn claim_due(&mut self, owner: &str, now: i64) -> Result<Option<Reminder>, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let raw:Option<String>=tx.query_row("SELECT data FROM reminders WHERE owner_id=?1 AND json_extract(data,'$.status')='scheduled' AND json_extract(data,'$.due_at')<=?2 AND (json_extract(data,'$.lease_expires_at') IS NULL OR json_extract(data,'$.lease_expires_at')<=?2) ORDER BY json_extract(data,'$.due_at'),id LIMIT 1",params![owner,now],|r|r.get(0)).optional()?;
        let Some(raw) = raw else { return Ok(None) };
        let mut record: Reminder = decode(raw)?;
        record.delivery_id = Some(Uuid::new_v4().to_string());
        record.lease_expires_at = Some(now.saturating_add(120));
        record.updated_at = now;
        record.last_delivery_id = None;
        record.last_delivery_status = None;
        save(&tx, "reminders", owner, &record.id, &record)?;
        tx.commit()?;
        Ok(Some(record))
    }
    pub fn reminder_delivery(
        &mut self,
        owner: &str,
        id: &str,
        input: Delivery,
        now: i64,
    ) -> Result<Reminder, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut record: Reminder = read(&tx, "reminders", owner, id)?;
        if record.last_delivery_id.as_deref() == Some(&input.delivery_id)
            && record.last_delivery_status == Some(input.status)
        {
            return Ok(record);
        }
        if record.status != "scheduled"
            || record.delivery_id.as_deref() != Some(&input.delivery_id)
            || record.lease_expires_at.is_none_or(|expiry| expiry <= now)
        {
            return Err(Error::Conflict(
                "Delivery lease is stale, expired, or invalidated",
            ));
        }
        record.last_delivery_id = record.delivery_id.take();
        record.last_delivery_status = Some(input.status);
        record.lease_expires_at = None;
        record.updated_at = now;
        if input.status == DeliveryStatus::Announced {
            record.status = "announced".into();
            record.announced_at = Some(now);
        }
        save(&tx, "reminders", owner, id, &record)?;
        tx.commit()?;
        Ok(record)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn required<T>(value: Result<T, Error>) -> T {
        value.unwrap_or_else(|_| panic!("Expected successful storage operation"))
    }
    fn reminder(key: &str, time: i64) -> NewReminder {
        NewReminder {
            text: "Synthetic reminder".into(),
            due_at: time,
            idempotency_key: key.into(),
        }
    }
    fn new_message(contact: &Contact, key: &str) -> NewMessage {
        NewMessage {
            contact_id: contact.id.clone(),
            contact_version: contact.version.clone(),
            subject: "Synthetic subject".into(),
            body: "Synthetic body".into(),
            idempotency_key: key.into(),
        }
    }
    fn decision(message: &Message) -> crate::actions::Decide {
        crate::actions::Decide {
            decision: crate::actions::Decision::Approve,
            argument_hash: message.argument_hash.clone(),
        }
    }
    #[test]
    fn due_lease_expiry_reclaims_once_and_survives_restart_without_false_delivery() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("lease.db");
        let mut store = Knowledge::open(&path).unwrap();
        let (item, _) = required(store.create_reminder("owner", reminder("due", 1000), 1000));
        assert!(required(store.claim_due("owner", 999)).is_none());
        let first = required(store.claim_due("owner", 1000)).unwrap();
        assert_eq!(first.lease_expires_at, Some(1120));
        drop(store);
        let mut store = Knowledge::open(&path).unwrap();
        assert!(required(store.claim_due("owner", 1119)).is_none());
        let next = required(store.claim_due("owner", 1120)).unwrap();
        assert_ne!(first.delivery_id, next.delivery_id);
        assert!(matches!(
            store.reminder_delivery(
                "owner",
                &item.id,
                Delivery {
                    delivery_id: first.delivery_id.unwrap(),
                    status: DeliveryStatus::Announced
                },
                1121
            ),
            Err(Error::Conflict(_))
        ));
        required(store.close_reminder("owner", &item.id, true, 1122));
        assert!(matches!(
            store.reminder_delivery(
                "owner",
                &item.id,
                Delivery {
                    delivery_id: next.delivery_id.unwrap(),
                    status: DeliveryStatus::Retry
                },
                1123
            ),
            Err(Error::Conflict(_))
        ));
        assert!(required(store.claim_due("owner", 10000)).is_none());
    }
    #[test]
    fn expired_lease_cannot_report_success_and_creation_checks_clock_boundaries() {
        let dir = tempfile::TempDir::new().unwrap();
        let mut store = Knowledge::open(&dir.path().join("time.db")).unwrap();
        required(store.create_reminder("owner", reminder("boundary", 940), 1000));
        for due in [939, 1000 + 366 * 86400 + 1, i64::MIN, i64::MAX] {
            assert!(matches!(
                store.create_reminder("owner", reminder("invalid", due), 1000),
                Err(Error::BadRequest(_))
            ));
        }
        let claim = required(store.claim_due("owner", 1000)).unwrap();
        assert!(matches!(
            store.reminder_delivery(
                "owner",
                &claim.id,
                Delivery {
                    delivery_id: claim.delivery_id.unwrap(),
                    status: DeliveryStatus::Announced
                },
                1120
            ),
            Err(Error::Conflict(_))
        ));
        // A late retry of an already committed creation remains idempotent.
        assert!(!required(store.create_reminder("owner", reminder("boundary", 940), 100000)).1);
    }
    #[test]
    fn message_expiry_and_contact_aba_do_not_restore_authority() {
        let dir = tempfile::TempDir::new().unwrap();
        let mut store = Knowledge::open(&dir.path().join("expiry.db")).unwrap();
        let contact = required(store.create_contact(
            "owner",
            NewContact {
                display_name: "Friend".into(),
                email: Some("friend@example.test".into()),
                phone_e164: None,
            },
            1000,
        ));
        let (expired, _) =
            required(store.propose_message("owner", new_message(&contact, "expires"), 1000));
        assert!(matches!(
            store.decide_message("owner", &expired.id, decision(&expired), 1300),
            Err(Error::Conflict(_))
        ));
        let (late_claim, _) =
            required(store.propose_message("owner", new_message(&contact, "late-claim"), 1000));
        required(store.decide_message("owner", &late_claim.id, decision(&late_claim), 1299));
        assert!(matches!(
            store.claim_message("owner", &late_claim.id, 1300),
            Err(Error::Conflict(_))
        ));
        let (aba, _) = required(store.propose_message("owner", new_message(&contact, "aba"), 1000));
        required(store.decide_message("owner", &aba.id, decision(&aba), 1001));
        let changed = required(store.update_contact(
            "owner",
            &contact.id,
            UpdateContact {
                expected_version: contact.version.clone(),
                display_name: "Changed".into(),
                email: Some("friend@example.test".into()),
                phone_e164: None,
            },
            1002,
        ));
        let reverted = required(store.update_contact(
            "owner",
            &contact.id,
            UpdateContact {
                expected_version: changed.version,
                display_name: contact.display_name,
                email: contact.email,
                phone_e164: None,
            },
            1003,
        ));
        assert_ne!(contact.version, reverted.version);
        assert!(matches!(
            store.claim_message("owner", &aba.id, 1004),
            Err(Error::Conflict(_))
        ));
    }
    #[test]
    fn independent_connections_cannot_both_claim_one_due_reminder() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("race.db");
        let mut store = Knowledge::open(&path).unwrap();
        required(store.create_reminder("owner", reminder("race", 1000), 1000));
        drop(store);
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
        let threads: Vec<_> = (0..2)
            .map(|_| {
                let path = path.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    let mut store = Knowledge::open(&path).unwrap();
                    barrier.wait();
                    required(store.claim_due("owner", 1000)).is_some()
                })
            })
            .collect();
        assert_eq!(
            threads
                .into_iter()
                .map(|t| usize::from(t.join().unwrap()))
                .sum::<usize>(),
            1
        );
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewContact {
    pub display_name: String,
    #[serde(default)]
    pub email: Option<String>,
    #[serde(default)]
    pub phone_e164: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateContact {
    pub expected_version: String,
    pub display_name: String,
    #[serde(default)]
    pub email: Option<String>,
    #[serde(default)]
    pub phone_e164: Option<String>,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Contact {
    pub id: String,
    pub display_name: String,
    pub email: Option<String>,
    #[serde(default)]
    pub phone_e164: Option<String>,
    pub version: String,
    pub created_at: i64,
    pub updated_at: i64,
}
fn valid_email(email: &str) -> bool {
    if email.len() > 254 || !email.is_ascii() {
        return false;
    }
    let parts: Vec<_> = email.split('@').collect();
    if parts.len() != 2 {
        return false;
    }
    let (local, domain) = (parts[0], parts[1]);
    !local.is_empty()
        && local.len() <= 64
        && !local.starts_with('.')
        && !local.ends_with('.')
        && !local.contains("..")
        && local
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".!#$%&'*+-/=?^_`{|}~".contains(&b))
        && domain.contains('.')
        && domain.split('.').all(|part| {
            !part.is_empty()
                && part.len() <= 63
                && !part.starts_with('-')
                && !part.ends_with('-')
                && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
}
fn valid_phone(phone: &str) -> bool {
    let Some(digits) = phone.strip_prefix('+') else {
        return false;
    };
    (2..=15).contains(&digits.len())
        && matches!(digits.as_bytes().first(), Some(b'1'..=b'9'))
        && digits.bytes().all(|b| b.is_ascii_digit())
}
fn contact_input(name: &str, email: Option<&str>, phone: Option<&str>) -> Result<(), Error> {
    if !bounded(name, 100, false)
        || (email.is_none() && phone.is_none())
        || email.is_some_and(|value| !valid_email(value))
        || phone.is_some_and(|value| !valid_phone(value))
    {
        return Err(Error::BadRequest(
            "Provide a bounded name and at least one valid plain email or explicit E.164 phone number",
        ));
    }
    Ok(())
}
fn contact_version() -> String {
    format!("{:x}", Sha256::digest(Uuid::new_v4().as_bytes()))
}
impl Knowledge {
    pub fn contacts(&self, owner: &str) -> Result<Vec<Contact>, Error> {
        list(&self.db, "contacts", owner)
    }
    pub fn create_contact(
        &mut self,
        owner: &str,
        input: NewContact,
        now: i64,
    ) -> Result<Contact, Error> {
        contact_input(
            &input.display_name,
            input.email.as_deref(),
            input.phone_e164.as_deref(),
        )?;
        let value = Contact {
            id: Uuid::new_v4().to_string(),
            display_name: input.display_name,
            email: input.email,
            phone_e164: input.phone_e164,
            version: contact_version(),
            created_at: now,
            updated_at: now,
        };
        self.db.execute(
            "INSERT INTO contacts(id,owner_id,data) VALUES(?1,?2,?3)",
            params![value.id, owner, encoded(&value)?],
        )?;
        Ok(value)
    }
    pub fn update_contact(
        &mut self,
        owner: &str,
        id: &str,
        input: UpdateContact,
        now: i64,
    ) -> Result<Contact, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut value: Contact = read(&tx, "contacts", owner, id)?;
        contact_input(
            &input.display_name,
            input.email.as_deref(),
            input.phone_e164.as_deref(),
        )?;
        if value.version != input.expected_version {
            return Err(Error::Conflict("Contact revision changed"));
        }
        value.display_name = input.display_name;
        value.email = input.email;
        value.phone_e164 = input.phone_e164;
        value.version = contact_version();
        value.updated_at = now;
        save(&tx, "contacts", owner, id, &value)?;
        tx.commit()?;
        Ok(value)
    }
    pub fn delete_contact(&mut self, owner: &str, id: &str) -> Result<(), Error> {
        if self.db.execute(
            "DELETE FROM contacts WHERE owner_id=?1 AND id=?2",
            params![owner, id],
        )? != 1
        {
            return Err(Error::NotFound);
        }
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewMessage {
    pub contact_id: String,
    pub contact_version: String,
    pub subject: String,
    pub body: String,
    pub idempotency_key: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct EmailPayload {
    pub contact_id: String,
    pub contact_version: String,
    pub recipient: String,
    pub subject: String,
    pub body: String,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Provider {
    Gmail,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryVerification {
    Unverified,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EmailEvidence {
    pub provider: Provider,
    pub recipient: String,
    pub provider_message_id: Option<String>,
    pub delivery_status: DeliveryVerification,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MessageResultStatus {
    Accepted,
    Failed,
    Unknown,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MessageResult {
    pub status: MessageResultStatus,
    pub evidence: EmailEvidence,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Message {
    pub id: String,
    pub kind: String,
    pub payload: EmailPayload,
    pub argument_hash: String,
    pub created_at: i64,
    pub expires_at: i64,
    pub status: String,
    pub evidence: Option<EmailEvidence>,
    pub recovery_note: Option<String>,
    decision: Option<crate::actions::Decision>,
}
impl Message {
    pub fn view(&self) -> Value {
        let mut v = json!(self);
        v.as_object_mut().expect("record object").remove("decision");
        v
    }
}
fn current_contact(db: &Connection, owner: &str, payload: &EmailPayload) -> Result<(), Error> {
    let contact = match read::<Contact>(db, "contacts", owner, &payload.contact_id) {
        Ok(c) => c,
        Err(Error::NotFound) => {
            return Err(Error::Conflict(
                "The proposed contact is no longer available",
            ))
        }
        Err(e) => return Err(e),
    };
    if contact.version != payload.contact_version
        || contact.email.as_deref() != Some(payload.recipient.as_str())
    {
        return Err(Error::Conflict("The proposed contact has changed"));
    }
    Ok(())
}
impl Knowledge {
    pub fn messages(&self, owner: &str) -> Result<Vec<Message>, Error> {
        list(&self.db, "messages", owner)
    }
    pub fn message(&self, owner: &str, id: &str) -> Result<Message, Error> {
        read(&self.db, "messages", owner, id)
    }
    pub fn propose_message(
        &mut self,
        owner: &str,
        input: NewMessage,
        now: i64,
    ) -> Result<(Message, bool), Error> {
        key(&input.idempotency_key)?;
        if !bounded(&input.subject, 200, false)
            || !bounded(&input.body, 4000, true)
            || input.contact_id.len() > 128
            || input.contact_version.len() != 64
        {
            return Err(Error::BadRequest("Invalid email proposal fields"));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior: Option<String> = tx
            .query_row(
                "SELECT data FROM messages WHERE owner_id=?1 AND idempotency_key=?2",
                params![owner, input.idempotency_key],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(raw) = prior {
            let record: Message = decode(raw)?;
            let p = &record.payload;
            if p.contact_id != input.contact_id
                || p.contact_version != input.contact_version
                || p.subject != input.subject
                || p.body != input.body
            {
                return Err(Error::Conflict(
                    "Idempotency key already binds different message arguments",
                ));
            }
            return Ok((record, false));
        }
        let contact: Contact = read(&tx, "contacts", owner, &input.contact_id)?;
        if contact.version != input.contact_version {
            return Err(Error::Conflict("Contact revision changed"));
        }
        let payload = EmailPayload {
            contact_id: contact.id,
            contact_version: contact.version,
            recipient: contact.email.ok_or(Error::BadRequest(
                "The selected contact has no email address",
            ))?,
            subject: input.subject,
            body: input.body,
        };
        let argument_hash = hash(&json!({"kind":"contact.email","payload":payload}))?;
        let value = Message {
            id: Uuid::new_v4().to_string(),
            kind: "contact.email".into(),
            payload,
            argument_hash,
            created_at: now,
            expires_at: now.saturating_add(300),
            status: "awaiting_approval".into(),
            evidence: None,
            recovery_note: None,
            decision: None,
        };
        tx.execute("INSERT INTO messages(id,owner_id,idempotency_key,argument_hash,data) VALUES(?1,?2,?3,?4,?5)",params![value.id,owner,input.idempotency_key,value.argument_hash,encoded(&value)?])?;
        tx.commit()?;
        Ok((value, true))
    }
    pub fn decide_message(
        &mut self,
        owner: &str,
        id: &str,
        input: crate::actions::Decide,
        now: i64,
    ) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut value: Message = read(&tx, "messages", owner, id)?;
        if input.argument_hash != value.argument_hash {
            return Err(Error::Conflict("Approval arguments do not match"));
        }
        if let Some(previous) = value.decision {
            if previous == input.decision {
                return Ok(value);
            }
            return Err(Error::Conflict("A different decision was already recorded"));
        }
        if value.status != "awaiting_approval" || now >= value.expires_at {
            return Err(Error::Conflict(
                "Proposal is no longer awaiting fresh approval",
            ));
        }
        if input.decision == crate::actions::Decision::Approve {
            current_contact(&tx, owner, &value.payload)?;
            value.status = "approved".into();
        } else {
            value.status = "rejected".into();
        }
        value.decision = Some(input.decision);
        save(&tx, "messages", owner, id, &value)?;
        tx.commit()?;
        Ok(value)
    }
    pub fn claim_message(&mut self, owner: &str, id: &str, now: i64) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut value: Message = read(&tx, "messages", owner, id)?;
        if value.status != "approved" || now >= value.expires_at {
            return Err(Error::Conflict("Message cannot be claimed"));
        }
        current_contact(&tx, owner, &value.payload)?;
        value.status = "executing".into();
        save(&tx, "messages", owner, id, &value)?;
        tx.commit()?;
        Ok(value)
    }
    pub fn finish_message(
        &mut self,
        owner: &str,
        id: &str,
        input: MessageResult,
    ) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut value: Message = read(&tx, "messages", owner, id)?;
        let evidence = &input.evidence;
        if evidence.recipient != value.payload.recipient
            || evidence.provider_message_id.as_ref().is_some_and(|id| {
                id.is_empty() || id.len() > 256 || !id.bytes().all(|b| (33..=126).contains(&b))
            })
            || (input.status == MessageResultStatus::Accepted
                && evidence.provider_message_id.is_none())
            || (input.status == MessageResultStatus::Failed
                && evidence.provider_message_id.is_some())
        {
            return Err(Error::BadRequest(
                "Provider evidence does not match the proposed recipient or result",
            ));
        }
        let status = match input.status {
            MessageResultStatus::Accepted => "accepted",
            MessageResultStatus::Failed => "failed",
            MessageResultStatus::Unknown => "unknown",
        };
        if value.status == status && value.evidence.as_ref() == Some(evidence) {
            return Ok(value);
        }
        if value.status != "executing" {
            return Err(Error::Conflict(
                "Message result is immutable or execution was not claimed",
            ));
        }
        value.status = status.into();
        value.evidence = Some(input.evidence);
        save(&tx, "messages", owner, id, &value)?;
        tx.commit()?;
        Ok(value)
    }
}
