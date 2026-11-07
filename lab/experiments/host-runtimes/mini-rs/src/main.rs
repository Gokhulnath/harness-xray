// The bare agent loop in Rust: same requests, parsing, tool and SQLite writes as mini.mjs.
// Single-threaded runtime (like Node's one JS thread) so CPU per turn compares like for like.
use futures_util::{future::join_all, StreamExt};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::error::Error;

type Res<T> = Result<T, Box<dyn Error>>;

struct Session { msgs: Vec<Value>, db: Connection, seq: i64 }

fn save(s: &mut Session, m: Value) -> Res<()> {
    s.db.prepare_cached("INSERT INTO entries (seq, json) VALUES (?1, ?2)")?.execute(params![s.seq, m.to_string()])?;
    s.seq += 1;
    s.msgs.push(m);
    Ok(())
}

fn find(buf: &[u8], pat: &[u8]) -> Option<usize> { buf.windows(pat.len()).position(|w| w == pat) }

async fn turn(client: &reqwest::Client, url: &str, tools: &Value, dir: &str, s: &mut Session, text: String) -> Res<()> {
    save(s, json!({"role": "user", "content": text}))?;
    loop {
        let body = json!({"model": "mock", "messages": s.msgs, "stream": true, "stream_options": {"include_usage": true}, "max_completion_tokens": 8192, "tools": tools});
        let resp = client.post(url).header("content-type", "application/json").header("authorization", "Bearer x").body(serde_json::to_vec(&body)?).send().await?;
        let mut stream = resp.bytes_stream();
        let (mut buf, mut out): (Vec<u8>, String) = (Vec::new(), String::new());
        let mut calls: Vec<(String, String, String)> = Vec::new();
        while let Some(chunk) = stream.next().await {
            buf.extend_from_slice(&chunk?);
            while let Some(i) = find(&buf, b"\n\n") {
                let line: Vec<u8> = buf.drain(..i + 2).collect();
                let line = &line[..i];
                if !line.starts_with(b"data: ") || line == b"data: [DONE]" { continue; }
                let j: Value = serde_json::from_slice(&line[6..])?;
                let Some(d) = j["choices"].get(0).and_then(|c| c.get("delta")) else { continue };
                if let Some(t) = d.get("content").and_then(Value::as_str) { out.push_str(t); }
                if let Some(tcs) = d.get("tool_calls").and_then(Value::as_array) {
                    for tc in tcs {
                        let k = tc["index"].as_u64().unwrap_or(0) as usize;
                        while calls.len() <= k { calls.push(Default::default()); }
                        if let Some(id) = tc.get("id").and_then(Value::as_str) { calls[k].0 = id.into(); }
                        if let Some(n) = tc["function"].get("name").and_then(Value::as_str) { calls[k].1 = n.into(); }
                        if let Some(a) = tc["function"].get("arguments").and_then(Value::as_str) { calls[k].2.push_str(a); }
                    }
                }
            }
        }
        let mut msg = json!({"role": "assistant", "content": if out.is_empty() { Value::Null } else { Value::String(out) }});
        if !calls.is_empty() {
            msg["tool_calls"] = calls.iter().map(|(id, n, a)| json!({"id": id, "type": "function", "function": {"name": n, "arguments": a}})).collect();
        }
        save(s, msg)?;
        if calls.is_empty() { return Ok(()); }
        for (id, _, a) in calls {
            let args: Value = serde_json::from_str(&a)?;
            let content = tokio::fs::read_to_string(format!("{}/{}", dir, args["path"].as_str().unwrap_or(""))).await?;
            save(s, json!({"role": "tool", "tool_call_id": id, "content": content}))?;
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Res<()> {
    let a: Vec<String> = std::env::args().collect();
    let (n_sessions, n_turns): (usize, usize) = (a.get(1).map_or(1, |v| v.parse().unwrap()), a.get(2).map_or(1, |v| v.parse().unwrap()));
    let url = format!("http://127.0.0.1:{}/v1/chat/completions", std::env::var("PORT").unwrap_or("18080".into()));
    let tools: Value = serde_json::from_str(&std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../tools.json"))?)?;
    let dir = format!("{}/mini-rs-{}", std::env::var("TMPDIR").unwrap_or("/tmp".into()).trim_end_matches('/'), std::process::id());
    std::fs::create_dir_all(&dir)?;
    std::fs::write(format!("{dir}/hello.txt"), "hello from the workspace\n")?;
    let client = reqwest::Client::new();
    let mut sessions = Vec::new();
    for i in 0..n_sessions {
        let db = Connection::open(format!("{dir}/s{i}.sqlite"))?;
        db.execute_batch("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; CREATE TABLE entries (seq INTEGER PRIMARY KEY, json TEXT NOT NULL)")?;
        sessions.push(Session { msgs: Vec::new(), db, seq: 0 });
    }
    let results = join_all(sessions.iter_mut().map(|s| {
        let (client, url, tools, dir) = (&client, &url, &tools, &dir);
        async move {
            for t in 0..n_turns { turn(client, url, tools, dir, s, format!("turn {t}: read hello.txt and summarise it")).await.map_err(|e| e.to_string())?; }
            Ok::<(), String>(())
        }
    })).await;
    let failed = results.iter().filter(|r| r.is_err()).count();
    if let Some(Err(e)) = results.iter().find(|r| r.is_err()) { eprintln!("first error: {e}"); }
    let rows: i64 = sessions.iter().map(|s| s.seq).sum();
    drop(sessions);
    std::fs::remove_dir_all(&dir)?;
    println!("{}", json!({"impl": "mini-rs", "sessions": n_sessions, "turns": n_sessions * n_turns, "rows": rows, "failed": failed}));
    Ok(())
}
