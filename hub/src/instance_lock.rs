use std::{
    fs::{self, File},
    io::ErrorKind,
    path::{Path, PathBuf},
};

/// The handle, rather than the sidecar's existence, establishes ownership.
/// Keep the sidecar after shutdown: unlinking it could let another process lock
/// a different file while the original file is still held open.
pub struct InstanceLock {
    _handle: File,
}

impl InstanceLock {
    pub fn acquire(database: &Path) -> Result<(Self, PathBuf), String> {
        let parent = database
            .parent()
            .ok_or("Cannot resolve database directory")?;
        fs::create_dir_all(parent).map_err(|_| "Cannot prepare database directory")?;
        let normalized = match fs::symlink_metadata(database) {
            Ok(_) => database
                .canonicalize()
                .map_err(|_| "Cannot resolve database location")?,
            Err(e) if e.kind() == ErrorKind::NotFound => parent
                .canonicalize()
                .map_err(|_| "Cannot resolve database directory")?
                .join(
                    database
                        .file_name()
                        .ok_or("Cannot resolve database filename")?,
                ),
            Err(_) => return Err("Cannot inspect database location".into()),
        };
        let mut filename = normalized
            .file_name()
            .ok_or("Cannot resolve database filename")?
            .to_os_string();
        filename.push(".hub.lock");
        let handle = File::options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(normalized.with_file_name(filename))
            .map_err(|_| "Cannot open database ownership lock")?;
        handle.try_lock().map_err(|_| {
            "Cannot acquire exclusive database ownership; another hub may already be running"
        })?;
        Ok((Self { _handle: handle }, normalized))
    }
}
