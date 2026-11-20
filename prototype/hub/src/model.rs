use crate::domain::ModelStatus;
use reqwest::{Client, Response};
use serde_json::{json, Value};
use std::{sync::Arc, time::{Duration, Instant}};
use tokio::sync::{Mutex, Semaphore};

const OLLAMA: &str = "http://127.0.0.1:11434";
pub const MODEL: &str = "qwen3:4b-instruct";

#[derive(Clone)]
pub struct LocalModel {
    client: Client,
    generation: Arc<Semaphore>,
    status: Arc<Mutex<Option<(Instant, ModelStatus)>>>,
}
impl LocalModel {
    pub fn new() -> Result<Self,String> {
        let client=Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(2)).timeout(Duration::from_secs(90))
            .build().map_err(|_| "Could not initialize the loopback model client".to_string())?;
        Ok(Self {client,generation:Arc::new(Semaphore::new(1)),status:Arc::new(Mutex::new(None))})
    }
    pub async fn status(&self) -> ModelStatus {
        let mut cached=self.status.lock().await;
        if let Some((at,status))=&*cached { if at.elapsed()<Duration::from_secs(20) { return status.clone(); } }
        let available=async {
            let response=self.client.get(format!("{OLLAMA}/api/tags")).timeout(Duration::from_secs(2)).send().await.ok()?;
            if !response.status().is_success() { return None; }
            let value=bounded_json(response,128_000).await.ok()?;
            Some(value["models"].as_array()?.iter().any(|m|m["name"].as_str()==Some(MODEL)||m["model"].as_str()==Some(MODEL)))
        }.await.unwrap_or(false);
        let status=ModelStatus {available,model:MODEL.into(),detail:if available { "Local Ollama model available. Household answers use scoped lexical retrieval; generation stays on this PC." } else { "Local model unavailable. Install/start Ollama and qwen3:4b-instruct to enable generation. Scoped note search, demo controls, and curated play still work." }.into()};
        *cached=Some((Instant::now(),status.clone())); status
    }
    pub async fn generate(&self, system: &str, prompt: &str) -> Result<String,String> {
        if !self.status().await.available { return Err("Local model unavailable. No cloud fallback was used.".into()); }
        let _permit=self.generation.try_acquire().map_err(|_| "Another local answer is being generated. Try again after it finishes.".to_string())?;
        let response=self.client.post(format!("{OLLAMA}/api/chat"))
            .json(&json!({"model":MODEL,"stream":false,"keep_alive":"5m","messages":[{"role":"system","content":system},{"role":"user","content":prompt}],"options":{"num_predict":512,"num_ctx":4096,"temperature":0.2}}))
            .send().await.map_err(|_| "Local generation did not finish within 90 seconds, or Ollama disconnected. No cloud fallback was used.".to_string())?;
        if !response.status().is_success() { return Err("Ollama could not generate this answer. Check the local model; no cloud fallback was used.".into()); }
        let value=bounded_json(response,256_000).await?;
        let text=value["message"]["content"].as_str().unwrap_or("").trim();
        if text.is_empty() { return Err("The local model returned no answer. Try a shorter question.".into()); }
        Ok(text.chars().take(16_000).collect())
    }
}

async fn bounded_json(mut response: Response, maximum: usize) -> Result<Value,String> {
    if response.content_length().is_some_and(|size|size>maximum as u64) { return Err("Local model response exceeded the size limit".into()); }
    let mut bytes=Vec::new();
    while let Some(chunk)=response.chunk().await.map_err(|_| "Local model response was interrupted".to_string())? {
        if bytes.len()+chunk.len()>maximum { return Err("Local model response exceeded the size limit".into()); }
        bytes.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&bytes).map_err(|_| "Local model returned invalid JSON".to_string())
}

