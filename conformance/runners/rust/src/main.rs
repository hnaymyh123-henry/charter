//! Charter conformance — Rust skeleton runner.
//!
//! Deliberate skeleton. Implements the SPEC §8 transparency-log-chain
//! verification op as a single end-to-end demo to prove the vector format
//! is implementable in Rust using only serde + sha2 + walkdir. The other
//! ~43 vectors are intentionally TODO until a future Rust Charter SDK
//! ships.
//!
//! Run with:
//!     cd conformance/runners/rust
//!     cargo run --release
//!
//! Exit 0 iff every implemented vector passes; non-zero otherwise.

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const GENESIS_PREV_HASH: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";

#[derive(Debug, Deserialize)]
struct Vector {
    name: String,
    #[serde(rename = "spec_section")]
    _spec_section: String,
    input: Value,
    expected_output: Value,
    expected_error: Value,
}

fn vectors_dir() -> PathBuf {
    let mut p = std::env::current_dir().expect("cwd");
    // From conformance/runners/rust/ go up to conformance/, then into vectors/.
    p.push("..");
    p.push("..");
    p.push("vectors");
    p
}

/// Walk every JSON vector under `dir`, sorted by relative path.
fn discover(dir: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = walkdir::WalkDir::new(dir)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("json"))
        .filter(|e| {
            e.path()
                .file_name()
                .and_then(|s| s.to_str())
                .map(|s| s != "generation_metadata.json")
                .unwrap_or(true)
        })
        .map(|e| e.path().to_path_buf())
        .collect();
    out.sort();
    out
}

/// Canonical JSON serialization matching SPEC §1.3 / §8.2:
/// sorted keys at every level, no whitespace, UTF-8.
fn canonical_json(value: &Value) -> Vec<u8> {
    fn sort(v: &Value) -> Value {
        match v {
            Value::Array(arr) => Value::Array(arr.iter().map(sort).collect()),
            Value::Object(map) => {
                let sorted: BTreeMap<&String, &Value> = map.iter().collect();
                let mut out = serde_json::Map::new();
                for (k, v) in sorted {
                    out.insert(k.clone(), sort(v));
                }
                Value::Object(out)
            }
            other => other.clone(),
        }
    }
    let sorted = sort(value);
    serde_json::to_vec(&sorted).expect("serde_json::to_vec cannot fail on Value")
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// Implements `verify_log_chain`. Returns the response Value the vector
/// expects, OR Err with a human-readable reason on failure.
fn op_verify_log_chain(input: &Value) -> Result<Value, String> {
    let entries = input
        .get("entries")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "input.entries missing or not array".to_string())?;

    if entries.is_empty() {
        return Ok(serde_json::json!({
            "ok": true,
            "entries": 0,
            "head_hash": GENESIS_PREV_HASH,
        }));
    }

    let mut expected_prev = GENESIS_PREV_HASH.to_string();
    for entry in entries {
        let obj = entry
            .as_object()
            .ok_or_else(|| "entry is not an object".to_string())?;
        let seq = obj
            .get("seq")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| "entry.seq missing or not integer".to_string())?;
        let prev_hash = obj
            .get("prev_hash")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "entry.prev_hash missing or not string".to_string())?;

        if prev_hash != expected_prev {
            return Ok(serde_json::json!({
                "ok": false,
                "broken_at_seq": seq,
            }));
        }

        // Recompute entry_hash over canonical JSON of entry without entry_hash.
        let mut without_hash = obj.clone();
        without_hash.remove("entry_hash");
        let recomputed = format!(
            "sha256:{}",
            sha256_hex(&canonical_json(&Value::Object(without_hash)))
        );
        let entry_hash = obj
            .get("entry_hash")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "entry.entry_hash missing or not string".to_string())?;

        if recomputed != entry_hash {
            return Ok(serde_json::json!({
                "ok": false,
                "broken_at_seq": seq,
            }));
        }
        expected_prev = entry_hash.to_string();
    }

    let head_hash = entries
        .last()
        .and_then(|v| v.get("entry_hash"))
        .and_then(|v| v.as_str())
        .unwrap_or(GENESIS_PREV_HASH)
        .to_string();

    Ok(serde_json::json!({
        "ok": true,
        "entries": entries.len(),
        "head_hash": head_hash,
    }))
}

/// Dispatch a vector to the right handler. Returns (passed, todo, reason).
fn run_one(vector: &Vector) -> (bool, bool, Option<String>) {
    let op = vector
        .input
        .get("operation")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let actual = match op {
        "verify_log_chain" => op_verify_log_chain(&vector.input),
        // TODO(future Rust SDK): implement canonical_bytes / sign / verify /
        // aggregate / verify_chain_strict / lifecycle_status / JWKS / pin /
        // redact_clause / verify_disclosure operations.
        _ => return (true, true, None),
    };

    let actual = match actual {
        Ok(v) => v,
        Err(e) => return (false, false, Some(e)),
    };

    if actual == vector.expected_output {
        (true, false, None)
    } else {
        (
            false,
            false,
            Some(format!(
                "output mismatch\n  expected: {}\n  actual:   {}",
                serde_json::to_string(&vector.expected_output).unwrap(),
                serde_json::to_string(&actual).unwrap()
            )),
        )
    }
}

fn main() -> ExitCode {
    let vdir = vectors_dir();
    let paths = discover(&vdir);
    if paths.is_empty() {
        eprintln!("No vectors found under {}", vdir.display());
        return ExitCode::from(2);
    }

    let mut implemented = 0usize;
    let mut passed = 0usize;
    let mut failed = 0usize;
    let mut todo = 0usize;

    for path in &paths {
        let bytes = match std::fs::read(path) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("[FAIL] {}: read error: {}", path.display(), e);
                failed += 1;
                implemented += 1;
                continue;
            }
        };
        let vector: Vector = match serde_json::from_slice(&bytes) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("[FAIL] {}: parse error: {}", path.display(), e);
                failed += 1;
                implemented += 1;
                continue;
            }
        };
        let rel = path
            .strip_prefix(&vdir)
            .unwrap_or(path)
            .display()
            .to_string();
        let (ok, is_todo, reason) = run_one(&vector);
        if is_todo {
            todo += 1;
        } else if ok {
            implemented += 1;
            passed += 1;
            println!("[PASS] {}  ({})", rel, vector.name);
        } else {
            implemented += 1;
            failed += 1;
            println!("[FAIL] {}", rel);
            if let Some(r) = reason {
                for line in r.lines() {
                    println!("       {}", line);
                }
            }
        }
    }

    println!();
    println!(
        "Rust skeleton runner: {}/{} implemented vectors passed; {} TODO (await future Rust SDK)",
        passed, implemented, todo
    );
    if failed == 0 && implemented > 0 {
        println!(
            "Framework proof: vector schema parses + canonical JSON hashing matches the Python runner."
        );
    }
    if failed == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
