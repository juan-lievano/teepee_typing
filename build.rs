//! Bakes the commit this binary is built from into `TEEPEE_COMMIT`, stored
//! on every drill: `abc1234`, or `abc1234+dirty` when the code differs from
//! that commit (anything `git status` reports under the paths below, new
//! files included). Empty when there is no git checkout.

use std::process::Command;

/// What the binary is made of.
const CODE: [&str; 5] = ["src", "assets", "build.rs", "Cargo.toml", "Cargo.lock"];

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn main() {
    // Rerun when the code changes (clean -> dirty) or a commit lands (dirty -> clean).
    for p in CODE.iter().chain(&[".git/HEAD", ".git/logs/HEAD", ".git/index"]) {
        println!("cargo:rerun-if-changed={p}");
    }
    let commit = git(&["rev-parse", "--short", "HEAD"]).unwrap_or_default();
    let mut status = vec!["status", "--porcelain", "--"];
    status.extend(CODE);
    let dirty = git(&status).is_some_and(|s| !s.is_empty());
    let dirty = if dirty && !commit.is_empty() { "+dirty" } else { "" };
    println!("cargo:rustc-env=TEEPEE_COMMIT={commit}{dirty}");
}
