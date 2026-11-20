use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use tempfile::TempDir;

const TOKEN: &str = "synthetic-process-test-credential-000000000001";
struct Server(Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn spawn(config: &Path) -> Server {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_edith-central-hub"));
    cmd.env("EDITH_HUB_CONFIG", config)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    Server(cmd.spawn().unwrap())
}
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}
fn config(dir: &TempDir, name: &str, port: u16) -> std::path::PathBuf {
    let path = dir.path().join(name);
    fs::write(&path,json!({"database_path":dir.path().join("hub.sqlite3"),"bind":format!("127.0.0.1:{port}"),"home_entities":["light.study"],"principals":[{"id":"owner","role":"adult","token":TOKEN}]}).to_string()).unwrap();
    path
}
fn call(port: u16, method: &str, path: &str, body: Value) -> Result<(u16, Value), String> {
    let mut stream = TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}").parse().unwrap(),
        Duration::from_millis(150),
    )
    .map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    stream
        .set_write_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let text = if body.is_null() {
        String::new()
    } else {
        body.to_string()
    };
    write!(stream,"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",text.len()).map_err(|e|e.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|e| e.to_string())?;
    let (headers, content) = response.split_once("\r\n\r\n").ok_or("No HTTP response")?;
    let code = headers
        .split_whitespace()
        .nth(1)
        .ok_or("No status")?
        .parse()
        .map_err(|_| "Bad status")?;
    Ok((
        code,
        serde_json::from_str(content).map_err(|_| "Bad JSON body")?,
    ))
}
fn ready(server: &mut Server, port: u16) {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        assert!(
            server.0.try_wait().unwrap().is_none(),
            "server exited before becoming ready"
        );
        if call(port, "GET", "/health", Value::Null).is_ok_and(|(status, _)| status == 200) {
            return;
        }
        assert!(Instant::now() < deadline, "server never became ready");
        thread::sleep(Duration::from_millis(20));
    }
}
fn executing(port: u16) -> String {
    let (status,a)=call(port,"POST","/v1/actions",json!({"kind":"home.power","payload":{"entity_id":"light.study","on":true},"idempotency_key":"process-test"})).unwrap();
    assert_eq!(status, 201);
    let id = a["id"].as_str().unwrap();
    assert_eq!(
        call(
            port,
            "POST",
            &format!("/v1/actions/{id}/decide"),
            json!({"decision":"approve","argument_hash":a["argument_hash"]})
        )
        .unwrap()
        .0,
        200
    );
    assert_eq!(
        call(
            port,
            "POST",
            &format!("/v1/actions/{id}/claim"),
            Value::Null
        )
        .unwrap()
        .1["status"],
        "executing"
    );
    id.to_owned()
}
fn wait_exit(server: &mut Server) {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(status) = server.0.try_wait().unwrap() {
            assert!(!status.success());
            return;
        }
        assert!(
            Instant::now() < deadline,
            "second process did not refuse startup"
        );
        thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn another_port_cannot_recover_a_live_hubs_database_and_crash_releases_ownership() {
    let dir = TempDir::new().unwrap();
    let first_port = free_port();
    let second_port = free_port();
    let first_config = config(&dir, "first.json", first_port);
    let second_config = config(&dir, "second.json", second_port);
    let mut first = spawn(&first_config);
    ready(&mut first, first_port);
    let id = executing(first_port);
    let mut second = spawn(&second_config);
    // Wait until competing process exits or starts answering; both distinguish
    // startup from a timing-dependent fixed sleep.
    let deadline = Instant::now() + Duration::from_secs(3);
    while second.0.try_wait().unwrap().is_none()
        && call(second_port, "GET", "/health", Value::Null).is_err()
    {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(20));
    }
    let state = call(first_port, "GET", "/v1/actions", Value::Null)
        .unwrap()
        .1;
    assert_eq!(
        state["actions"][0]["status"], "executing",
        "a competing process must not run recovery on a live journal"
    );
    wait_exit(&mut second);
    drop(second);
    drop(first);
    let mut restarted = spawn(&second_config);
    ready(&mut restarted, second_port);
    let state = call(second_port, "GET", "/v1/actions", Value::Null)
        .unwrap()
        .1;
    assert_eq!(state["actions"][0]["id"], id);
    assert_eq!(state["actions"][0]["status"], "unknown");
    assert_eq!(
        call(
            second_port,
            "POST",
            &format!("/v1/actions/{id}/claim"),
            Value::Null
        )
        .unwrap()
        .0,
        409
    );
}

#[test]
fn occupied_listener_does_not_modify_the_database_before_startup_succeeds() {
    let dir = TempDir::new().unwrap();
    let port = free_port();
    let path = config(&dir, "first.json", port);
    let mut first = spawn(&path);
    ready(&mut first, port);
    let id = executing(port);
    drop(first);
    let occupied = TcpListener::bind("127.0.0.1:0").unwrap();
    let busy_port = occupied.local_addr().unwrap().port();
    let busy_path = config(&dir, "busy.json", busy_port);
    let mut failed = spawn(&busy_path);
    wait_exit(&mut failed);
    let db = rusqlite::Connection::open(dir.path().join("hub.sqlite3")).unwrap();
    let status: String = db
        .query_row("SELECT status FROM actions WHERE id=?1", [id], |r| r.get(0))
        .unwrap();
    assert_eq!(
        status, "executing",
        "binding failure must not trigger journal recovery"
    );
}
