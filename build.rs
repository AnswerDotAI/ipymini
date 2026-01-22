use std::collections::HashSet;
use std::env;
use std::path::Path;
use std::process::Command;

const PY_ENV_SCRIPT: &str = include_str!("scripts/python_env.py");

fn main() {
    println!("cargo:rerun-if-env-changed=PYO3_PYTHON");
    println!("cargo:rerun-if-env-changed=PYTHONHOME");
    println!("cargo:rerun-if-changed=scripts/python_env.py");

    let mut candidates = Vec::new();
    if let Ok(py) = env::var("PYO3_PYTHON") {
        if !py.is_empty() {
            candidates.push(py);
        }
    }
    candidates.push("python".to_string());
    candidates.push("python3".to_string());

    let mut libdirs = HashSet::new();
    for candidate in candidates {
        if let Some(libdir) = python_libdir(&candidate) {
            if Path::new(&libdir).exists() {
                libdirs.insert(libdir);
            }
        }
    }

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "macos" || target_os == "linux" {
        for libdir in &libdirs {
            println!("cargo:rustc-link-search=native={libdir}");
            println!("cargo:rustc-link-arg=-Wl,-rpath,{libdir}");
        }
    }
}

fn python_libdir(exe: &str) -> Option<String> {
    let output = Command::new(exe)
        .args(["-c", PY_ENV_SCRIPT, "--libdir"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}
