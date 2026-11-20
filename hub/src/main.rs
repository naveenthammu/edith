use edith_central_hub::{app, Config};
use std::{net::SocketAddr, path::PathBuf};
mod instance_lock;

#[tokio::main]
async fn main() {
    if let Err(message) = run().await {
        eprintln!("EDITH hub: {message}");
        std::process::exit(1);
    }
}
async fn run() -> Result<(), String> {
    let path = std::env::var_os("EDITH_HUB_CONFIG")
        .map(PathBuf::from)
        .ok_or("Set EDITH_HUB_CONFIG to an absolute JSON configuration path")?;
    if !path.is_absolute() {
        return Err("EDITH_HUB_CONFIG must be an absolute path".into());
    }
    let mut config = Config::load(&path)?;
    let address: SocketAddr = config.bind.parse().map_err(|_| "Invalid loopback bind")?;
    // Neither a conflicting port nor another hub instance may trigger recovery
    // against a database owned by a live server.
    let listener = tokio::net::TcpListener::bind(address).await.map_err(|_| {
        "Cannot bind hub listener; check whether the configured port is already in use"
    })?;
    let (_database_ownership, database_path) =
        instance_lock::InstanceLock::acquire(&config.database_path)?;
    config.database_path = database_path;
    let router = app(config)?;
    println!("EDITH hub listening on http://{address}; private knowledge requires a configured principal credential.");
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .map_err(|_| "Hub listener stopped unexpectedly".into())
}
