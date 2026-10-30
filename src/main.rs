mod input;
mod mode;
mod modes;
mod paths;
mod runner;
mod store;
mod words;

use std::sync::Arc;

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};

use crate::mode::{Mode, Strictness};
use crate::modes::literature::Literature;
use crate::modes::master::Master;
use crate::runner::Kernel;
use crate::store::Store;

/// Terminal typing trainer that records every keystroke.
#[derive(Parser)]
#[command(name = "teepee", version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
    /// Label stored on every drill of this session, e.g. the keyboard.
    /// Repeatable. Defaults to the comma-separated `TEEPEE_TAGS` env var.
    #[arg(long, global = true)]
    tag: Vec<String>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Random words from the english 10k list; the first wrong key fails the
    /// drill and the missed word is practised before a fresh drill (default).
    Master {
        /// Words per drill.
        #[arg(long, default_value_t = 50)]
        words: usize,
        /// Times a missed word is repeated.
        #[arg(long, default_value_t = 10)]
        reps: usize,
    },
    /// Type a book one sentence at a time.
    Lit {
        /// Name of a file in the books directory, without `.txt`.
        book: String,
        /// Sentences per drill.
        #[arg(long, default_value_t = 10)]
        sentences: usize,
        /// How mistakes are judged.
        #[arg(long, value_enum, default_value_t = Strictness::Expert)]
        strict: Strictness,
        /// Times a missed word is repeated.
        #[arg(long, default_value_t = 10)]
        reps: usize,
    },
    /// List available books.
    Books,
    /// Print the data directory.
    Data,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let cmd = cli.cmd.unwrap_or(Cmd::Master { words: 50, reps: 10 });
    let mut tags = cli.tag;
    if tags.is_empty() && let Ok(env) = std::env::var("TEEPEE_TAGS") {
        tags = env.split(',').map(str::trim).filter(|t| !t.is_empty()).map(String::from).collect();
    }
    tags.sort();
    tags.dedup();

    match cmd {
        Cmd::Data => {
            println!("{}", paths::data_dir().display());
            Ok(())
        }
        Cmd::Books => {
            let dir = paths::books_dir();
            let mut names: Vec<String> = std::fs::read_dir(&dir)
                .map(|rd| {
                    rd.filter_map(|e| e.ok())
                        .filter_map(|e| {
                            let p = e.path();
                            (p.extension()? == "txt").then(|| p.file_stem()?.to_str().map(String::from))?
                        })
                        .collect()
                })
                .unwrap_or_default();
            names.sort();
            if names.is_empty() {
                println!("no books in {}", dir.display());
            }
            for n in names {
                println!("{n}");
            }
            Ok(())
        }
        Cmd::Master { words, reps } => {
            let store = Store::open(&paths::db_path())?;
            run(store, tags, vec![Box::new(Master::new(words::english_10k(), words, reps))])
        }
        Cmd::Lit { book, sentences, strict, reps } => {
            let store = Store::open(&paths::db_path())?;
            let path = paths::books_dir().join(format!("{book}.txt"));
            let text = std::fs::read_to_string(&path)
                .with_context(|| format!("reading {} (see `teepee books`)", path.display()))?;
            let lines: Vec<String> =
                text.lines().map(str::trim).filter(|l| !l.is_empty()).map(String::from).collect();
            if lines.is_empty() {
                bail!("{} has no sentences", path.display());
            }
            let start = store.last_completed_sentence(&book)?.map_or(0, |i| i + 1);
            if start >= lines.len() {
                println!("{book}: all {} sentences done", lines.len());
                return Ok(());
            }
            let mode = Literature::new(book, Arc::new(lines), start, sentences, strict, reps);
            run(store, tags, vec![Box::new(mode)])
        }
    }
}

fn run(store: Store, tags: Vec<String>, first: Vec<Box<dyn Mode>>) -> Result<()> {
    let terminal = ratatui::init();
    let rx = input::spawn();
    let mut kernel = Kernel { store, rx, terminal, tags };
    let result = kernel.run(first);
    ratatui::restore();
    result
}
