use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    net::SocketAddr,
    path::{Path, PathBuf},
};

fn enabled() -> bool {
    true
}
fn bind() -> String {
    "127.0.0.1:8788".into()
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Adult,
    Elder,
    Child,
    Guest,
}
impl Role {
    pub fn is_adult(self) -> bool {
        matches!(self, Self::Adult | Self::Elder)
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Principal {
    pub id: String,
    pub token: String,
    pub role: Role,
    #[serde(default = "enabled")]
    pub enabled: bool,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhatsAppWorker {
    pub id: String,
    pub principal_id: String,
    pub connection_id: String,
    pub waba_id: String,
    pub phone_number_id: String,
    pub token: String,
    #[serde(default = "enabled")]
    pub enabled: bool,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub database_path: PathBuf,
    #[serde(default = "bind")]
    pub bind: String,
    pub principals: Vec<Principal>,
    #[serde(default)]
    pub home_entities: Vec<String>,
    #[serde(default)]
    pub commerce_merchants: Vec<crate::commerce::MerchantConfig>,
    #[serde(default)]
    pub whatsapp_workers: Vec<WhatsAppWorker>,
}
impl Config {
    pub fn load(path: &Path) -> Result<Self, String> {
        let metadata = std::fs::metadata(path).map_err(|_| "Cannot read EDITH_HUB_CONFIG file")?;
        if metadata.len() > 256_000 {
            return Err("Hub configuration exceeds the size limit".into());
        }
        let bytes = std::fs::read(path).map_err(|_| "Cannot read EDITH_HUB_CONFIG file")?;
        let config: Self = serde_json::from_slice(&bytes)
            .map_err(|_| "Hub configuration contains invalid fields or values")?;
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> Result<(), String> {
        let addr: SocketAddr = self
            .bind
            .parse()
            .map_err(|_| "Hub bind must be a loopback IP and port")?;
        if !addr.ip().is_loopback() || addr.port() == 0 {
            return Err("Hub bind must be a loopback IP and nonzero port".into());
        }
        if self.database_path.as_os_str().is_empty() || !self.database_path.is_absolute() {
            return Err("Database path must be an absolute path".into());
        }
        if self.principals.is_empty() || self.principals.len() > 64 {
            return Err("Configure between one and 64 principals".into());
        }
        let mut entities = HashSet::new();
        if self.home_entities.len() > 128
            || self
                .home_entities
                .iter()
                .any(|entity| !valid_entity(entity) || !entities.insert(entity))
        {
            return Err(
                "Home entities must be at most 128 unique exact light, switch or fan entity IDs"
                    .into(),
            );
        }
        let mut ids = HashSet::new();
        let mut tokens = HashSet::new();
        for p in &self.principals {
            if p.id.is_empty()
                || p.id.len() > 64
                || !p
                    .id
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
            {
                return Err("Principal identifiers must contain one to 64 ASCII letters, digits, hyphens or underscores".into());
            }
            if !(32..=256).contains(&p.token.len())
                || !p.token.bytes().all(|b| b.is_ascii_graphic())
            {
                return Err("Principal credentials must have 32 to 256 printable non-space ASCII characters".into());
            }
            if !ids.insert(&p.id) || !tokens.insert(&p.token) {
                return Err("Principal identifiers and credentials must each be unique".into());
            }
        }
        let mut merchants = HashSet::new();
        if self.commerce_merchants.len() > 128
            || self.commerce_merchants.iter().any(|m| {
                !m.valid()
                    || !self
                        .principals
                        .iter()
                        .any(|p| p.id == m.principal_id && p.role.is_adult())
                    || !merchants.insert((&m.principal_id, &m.id))
            })
        {
            return Err("Merchants require unique owner-scoped identifiers, an adult owner, exact shop domain and checkout hosts".into());
        }
        let mut connections = HashSet::new();
        if self.whatsapp_workers.len() > 64 {
            return Err("Configure at most 64 business workers".into());
        }
        for worker in &self.whatsapp_workers {
            if !crate::business_messages::identifier(&worker.id)
                || !crate::business_messages::identifier(&worker.connection_id)
                || !crate::business_messages::numeric(&worker.waba_id)
                || !crate::business_messages::numeric(&worker.phone_number_id)
                || !self.principals.iter().any(|p| {
                    p.id == worker.principal_id
                        && p.role.is_adult()
                        && (!worker.enabled || p.enabled)
                })
                || !(32..=256).contains(&worker.token.len())
                || !worker.token.bytes().all(|b| b.is_ascii_graphic())
                || !ids.insert(&worker.id)
                || !tokens.insert(&worker.token)
                || !connections.insert((&worker.principal_id, &worker.connection_id))
            {
                return Err("Business workers require distinct credentials and exact account bindings to enabled adult or elder principals".into());
            }
        }
        Ok(())
    }
}

pub(crate) fn valid_entity(entity: &str) -> bool {
    let Some((domain, name)) = entity.split_once('.') else {
        return false;
    };
    ["light", "switch", "fan"].contains(&domain)
        && !name.is_empty()
        && name.len() <= 100
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}
