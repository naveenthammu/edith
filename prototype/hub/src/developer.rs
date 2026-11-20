use std::{collections::HashSet, fs, io::Read, path::{Component, Path, PathBuf}};

pub const MAX_SOURCE_BYTES: u64 = 100_000;

fn allowed_name(input: &str) -> bool {
    if input.is_empty() || input.len() > 240 || input.contains(['\\', ':', '\0']) {
        return false;
    }
    let parts: Vec<_> = input.split('/').collect();
    for part in &parts {
        let name = part.to_ascii_lowercase();
        if part.is_empty() || part.starts_with('.') || !part.chars().all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
            || ["node_modules", "target", "venv", "data", "recordings"].contains(&name.as_str())
            || ["credential", "secret", "password", "token", "private_key", "apikey", "api_key"].iter().any(|blocked| name.contains(blocked)) {
            return false;
        }
    }
    if ["main.py", "README.md", "LICENSE"].contains(&input) { return true; }
    let extension = Path::new(input).extension().and_then(|s| s.to_str()).unwrap_or("");
    if input.starts_with("docs/product-plan/") { return extension == "md"; }
    if input.starts_with("prototype/hub/src/") { return extension == "rs"; }
    if input.starts_with("prototype/hub/tests/") { return extension == "rs"; }
    if input.starts_with("prototype/web/src/") { return ["ts", "tsx", "css"].contains(&extension); }
    if input.starts_with("prototype/voice/") { return ["ps1", "md"].contains(&extension); }
    matches!(input,
        "prototype/hub/Cargo.toml" | "prototype/hub/Cargo.lock" | "prototype/README.md" |
        "prototype/web/package.json" | "prototype/web/package-lock.json" | "prototype/web/index.html" |
        "prototype/web/vite.config.ts" | "prototype/web/tsconfig.json" | "prototype/web/tsconfig.app.json" |
        "prototype/web/tsconfig.node.json" | "prototype/start.ps1")
}

pub fn resolve_source(root: &Path, input: &str) -> Result<PathBuf, String> {
    if !allowed_name(input) || Path::new(input).components().any(|p| !matches!(p, Component::Normal(_))) {
        return Err("Only allowlisted EDITH source paths are readable".into());
    }
    let mut checked = root.to_path_buf();
    for part in input.split('/') {
        checked.push(part);
        let metadata = fs::symlink_metadata(&checked).map_err(|_| "Source file not found".to_string())?;
        if metadata.file_type().is_symlink() { return Err("Linked source paths are not allowed".into()); }
    }
    let resolved = checked.canonicalize().map_err(|_| "Source file not found".to_string())?;
    let canonical_root = root.canonicalize().map_err(|_| "Repository root is unavailable".to_string())?;
    if !resolved.starts_with(&canonical_root) { return Err("Source path escapes the repository".into()); }
    let metadata = fs::metadata(&resolved).map_err(|_| "Source file not found".to_string())?;
    if !metadata.is_file() || metadata.len() > MAX_SOURCE_BYTES { return Err("Source must be a text file no larger than 100 KB".into()); }
    Ok(resolved)
}

pub fn read_source(root: &Path, input: &str) -> Result<String, String> {
    let path = resolve_source(root, input)?;
    let mut content = String::new();
    fs::File::open(path).map_err(|_| "Source file is unavailable".to_string())?
        .take(MAX_SOURCE_BYTES + 1).read_to_string(&mut content)
        .map_err(|_| "Source is not readable UTF-8 text".to_string())?;
    if content.len() as u64 > MAX_SOURCE_BYTES { return Err("Source exceeds 100 KB".into()); }
    Ok(content)
}

pub fn list_sources(root: &Path) -> Vec<(String, u64)> {
    let mut files = Vec::new();
    let mut queue = vec![root.to_path_buf()];
    let mut visited = HashSet::new();
    let mut scanned = 0;
    while let Some(dir) = queue.pop() {
        let Ok(canonical) = dir.canonicalize() else { continue };
        if !canonical.starts_with(root) || !visited.insert(canonical) { continue; }
        let Ok(entries) = fs::read_dir(dir) else { continue };
        let mut entries: Vec<_> = entries.filter_map(Result::ok).collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            scanned += 1;
            if scanned > 2_000 || files.len() >= 150 { break; }
            let path = entry.path();
            let Ok(relative) = path.strip_prefix(root) else { continue };
            let input = relative.to_string_lossy().replace('\\', "/");
            let Ok(kind) = entry.file_type() else { continue };
            if kind.is_symlink() { continue; }
            if kind.is_dir() {
                if ["prototype", "prototype/hub", "prototype/hub/src", "prototype/hub/tests", "prototype/web", "prototype/web/src", "prototype/voice", "docs", "docs/product-plan"].contains(&input.as_str())
                    || input.starts_with("prototype/web/src/") || input.starts_with("prototype/hub/src/") {
                    if !input.split('/').any(|part| part.starts_with('.') || ["node_modules", "target", "venv"].contains(&part)) { queue.push(path); }
                }
            } else if let Ok(resolved) = resolve_source(root, &input) {
                if let Ok(metadata) = fs::metadata(resolved) { files.push((input, metadata.len())); }
            }
        }
        if scanned > 2_000 || files.len() >= 150 { break; }
    }
    files.sort_by(|a,b| a.0.cmp(&b.0));
    files
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_resolution_allows_repo_code_and_rejects_private_and_escaping_paths() {
        let root = std::env::temp_dir().join(format!("edith-path-test-{}", std::process::id()));
        std::fs::create_dir_all(root.join("prototype/hub/src")).unwrap();
        std::fs::write(root.join("main.py"), "print('fixture')").unwrap();
        std::fs::write(root.join(".env"), "secret fixture").unwrap();
        std::fs::write(root.join("prototype/hub/src/credentials.rs"), "private fixture").unwrap();
        let root = root.canonicalize().unwrap();
        assert_eq!(resolve_source(&root, "main.py").unwrap(), root.join("main.py").canonicalize().unwrap());
        for path in ["../main.py", "main.py/../.env", ".env", "C:\\Windows\\win.ini", "/etc/passwd", "prototype/hub/src/credentials.rs", "prototype\\..\\..\\.env"] {
            assert!(resolve_source(&root, path).is_err(), "allowed {path}");
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
