use std::path::PathBuf;

#[derive(Clone)]
pub struct AppState;

impl AppState {
    pub fn new(_data_dir: PathBuf, _repo_root: PathBuf) -> Result<Self, String> {
        Ok(Self)
    }
}

pub fn router(_state: AppState) -> axum::Router {
    axum::Router::new()
}

