use crate::domain::*;
use serde::{Deserialize, Serialize};
use std::{fs::{self, File, OpenOptions}, io::{Read, Write}, path::Path};
use uuid::Uuid;

pub const MAX_STATE_BYTES: u64 = 24 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Store {
    pub version: u32, pub devices: Vec<Device>, pub documents: Vec<Document>,
    pub actions: Vec<OwnedAction>, pub activity: Vec<OwnedActivity>, pub sessions: Vec<Session>,
}
impl Store {
    pub fn seed() -> Self {
        let notes = [
            ("demo-router", "Demo · Router restart", Scope::Household, "Synthetic household note: To restart the router, switch its power off, wait 30 seconds, then turn it on. Wait two minutes for it to reconnect. No passwords are stored in this note."),
            ("demo-recycling", "Demo · Recycling day", Scope::Household, "Synthetic household note: Recycling collection is on Thursday. Put clean paper and rinsed containers in the recycling bin on Wednesday evening."),
            ("demo-plants", "Demo · Plant watering", Scope::Household, "Synthetic household note: Water the household plants on Sunday morning. Check the soil first and avoid overwatering."),
            ("demo-budget", "Demo · Adult budget", Scope::Adult, "Synthetic adult-only note: The demonstration household grocery budget is INR 6,000 per month. This is made-up planning data, not a real account or balance."),
        ];
        Self {
            version: 1,
            devices: [("study-light", "Study light", "Study", false), ("living-light", "Living room light", "Living room", true), ("bedroom-fan", "Bedroom fan", "Bedroom", false)]
                .into_iter().map(|(id,name,room,on)| Device {id:id.into(),name:name.into(),room:room.into(),on,provider:"demo".into()}).collect(),
            documents: notes.into_iter().map(|(id,title,scope,content)| Document { info: DocumentInfo {id:id.into(),title:title.into(),scope,created_at:timestamp(now())},content:content.into() }).collect(),
            actions: vec![], activity: vec![], sessions: vec![],
        }
    }
    pub fn load(data_dir: &Path) -> Result<Self,String> {
        fs::create_dir_all(data_dir).map_err(|e| format!("Cannot create local data directory: {e}"))?;
        let path = data_dir.join("state.json");
        if !path.exists() { let seeded = Self::seed(); seeded.persist(data_dir)?; return Ok(seeded); }
        let file = File::open(&path).map_err(|e| format!("Cannot read local state: {e}"))?;
        if file.metadata().map_err(|e|e.to_string())?.len() > MAX_STATE_BYTES { return Err("Local state exceeds the 24 MB storage limit".into()); }
        let mut bytes = Vec::new();
        file.take(MAX_STATE_BYTES + 1).read_to_end(&mut bytes).map_err(|e|e.to_string())?;
        if bytes.len() as u64 > MAX_STATE_BYTES { return Err("Local state exceeds the storage limit".into()); }
        let state: Self = serde_json::from_slice(&bytes).map_err(|_| "Local state is invalid. It was left untouched; restore it from a backup or choose a new data directory.".to_string())?;
        if state.version != 1 || state.documents.len()>100 || state.actions.len()>100 || state.activity.len()>200 || state.sessions.len()>64 || state.devices.len()!=3
            || state.documents.iter().any(|d|d.content.chars().count()>50_000 || d.info.title.chars().count()>120)
            || state.actions.iter().any(|a|a.action.details.to_string().len()>32_000)
            || state.sessions.iter().any(|s|s.id.len()>80) {
            return Err("Local state violates prototype storage bounds; it was left untouched".into());
        }
        Ok(state)
    }
    pub fn persist(&self, data_dir: &Path) -> Result<(),String> {
        let bytes = serde_json::to_vec(self).map_err(|_| "Could not encode local state".to_string())?;
        if bytes.len() as u64 > MAX_STATE_BYTES { return Err("Local state exceeds the storage limit".into()); }
        let temporary = data_dir.join(format!("state-{}.tmp",Uuid::new_v4()));
        let operation = || -> std::io::Result<()> {
            let mut file = OpenOptions::new().write(true).create_new(true).open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            drop(file);
            fs::rename(&temporary, data_dir.join("state.json"))?;
            #[cfg(unix)] File::open(data_dir)?.sync_all()?;
            Ok(())
        };
        if operation().is_err() {
            let _ = fs::remove_file(&temporary);
            return Err("Could not save local state. No change was committed; check disk space and data-directory permissions.".into());
        }
        Ok(())
    }
    pub fn record(&mut self, session_id: &str, profile: Profile, kind: &str, summary: &str) {
        self.activity.push(OwnedActivity { activity: Activity {id:Uuid::new_v4().to_string(),timestamp:timestamp(now()),kind:kind.into(),summary:summary.into()},session_id:session_id.into(),profile });
        if self.activity.len()>200 { self.activity.drain(..self.activity.len()-200); }
    }
    pub fn view(&self, session_id: &str, profile: Profile, model: ModelStatus) -> StateView {
        StateView {
            profile,
            devices:self.devices.iter().filter(|d|profile.can_control(&d.id)).cloned().collect(),
            documents:self.documents.iter().filter(|d|profile.can_read(d.info.scope)).map(|d|d.info.clone()).collect(),
            actions:self.actions.iter().filter(|a|profile.is_adult() && a.session_id==session_id && a.profile==profile).map(|a|a.action.clone()).rev().collect(),
            activity:self.activity.iter().filter(|a|a.session_id==session_id && a.profile==profile).map(|a|a.activity.clone()).rev().collect(), model,
        }
    }
}

