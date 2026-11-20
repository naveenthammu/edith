//! Trusted worker metadata and immutable template-message journal. No Meta requests.
use crate::{
    actions::{Decide, Decision, Error},
    knowledge::Knowledge,
    personal::Contact,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use uuid::Uuid;

fn encode<T: Serialize>(v: &T) -> Result<String, Error> {
    serde_json::to_string(v).map_err(|_| Error::Storage)
}
fn decode<T: DeserializeOwned>(s: String) -> Result<T, Error> {
    serde_json::from_str(&s).map_err(|_| Error::Storage)
}
fn digest(v: &Value) -> String {
    format!("{:x}", Sha256::digest(v.to_string().as_bytes()))
}
pub(crate) fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}
pub(crate) fn numeric(s: &str) -> bool {
    !s.starts_with('0') && (1..=32).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}
fn hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn id(s: &str) -> bool {
    s.len() == 36 && Uuid::parse_str(s).is_ok()
}
fn phone(s: &str) -> bool {
    s.strip_prefix('+').is_some_and(|n| {
        !n.starts_with('0') && (2..=15).contains(&n.len()) && n.bytes().all(|b| b.is_ascii_digit())
    })
}
fn bounded(s: &str, max: usize, multiline: bool) -> bool {
    !s.trim().is_empty()
        && s.chars().count() <= max
        && !s
            .chars()
            .any(|c| c.is_control() && !(multiline && matches!(c, '\r' | '\n')))
}
fn fresh(stamp: i64, now: i64) -> bool {
    stamp >= 0 && stamp > now.saturating_sub(300) && stamp <= now.saturating_add(30)
}
#[derive(Clone)]
pub struct WorkerScope {
    pub worker_id: String,
    pub principal_id: String,
    pub connection_id: String,
    pub waba_id: String,
    pub phone_number_id: String,
}
impl WorkerScope {
    pub fn matches(&self, a: &Account) -> bool {
        self.principal_id == a.principal_id
            && self.connection_id == a.connection_id
            && self.waba_id == a.waba_id
            && self.phone_number_id == a.phone_number_id
    }
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Account {
    pub connection_id: String,
    pub principal_id: String,
    pub waba_id: String,
    pub phone_number_id: String,
    pub display_phone_number: String,
    pub verified_name: String,
    pub version: String,
    pub verified_at: i64,
}
impl Account {
    fn stable(&self) -> Value {
        let mut v = json!(self);
        v.as_object_mut().expect("object").remove("verified_at");
        v
    }
    fn valid(&self) -> bool {
        identifier(&self.connection_id)
            && identifier(&self.principal_id)
            && numeric(&self.waba_id)
            && numeric(&self.phone_number_id)
            && phone(&self.display_phone_number)
            && bounded(&self.verified_name, 256, false)
            && hash(&self.version)
    }
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub enum Category {
    UTILITY,
    MARKETING,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Template {
    pub id: String,
    pub name: String,
    pub language: String,
    pub status: String,
    pub category: Category,
    pub parameter_format: String,
    pub body: String,
    pub content_hash: String,
}
type Slots = Vec<(usize, usize, usize)>;
fn slots(body: &str) -> Result<(Slots, usize), Error> {
    let bytes = body.as_bytes();
    let mut slots = Vec::new();
    let mut used = BTreeSet::new();
    let mut at = 0usize;
    let mut previous = 0usize;
    let mut remainder = String::new();
    while at + 3 < bytes.len() {
        if bytes[at..].starts_with(b"{{") {
            let start = at + 2;
            let mut end = start;
            while end < bytes.len() && end < start + 2 && bytes[end].is_ascii_digit() {
                end += 1;
            }
            if end > start && bytes[start] != b'0' && bytes[end..].starts_with(b"}}") {
                let index = body[start..end]
                    .parse::<usize>()
                    .map_err(|_| Error::BadRequest("Invalid template parameter"))?;
                remainder.push_str(&body[previous..at]);
                slots.push((at, end + 2, index));
                used.insert(index);
                at = end + 2;
                previous = at;
                continue;
            }
        }
        at += 1;
    }
    remainder.push_str(&body[previous..]);
    if remainder.contains("{{")
        || remainder.contains("}}")
        || used.len() > 10
        || used.iter().copied().ne(1..=used.len())
    {
        return Err(Error::BadRequest(
            "Template parameters must be contiguous positional slots",
        ));
    }
    Ok((slots, used.len()))
}
impl Template {
    fn validate(&self) -> Result<(), Error> {
        let language = self.language.split('_').collect::<Vec<_>>();
        let valid_language = language.len() <= 2
            && (2..=3).contains(&language[0].len())
            && language[0].bytes().all(|b| b.is_ascii_lowercase())
            && (language.len() == 1
                || (language[1].len() == 2 && language[1].bytes().all(|b| b.is_ascii_uppercase())));
        if !numeric(&self.id)
            || self.name.is_empty()
            || self.name.len() > 512
            || !self
                .name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
            || !valid_language
            || self.status != "APPROVED"
            || self.parameter_format != "POSITIONAL"
            || !bounded(&self.body, 1024, true)
        {
            return Err(Error::BadRequest(
                "Only bounded approved body-only positional templates are supported",
            ));
        }
        slots(&self.body)?;
        let mut v = json!(self);
        v.as_object_mut().expect("object").remove("content_hash");
        if !hash(&self.content_hash) || digest(&v) != self.content_hash {
            return Err(Error::BadRequest("Template content hash does not match"));
        }
        Ok(())
    }
    fn render(&self, parameters: &[String]) -> Result<String, Error> {
        let (parts, count) = slots(&self.body)?;
        if parameters.len() != count
            || parameters
                .iter()
                .any(|p| !bounded(p, 1024, false) || p.contains("{{") || p.contains("}}"))
        {
            return Err(Error::BadRequest(
                "Supply exact bounded plain-text template parameters",
            ));
        }
        let mut output = String::new();
        let mut previous = 0;
        for (start, end, index) in parts {
            output.push_str(&self.body[previous..start]);
            output.push_str(&parameters[index - 1]);
            previous = end;
        }
        output.push_str(&self.body[previous..]);
        if !bounded(&output, 4096, true) {
            return Err(Error::BadRequest(
                "Rendered template exceeds the message limit",
            ));
        }
        Ok(output)
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindingInput {
    pub account: Account,
    pub template: Template,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Binding {
    pub id: String,
    pub version: String,
    pub account: Account,
    pub template: Template,
    pub updated_at: i64,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionStatus {
    Granted,
    Revoked,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionSource {
    ExplicitOperatorRecord,
}
fn required_option<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<String>, D::Error> {
    Option::<String>::deserialize(d)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionInput {
    pub connection_id: String,
    pub account_version: String,
    pub contact_id: String,
    pub contact_version: String,
    #[serde(deserialize_with = "required_option")]
    pub expected_version: Option<String>,
    pub status: PermissionStatus,
    pub allowed_categories: Vec<Category>,
    pub recorded_at: i64,
    pub expires_at: i64,
    pub source: PermissionSource,
    pub evidence_reference: String,
    #[serde(default)]
    pub source_note: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Permission {
    pub id: String,
    pub version: String,
    pub connection_id: String,
    pub account_version: String,
    pub contact_id: String,
    pub contact_version: String,
    pub recipient_e164: String,
    pub status: PermissionStatus,
    pub allowed_categories: Vec<Category>,
    pub recorded_at: i64,
    pub expires_at: i64,
    pub source: PermissionSource,
    pub evidence_reference: String,
    pub source_note: String,
    pub updated_at: i64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub binding_id: String,
    pub binding_version: String,
    pub contact_id: String,
    pub contact_version: String,
    pub permission_version: String,
    pub parameters: Vec<String>,
    pub idempotency_key: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Prepared {
    pub account: Account,
    pub template: Template,
    pub parameters: Vec<String>,
    pub rendered_text: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Payload {
    pub binding_id: String,
    pub binding_version: String,
    pub contact_id: String,
    pub contact_version: String,
    pub recipient_name: String,
    pub recipient_e164: String,
    pub permission_version: String,
    pub prepared: Prepared,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Provider {
    WhatsappBusiness,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryStatus {
    Unverified,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub provider: Provider,
    pub phone_number_id: String,
    pub waba_id: String,
    pub account_version: String,
    pub template_content_hash: String,
    pub recipient_e164: String,
    pub provider_message_id: Option<String>,
    pub delivery_status: DeliveryStatus,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ResultStatus {
    Accepted,
    Failed,
    Unknown,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Completion {
    pub status: ResultStatus,
    pub evidence: Evidence,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Claim {
    pub argument_hash: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Message {
    pub id: String,
    pub kind: String,
    pub payload: Payload,
    pub argument_hash: String,
    pub created_at: i64,
    pub expires_at: i64,
    pub status: String,
    pub evidence: Option<Evidence>,
    pub recovery_note: Option<String>,
    decision: Option<Decision>,
    claimed_worker: Option<String>,
}
impl Message {
    pub fn view(&self) -> Value {
        let mut v = json!(self);
        let o = v.as_object_mut().expect("object");
        o.remove("decision");
        o.remove("claimed_worker");
        v
    }
}
pub fn initialize(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch("BEGIN IMMEDIATE;
 CREATE TABLE IF NOT EXISTS whatsapp_accounts(owner_id TEXT NOT NULL,connection_id TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),PRIMARY KEY(owner_id,connection_id));
 CREATE TABLE IF NOT EXISTS whatsapp_bindings(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,connection_id TEXT NOT NULL,template_id TEXT NOT NULL,language TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),UNIQUE(owner_id,connection_id,template_id,language));
 CREATE TABLE IF NOT EXISTS whatsapp_permissions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,connection_id TEXT NOT NULL,contact_id TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),UNIQUE(owner_id,connection_id,contact_id));
 CREATE TABLE IF NOT EXISTS business_messages(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,connection_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),UNIQUE(owner_id,idempotency_key));
 CREATE INDEX IF NOT EXISTS business_messages_owner ON business_messages(owner_id,connection_id);
 UPDATE business_messages SET data=json_set(data,'$.status','unknown','$.recovery_note','Business message execution interrupted; provider outcome unverified. No automatic retry.') WHERE json_extract(data,'$.status')='executing';
 PRAGMA user_version=8;COMMIT;")
}
// Table names are internal fixed constants, never taken from a request.
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
fn list<T: DeserializeOwned>(
    db: &Connection,
    table: &str,
    owner: &str,
    connection: Option<&str>,
) -> Result<Vec<T>, Error> {
    let mut s=db.prepare(&format!("SELECT data FROM {table} WHERE owner_id=?1 AND (?2 IS NULL OR connection_id=?2) ORDER BY rowid DESC LIMIT 100"))?;
    let rows = s
        .query_map(params![owner, connection], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(decode).collect()
}
fn worker_list<T: DeserializeOwned>(
    db: &Connection,
    table: &str,
    account_path: &str,
    w: &WorkerScope,
) -> Result<Vec<T>, Error> {
    // Table/path values are fixed internal constants. Filter before the result cap.
    let mut statement=db.prepare(&format!("SELECT data FROM {table} WHERE owner_id=?1 AND connection_id=?2 AND json_extract(data,?3)=?4 AND json_extract(data,?5)=?6 AND json_extract(data,?7)=?1 ORDER BY rowid DESC LIMIT 100"))?;
    let rows = statement
        .query_map(
            params![
                w.principal_id,
                w.connection_id,
                format!("{account_path}.waba_id"),
                w.waba_id,
                format!("{account_path}.phone_number_id"),
                w.phone_number_id,
                format!("{account_path}.principal_id")
            ],
            |r| r.get::<_, String>(0),
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(decode).collect()
}
fn account(db: &Connection, owner: &str, connection: &str) -> Result<Account, Error> {
    let raw: Option<String> = db
        .query_row(
            "SELECT data FROM whatsapp_accounts WHERE owner_id=?1 AND connection_id=?2",
            params![owner, connection],
            |r| r.get(0),
        )
        .optional()?;
    decode(raw.ok_or(Error::Conflict("Verified account is unavailable"))?)
}
fn permission(
    db: &Connection,
    owner: &str,
    connection: &str,
    contact: &str,
) -> Result<Permission, Error> {
    let raw:Option<String>=db.query_row("SELECT data FROM whatsapp_permissions WHERE owner_id=?1 AND connection_id=?2 AND contact_id=?3",params![owner,connection,contact],|r|r.get(0)).optional()?;
    decode(raw.ok_or(Error::Conflict("Explicit recipient permission is required"))?)
}
fn configured(scopes: &[WorkerScope], a: &Account) -> Result<(), Error> {
    if scopes.iter().any(|s| s.matches(a)) {
        Ok(())
    } else {
        Err(Error::Conflict(
            "The business connection is no longer authorized",
        ))
    }
}
fn valid_binding(
    db: &Connection,
    b: &Binding,
    scopes: &[WorkerScope],
    now: Option<i64>,
) -> Result<(), Error> {
    configured(scopes, &b.account)?;
    let current = account(db, &b.account.principal_id, &b.account.connection_id)?;
    if current.stable() != b.account.stable()
        || now.is_some_and(|now| !fresh(b.account.verified_at, now))
    {
        return Err(Error::Conflict(
            "The account or verified template binding changed or expired",
        ));
    }
    Ok(())
}
fn valid_permission(
    p: &Permission,
    b: &Binding,
    c: &Contact,
    version: &str,
    now: i64,
) -> Result<(), Error> {
    if p.version != version
        || p.status != PermissionStatus::Granted
        || p.expires_at <= now
        || p.account_version != b.account.version
        || p.connection_id != b.account.connection_id
        || p.contact_version != c.version
        || c.phone_e164.as_deref() != Some(p.recipient_e164.as_str())
        || !p.allowed_categories.contains(&b.template.category)
    {
        return Err(Error::Conflict(
            "Recipient permission is changed, expired, revoked or insufficient",
        ));
    }
    Ok(())
}
fn validate_current(
    db: &Connection,
    owner: &str,
    v: &Message,
    scopes: &[WorkerScope],
    now: i64,
) -> Result<(), Error> {
    if now >= v.expires_at {
        return Err(Error::Conflict("Business-message approval expired"));
    }
    let p = &v.payload;
    let b: Binding = read(db, "whatsapp_bindings", owner, &p.binding_id)?;
    valid_binding(db, &b, scopes, Some(now))?;
    let c: Contact = read(db, "contacts", owner, &p.contact_id)
        .map_err(|_| Error::Conflict("The approved contact is no longer available"))?;
    if b.version != p.binding_version
        || b.account.stable() != p.prepared.account.stable()
        || b.template != p.prepared.template
        || c.version != p.contact_version
        || c.display_name != p.recipient_name
        || c.phone_e164.as_deref() != Some(p.recipient_e164.as_str())
    {
        return Err(Error::Conflict(
            "The approved contact, sender or template changed",
        ));
    }
    let permission = permission(db, owner, &b.account.connection_id, &c.id)?;
    valid_permission(&permission, &b, &c, &p.permission_version, now)
}
fn scoped_message(db: &Connection, w: &WorkerScope, id: &str) -> Result<Message, Error> {
    let v: Message = read(db, "business_messages", &w.principal_id, id)?;
    if !w.matches(&v.payload.prepared.account) {
        return Err(Error::NotFound);
    }
    Ok(v)
}
fn save_message(db: &Connection, owner: &str, v: &Message) -> Result<(), Error> {
    if db.execute(
        "UPDATE business_messages SET data=?3 WHERE owner_id=?1 AND id=?2",
        params![owner, v.id, encode(v)?],
    )? != 1
    {
        return Err(Error::NotFound);
    }
    Ok(())
}

impl Knowledge {
    pub fn business_bindings(
        &self,
        owner: &str,
        connection: Option<&str>,
    ) -> Result<Vec<Binding>, Error> {
        list(&self.db, "whatsapp_bindings", owner, connection)
    }
    pub fn business_binding(
        &self,
        owner: &str,
        id: &str,
        connection: Option<&str>,
    ) -> Result<Binding, Error> {
        let b: Binding = read(&self.db, "whatsapp_bindings", owner, id)?;
        if connection.is_some_and(|c| c != b.account.connection_id) {
            return Err(Error::NotFound);
        }
        Ok(b)
    }
    pub fn worker_business_bindings(&self, w: &WorkerScope) -> Result<Vec<Binding>, Error> {
        worker_list(&self.db, "whatsapp_bindings", "$.account", w)
    }
    pub fn worker_business_binding(&self, w: &WorkerScope, id: &str) -> Result<Binding, Error> {
        let b: Binding = read(&self.db, "whatsapp_bindings", &w.principal_id, id)?;
        if !w.matches(&b.account) {
            return Err(Error::NotFound);
        }
        Ok(b)
    }
    pub fn sync_business_binding(
        &mut self,
        w: &WorkerScope,
        input: BindingInput,
        now: i64,
    ) -> Result<(Binding, bool), Error> {
        let a = &input.account;
        if !a.valid() || !fresh(a.verified_at, now) {
            return Err(Error::BadRequest(
                "Account verification fields or timestamp are invalid",
            ));
        }
        if !w.matches(a) {
            return Err(Error::Forbidden);
        }
        input.template.validate()?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let previous_account: Option<String> = tx
            .query_row(
                "SELECT data FROM whatsapp_accounts WHERE owner_id=?1 AND connection_id=?2",
                params![w.principal_id, w.connection_id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(raw) = previous_account {
            let old: Account = decode(raw)?;
            if old.verified_at > a.verified_at
                || (old.stable() != a.stable() && old.verified_at == a.verified_at)
            {
                return Err(Error::Conflict(
                    "A newer account verification is already recorded",
                ));
            }
        }
        let old:Option<String>=tx.query_row("SELECT data FROM whatsapp_bindings WHERE owner_id=?1 AND connection_id=?2 AND template_id=?3 AND language=?4",params![w.principal_id,w.connection_id,input.template.id,input.template.language],|r|r.get(0)).optional()?;
        let previous = old.map(decode::<Binding>).transpose()?;
        let created = previous.is_none();
        let b = Binding {
            id: previous
                .map(|b| b.id)
                .unwrap_or_else(|| Uuid::new_v4().to_string()),
            version: digest(&json!({"account":a.stable(),"template":input.template})),
            account: input.account,
            template: input.template,
            updated_at: now,
        };
        tx.execute("INSERT INTO whatsapp_accounts(owner_id,connection_id,data) VALUES(?1,?2,?3) ON CONFLICT(owner_id,connection_id) DO UPDATE SET data=excluded.data",params![w.principal_id,w.connection_id,encode(&b.account)?])?;
        tx.execute("INSERT INTO whatsapp_bindings(id,owner_id,connection_id,template_id,language,data) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(id) DO UPDATE SET data=excluded.data",params![b.id,w.principal_id,w.connection_id,b.template.id,b.template.language,encode(&b)?])?;
        tx.commit()?;
        Ok((b, created))
    }
    pub fn business_permissions(
        &self,
        owner: &str,
        connection: Option<&str>,
    ) -> Result<Vec<Permission>, Error> {
        list(&self.db, "whatsapp_permissions", owner, connection)
    }
    pub fn worker_business_permissions(&self, w: &WorkerScope) -> Result<Vec<Permission>, Error> {
        let mut statement=self.db.prepare("SELECT p.data FROM whatsapp_permissions p JOIN whatsapp_accounts a ON a.owner_id=p.owner_id AND a.connection_id=p.connection_id WHERE p.owner_id=?1 AND p.connection_id=?2 AND json_extract(a.data,'$.waba_id')=?3 AND json_extract(a.data,'$.phone_number_id')=?4 AND json_extract(a.data,'$.principal_id')=?1 AND json_extract(p.data,'$.account_version')=json_extract(a.data,'$.version') ORDER BY p.rowid DESC LIMIT 100")?;
        let rows = statement
            .query_map(
                params![
                    w.principal_id,
                    w.connection_id,
                    w.waba_id,
                    w.phone_number_id
                ],
                |r| r.get::<_, String>(0),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter().map(decode).collect()
    }
    pub fn set_business_permission(
        &mut self,
        w: &WorkerScope,
        mut input: PermissionInput,
        now: i64,
    ) -> Result<(Permission, bool), Error> {
        if input.connection_id != w.connection_id {
            return Err(Error::Forbidden);
        }
        if !hash(&input.account_version)
            || !id(&input.contact_id)
            || !hash(&input.contact_version)
            || input.expected_version.as_ref().is_some_and(|s| !hash(s))
            || input.recorded_at < 0
            || input.recorded_at < now.saturating_sub(300)
            || input.recorded_at > now.saturating_add(30)
            || input.expires_at < 0
            || input.expires_at > now.saturating_add(366 * 86400)
            || !bounded(&input.evidence_reference, 200, false)
            || input.source_note.chars().count() > 400
            || input.source_note.chars().any(char::is_control)
            || input.allowed_categories.len() > 2
            || input
                .allowed_categories
                .iter()
                .collect::<BTreeSet<_>>()
                .len()
                != input.allowed_categories.len()
            || (input.status == PermissionStatus::Granted
                && (input.allowed_categories.is_empty() || input.expires_at <= now))
        {
            return Err(Error::BadRequest(
                "Invalid explicit permission fields or administrative event timestamp",
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let a = account(&tx, &w.principal_id, &w.connection_id)?;
        if !w.matches(&a) || a.version != input.account_version {
            return Err(Error::Conflict("The verified account revision changed"));
        }
        let c: Contact = read(&tx, "contacts", &w.principal_id, &input.contact_id)?;
        if c.version != input.contact_version {
            return Err(Error::Conflict("Contact revision changed"));
        }
        let recipient = c.phone_e164.ok_or(Error::BadRequest(
            "The selected contact has no phone number",
        ))?;
        let previous:Option<String>=tx.query_row("SELECT data FROM whatsapp_permissions WHERE owner_id=?1 AND connection_id=?2 AND contact_id=?3",params![w.principal_id,w.connection_id,input.contact_id],|r|r.get(0)).optional()?;
        let previous = previous.map(decode::<Permission>).transpose()?;
        if previous.as_ref().map(|p| p.version.as_str()) != input.expected_version.as_deref() {
            return Err(Error::Conflict(
                "Permission revision changed; explicit current revision is required",
            ));
        }
        input.allowed_categories.sort();
        let created = previous.is_none();
        let version = digest(&json!({"nonce":Uuid::new_v4().to_string()}));
        let p = Permission {
            id: previous
                .map(|p| p.id)
                .unwrap_or_else(|| Uuid::new_v4().to_string()),
            version,
            connection_id: input.connection_id,
            account_version: input.account_version,
            contact_id: input.contact_id,
            contact_version: input.contact_version,
            recipient_e164: recipient,
            status: input.status,
            allowed_categories: input.allowed_categories,
            recorded_at: input.recorded_at,
            expires_at: input.expires_at,
            source: input.source,
            evidence_reference: input.evidence_reference,
            source_note: input.source_note,
            updated_at: now,
        };
        tx.execute("INSERT INTO whatsapp_permissions(id,owner_id,connection_id,contact_id,data) VALUES(?1,?2,?3,?4,?5) ON CONFLICT(id) DO UPDATE SET data=excluded.data",params![p.id,w.principal_id,w.connection_id,p.contact_id,encode(&p)?])?;
        tx.commit()?;
        Ok((p, created))
    }
    pub fn business_messages(
        &self,
        owner: &str,
        connection: Option<&str>,
    ) -> Result<Vec<Message>, Error> {
        list(&self.db, "business_messages", owner, connection)
    }
    pub fn business_message(&self, owner: &str, id: &str) -> Result<Message, Error> {
        read(&self.db, "business_messages", owner, id)
    }
    pub fn worker_business_message(&self, w: &WorkerScope, id: &str) -> Result<Message, Error> {
        scoped_message(&self.db, w, id)
    }
    pub fn worker_business_messages(&self, w: &WorkerScope) -> Result<Vec<Message>, Error> {
        worker_list(
            &self.db,
            "business_messages",
            "$.payload.prepared.account",
            w,
        )
    }
    pub fn propose_business_message(
        &mut self,
        owner: &str,
        p: Proposal,
        scopes: &[WorkerScope],
        now: i64,
    ) -> Result<(Message, bool), Error> {
        if !id(&p.binding_id)
            || !id(&p.contact_id)
            || !hash(&p.binding_version)
            || !hash(&p.contact_version)
            || !hash(&p.permission_version)
            || p.idempotency_key.is_empty()
            || p.idempotency_key.len() > 128
            || !p
                .idempotency_key
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
            || p.parameters.len() > 10
            || p.parameters
                .iter()
                .any(|s| !bounded(s, 1024, false) || s.contains("{{") || s.contains("}}"))
        {
            return Err(Error::BadRequest(
                "Invalid business-message proposal fields",
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior: Option<String> = tx
            .query_row(
                "SELECT data FROM business_messages WHERE owner_id=?1 AND idempotency_key=?2",
                params![owner, p.idempotency_key],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(raw) = prior {
            let v: Message = decode(raw)?;
            let old = &v.payload;
            if old.binding_id != p.binding_id
                || old.binding_version != p.binding_version
                || old.contact_id != p.contact_id
                || old.contact_version != p.contact_version
                || old.permission_version != p.permission_version
                || old.prepared.parameters != p.parameters
            {
                return Err(Error::Conflict(
                    "Idempotency key already binds different business-message arguments",
                ));
            }
            return Ok((v, false));
        }
        let b: Binding = read(&tx, "whatsapp_bindings", owner, &p.binding_id)?;
        valid_binding(&tx, &b, scopes, None)?;
        if b.version != p.binding_version {
            return Err(Error::Conflict(
                "Verified template binding revision changed",
            ));
        }
        let c: Contact = read(&tx, "contacts", owner, &p.contact_id)?;
        if c.version != p.contact_version {
            return Err(Error::Conflict("Contact revision changed"));
        }
        let permission = permission(&tx, owner, &b.account.connection_id, &c.id)?;
        valid_permission(&permission, &b, &c, &p.permission_version, now)?;
        let rendered = b.template.render(&p.parameters)?;
        let payload = Payload {
            binding_id: b.id,
            binding_version: b.version,
            contact_id: c.id,
            contact_version: c.version,
            recipient_name: c.display_name,
            recipient_e164: permission.recipient_e164,
            permission_version: permission.version,
            prepared: Prepared {
                account: b.account,
                template: b.template,
                parameters: p.parameters,
                rendered_text: rendered,
            },
        };
        let v = Message {
            id: Uuid::new_v4().to_string(),
            kind: "contact.whatsapp_template".into(),
            argument_hash: digest(&json!({"kind":"contact.whatsapp_template","payload":payload})),
            payload,
            created_at: now,
            expires_at: now.saturating_add(300),
            status: "awaiting_approval".into(),
            evidence: None,
            recovery_note: None,
            decision: None,
            claimed_worker: None,
        };
        tx.execute("INSERT INTO business_messages(id,owner_id,connection_id,idempotency_key,data) VALUES(?1,?2,?3,?4,?5)",params![v.id,owner,v.payload.prepared.account.connection_id,p.idempotency_key,encode(&v)?])?;
        tx.commit()?;
        Ok((v, true))
    }
    pub fn decide_business_message(
        &mut self,
        owner: &str,
        id: &str,
        input: Decide,
        scopes: &[WorkerScope],
        now: i64,
    ) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v: Message = read(&tx, "business_messages", owner, id)?;
        if input.argument_hash != v.argument_hash {
            return Err(Error::Conflict("Approval arguments do not match"));
        }
        if let Some(previous) = v.decision {
            if previous == input.decision {
                return Ok(v);
            }
            // Withdrawal races the execution claim in the same IMMEDIATE transaction.
            // It grants no authority, so an expired/revoked approval may also be withdrawn.
            if previous == Decision::Approve
                && input.decision == Decision::Reject
                && v.status == "approved"
            {
                v.status = "rejected".into();
                v.decision = Some(Decision::Reject);
                save_message(&tx, owner, &v)?;
                tx.commit()?;
                return Ok(v);
            }
            return Err(Error::Conflict("A different decision was already recorded"));
        }
        if v.status != "awaiting_approval" || now >= v.expires_at {
            return Err(Error::Conflict(
                "Proposal is no longer awaiting fresh approval",
            ));
        }
        if input.decision == Decision::Approve {
            validate_current(&tx, owner, &v, scopes, now)?;
            v.status = "approved".into();
        } else {
            configured(scopes, &v.payload.prepared.account)?;
            v.status = "rejected".into();
        }
        v.decision = Some(input.decision);
        save_message(&tx, owner, &v)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn claim_business_message(
        &mut self,
        w: &WorkerScope,
        id: &str,
        input: Claim,
        scopes: &[WorkerScope],
        now: i64,
    ) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = scoped_message(&tx, w, id)?;
        if v.status != "approved" || v.argument_hash != input.argument_hash {
            return Err(Error::Conflict("Business message cannot be claimed"));
        }
        validate_current(&tx, &w.principal_id, &v, scopes, now)?;
        v.status = "executing".into();
        v.claimed_worker = Some(w.worker_id.clone());
        save_message(&tx, &w.principal_id, &v)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn authorize_business_message(
        &mut self,
        w: &WorkerScope,
        id: &str,
        input: Claim,
        scopes: &[WorkerScope],
        now: i64,
    ) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let v = scoped_message(&tx, w, id)?;
        if v.status != "executing"
            || v.claimed_worker.as_deref() != Some(w.worker_id.as_str())
            || v.argument_hash != input.argument_hash
        {
            return Err(Error::Conflict(
                "The exact executing claim is not authorized",
            ));
        }
        validate_current(&tx, &w.principal_id, &v, scopes, now)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn complete_business_message(
        &mut self,
        w: &WorkerScope,
        id: &str,
        input: Completion,
    ) -> Result<Message, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = scoped_message(&tx, w, id)?;
        if v.claimed_worker.as_deref() != Some(w.worker_id.as_str()) {
            return Err(Error::Conflict(
                "Only the claiming worker can record this receipt",
            ));
        }
        let a = &v.payload.prepared.account;
        let e = &input.evidence;
        if e.phone_number_id != a.phone_number_id
            || e.waba_id != a.waba_id
            || e.account_version != a.version
            || e.template_content_hash != v.payload.prepared.template.content_hash
            || e.recipient_e164 != v.payload.recipient_e164
            || e.provider_message_id.as_ref().is_some_and(|id| {
                id.is_empty() || id.len() > 512 || !id.bytes().all(|b| (33..=126).contains(&b))
            })
            || (input.status == ResultStatus::Accepted && e.provider_message_id.is_none())
            || (input.status == ResultStatus::Failed && e.provider_message_id.is_some())
        {
            return Err(Error::BadRequest("Provider evidence does not match the exact approved sender, template and recipient"));
        }
        let status = match input.status {
            ResultStatus::Accepted => "accepted",
            ResultStatus::Failed => "failed",
            ResultStatus::Unknown => "unknown",
        };
        if v.status == status && v.evidence.as_ref() == Some(e) {
            return Ok(v);
        }
        if v.status != "executing" {
            return Err(Error::Conflict(
                "Business-message result is immutable or was not claimed",
            ));
        }
        v.status = status.into();
        v.evidence = Some(input.evidence);
        save_message(&tx, &w.principal_id, &v)?;
        tx.commit()?;
        Ok(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn required<T>(result: Result<T, Error>) -> T {
        result.unwrap_or_else(|_| panic!("Expected storage success"))
    }
    fn worker() -> WorkerScope {
        WorkerScope {
            worker_id: "worker".into(),
            principal_id: "owner".into(),
            connection_id: "business".into(),
            waba_id: "111".into(),
            phone_number_id: "222".into(),
        }
    }
    fn input(at: i64) -> BindingInput {
        let mut t = Template {
            id: "555".into(),
            name: "pickup".into(),
            language: "en_US".into(),
            status: "APPROVED".into(),
            category: Category::UTILITY,
            parameter_format: "POSITIONAL".into(),
            body: "Hello {{1}}.".into(),
            content_hash: String::new(),
        };
        let mut raw = json!(t);
        raw.as_object_mut().unwrap().remove("content_hash");
        t.content_hash = digest(&raw);
        BindingInput {
            account: Account {
                connection_id: "business".into(),
                principal_id: "owner".into(),
                waba_id: "111".into(),
                phone_number_id: "222".into(),
                display_phone_number: "+16505551234".into(),
                verified_name: "Shop".into(),
                version: "a".repeat(64),
                verified_at: at,
            },
            template: t,
        }
    }
    fn grant(c: &Contact, expiry: i64) -> PermissionInput {
        PermissionInput {
            connection_id: "business".into(),
            account_version: "a".repeat(64),
            contact_id: c.id.clone(),
            contact_version: c.version.clone(),
            expected_version: None,
            status: PermissionStatus::Granted,
            allowed_categories: vec![Category::UTILITY],
            recorded_at: 1000,
            expires_at: expiry,
            source: PermissionSource::ExplicitOperatorRecord,
            evidence_reference: "test-record".into(),
            source_note: String::new(),
        }
    }
    fn proposal(b: &Binding, c: &Contact, p: &Permission, key: &str) -> Proposal {
        Proposal {
            binding_id: b.id.clone(),
            binding_version: b.version.clone(),
            contact_id: c.id.clone(),
            contact_version: c.version.clone(),
            permission_version: p.version.clone(),
            parameters: vec!["Asha".into()],
            idempotency_key: key.into(),
        }
    }
    fn completion(v: &Message) -> Completion {
        let a = &v.payload.prepared.account;
        Completion {
            status: ResultStatus::Accepted,
            evidence: Evidence {
                provider: Provider::WhatsappBusiness,
                phone_number_id: a.phone_number_id.clone(),
                waba_id: a.waba_id.clone(),
                account_version: a.version.clone(),
                template_content_hash: v.payload.prepared.template.content_hash.clone(),
                recipient_e164: v.payload.recipient_e164.clone(),
                provider_message_id: Some("wamid.synthetic".into()),
                delivery_status: DeliveryStatus::Unverified,
            },
        }
    }
    #[test]
    fn cached_draft_requires_fresh_verification_but_refresh_never_mutates_approved_snapshot() {
        let dir = tempfile::TempDir::new().unwrap();
        let mut s = Knowledge::open(&dir.path().join("hub.db")).unwrap();
        let w = worker();
        let scopes = vec![w.clone()];
        let (b, _) = required(s.sync_business_binding(&w, input(1000), 1000));
        let c = required(s.create_contact(
            "owner",
            crate::personal::NewContact {
                display_name: "Friend".into(),
                email: None,
                phone_e164: Some("+16505550000".into()),
            },
            1000,
        ));
        let (p, _) = required(s.set_business_permission(&w, grant(&c, 5000), 1000));
        let (v, _) = required(s.propose_business_message(
            "owner",
            proposal(&b, &c, &p, "cached"),
            &scopes,
            2000,
        ));
        assert_eq!(v.expires_at, 2300);
        assert!(matches!(
            s.decide_business_message(
                "owner",
                &v.id,
                Decide {
                    decision: Decision::Approve,
                    argument_hash: v.argument_hash.clone()
                },
                &scopes,
                2000
            ),
            Err(Error::Conflict(_))
        ));
        let (refreshed, _) = required(s.sync_business_binding(&w, input(2001), 2001));
        assert_eq!(refreshed.version, b.version);
        let approved = required(s.decide_business_message(
            "owner",
            &v.id,
            Decide {
                decision: Decision::Approve,
                argument_hash: v.argument_hash.clone(),
            },
            &scopes,
            2002,
        ));
        assert_eq!(approved.argument_hash, v.argument_hash);
        assert_eq!(approved.payload.prepared.account.verified_at, 1000);
        required(s.claim_business_message(
            &w,
            &v.id,
            Claim {
                argument_hash: v.argument_hash.clone(),
            },
            &scopes,
            2003,
        ));
        assert!(matches!(
            s.authorize_business_message(
                &w,
                &v.id,
                Claim {
                    argument_hash: v.argument_hash.clone()
                },
                &scopes,
                2300
            ),
            Err(Error::Conflict(_))
        ));
        assert_eq!(
            required(s.complete_business_message(&w, &v.id, completion(&v))).status,
            "accepted"
        );
    }
    #[test]
    fn consent_expiry_claimant_identity_and_connection_reassignment_are_rechecked_before_send() {
        let dir = tempfile::TempDir::new().unwrap();
        let mut s = Knowledge::open(&dir.path().join("hub.db")).unwrap();
        let w = worker();
        let scopes = vec![w.clone()];
        let (b, _) = required(s.sync_business_binding(&w, input(1000), 1000));
        let c = required(s.create_contact(
            "owner",
            crate::personal::NewContact {
                display_name: "Friend".into(),
                email: None,
                phone_e164: Some("+16505550000".into()),
            },
            1000,
        ));
        let (p, _) = required(s.set_business_permission(&w, grant(&c, 1050), 1000));
        let (v, _) = required(s.propose_business_message(
            "owner",
            proposal(&b, &c, &p, "expiring"),
            &scopes,
            1000,
        ));
        required(s.decide_business_message(
            "owner",
            &v.id,
            Decide {
                decision: Decision::Approve,
                argument_hash: v.argument_hash.clone(),
            },
            &scopes,
            1001,
        ));
        required(s.claim_business_message(
            &w,
            &v.id,
            Claim {
                argument_hash: v.argument_hash.clone(),
            },
            &scopes,
            1002,
        ));
        let mut other = w.clone();
        other.worker_id = "another-worker".into();
        assert!(matches!(
            s.authorize_business_message(
                &other,
                &v.id,
                Claim {
                    argument_hash: v.argument_hash.clone()
                },
                &scopes,
                1003
            ),
            Err(Error::Conflict(_))
        ));
        assert!(matches!(
            s.complete_business_message(&other, &v.id, completion(&v)),
            Err(Error::Conflict(_))
        ));
        assert!(matches!(
            s.authorize_business_message(
                &w,
                &v.id,
                Claim {
                    argument_hash: v.argument_hash.clone()
                },
                &[],
                1003
            ),
            Err(Error::Conflict(_))
        ));
        assert!(s
            .authorize_business_message(
                &w,
                &v.id,
                Claim {
                    argument_hash: v.argument_hash.clone()
                },
                &scopes,
                1049
            )
            .is_ok());
        assert!(matches!(
            s.authorize_business_message(
                &w,
                &v.id,
                Claim {
                    argument_hash: v.argument_hash.clone()
                },
                &scopes,
                1050
            ),
            Err(Error::Conflict(_))
        ));
        assert_eq!(
            required(s.complete_business_message(&w, &v.id, completion(&v))).status,
            "accepted"
        );
    }
    #[test]
    fn template_slot_rendering_matches_supported_positional_semantics() {
        let mut t = input(1000).template;
        t.body = "నమస్తే {{2}} / {{1}} / {{2}}".into();
        assert_eq!(
            required(t.render(&["Asha".into(), "42".into()])),
            "నమస్తే 42 / Asha / 42"
        );
        for body in [
            "{{0}}",
            "{{01}}",
            "{{11}}",
            "{{name}}",
            "{{1}} {{3}}",
            "{{1}",
            "{{1}} }}",
        ] {
            t.body = body.into();
            assert!(t.render(&["Asha".into()]).is_err(), "{body}");
        }
    }
}
