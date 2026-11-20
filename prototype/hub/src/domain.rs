use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::{SystemTime, UNIX_EPOCH};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

pub fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }
pub fn timestamp(seconds: u64) -> String {
    OffsetDateTime::from_unix_timestamp(seconds as i64).unwrap_or(OffsetDateTime::UNIX_EPOCH)
        .format(&Rfc3339).unwrap_or_else(|_| "1970-01-01T00:00:00Z".into())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum Profile { Adult, Elder, Child, Guest }
impl Profile {
    pub fn is_adult(self) -> bool { matches!(self, Self::Adult | Self::Elder) }
    pub fn can_control(self, device: &str) -> bool { self.is_adult() || (self == Self::Child && device == "study-light") }
    pub fn can_read(self, scope: Scope) -> bool { scope == Scope::Household || self.is_adult() }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum Scope { Household, Adult }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Device { pub id: String, pub name: String, pub room: String, pub on: bool, pub provider: String }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DocumentInfo { pub id: String, pub title: String, pub scope: Scope, pub created_at: String }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Document { #[serde(flatten)] pub info: DocumentInfo, pub content: String }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Citation { pub document_id: String, pub title: String, pub excerpt: String, pub score: usize }
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum ActionKind { Purchase, Message }
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum ActionStatus { AwaitingApproval, ApprovedPreview, Rejected }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PendingAction {
    pub id: String, pub kind: ActionKind, pub title: String, pub details: Value,
    pub status: ActionStatus, pub created_at: String, pub expires_at: String, pub demo: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnedAction {
    #[serde(flatten)] pub action: PendingAction,
    pub session_id: String, pub profile: Profile, pub expires_unix: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Activity { pub id: String, pub timestamp: String, pub kind: String, pub summary: String }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnedActivity { #[serde(flatten)] pub activity: Activity, pub session_id: String, pub profile: Profile }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session { pub id: String, pub profile: Profile, pub revision: u64, pub expires_unix: u64 }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelStatus { pub available: bool, pub model: String, pub detail: String }
#[derive(Debug, Serialize)]
pub struct StateView {
    pub profile: Profile, pub devices: Vec<Device>, pub documents: Vec<DocumentInfo>,
    pub actions: Vec<PendingAction>, pub activity: Vec<Activity>, pub model: ModelStatus,
}

