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
use serde_json::Value;

use crate::mode::Mode;
use crate::modes::literature::Literature;
use crate::modes::master::Master;
use crate::modes::sprint::Sprint;
use crate::runner::Kernel;
use crate::store::Store;

/// Terminal typing trainer that records every keystroke.
#[derive(Parser)]
#[command(name = "teepee", version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
    /// Set the tags stored on every drill from now on, e.g. the keyboard.
    /// Repeatable; `--tag none` clears. Tags stay until changed.
    #[arg(long, global = true)]
    tag: Vec<String>,
    /// Seconds between two keys after which the drill is void (outcome afk).
    #[arg(long, global = true, default_value_t = 3.0)]
    afk: f64,
    /// Plain Backspace does nothing (to build the Ctrl+W habit); the word
    /// deletes still work. Stays until `--no-backspace=false`.
    #[arg(long, global = true, num_args = 0..=1, require_equals = true, default_missing_value = "true")]
    no_backspace: Option<bool>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Random words; the first wrong key fails the drill and the missed word
    /// is practised (~25 keystrokes of it) before a fresh drill (default).
    /// A setting left unsaid is the last master drill's.
    Master {
        /// Word list: english 200, 1k, 5k or 10k [default: the last one, 10k at first]
        #[arg(long, value_parser = words::LIST_NAMES.to_vec())]
        list: Option<String>,
        /// Words per drill [default: the last one, 50 at first]
        #[arg(long, value_parser = positive)]
        words: Option<usize>,
        /// Keep capitals ("February", "I"); --keep-case=false lowercases
        /// everything again, like Monkeytype with punctuation off
        /// [default: the last one, false at first]
        #[arg(long, num_args = 0..=1, require_equals = true, default_missing_value = "true")]
        keep_case: Option<bool>,
    },
    /// Random words at normal difficulty: wrong keys are let through and
    /// nothing is practised. For going after a personal best; a fresh drill
    /// follows each one. A setting left unsaid is the last sprint drill's.
    Sprint {
        /// Word list: english 200, 1k, 5k or 10k [default: the last one, 200 at first]
        #[arg(long, value_parser = words::LIST_NAMES.to_vec())]
        list: Option<String>,
        /// Words per drill [default: the last one, 50 at first]
        #[arg(long, value_parser = positive)]
        words: Option<usize>,
    },
    /// Type a book one line at a time: a clause of prose, a verse of poetry.
    Lit {
        /// Book name (or a unique prefix of it). Omit it for the book last
        /// typed; with none yet, pick from a list.
        book: Option<String>,
    },
    /// List available books.
    Books,
    /// Show the current tags.
    Tags,
    /// Rewrite the tags of past drills, picked by time window or id range.
    Retag {
        /// New tags; `none` clears.
        #[arg(required = true)]
        tags: Vec<String>,
        /// Drills started in the last DURATION, e.g. 30m, 2h, 1d.
        #[arg(long)]
        last: Option<String>,
        /// Drill id range, e.g. 12-30 or 12- or -30 or 17.
        #[arg(long)]
        ids: Option<String>,
        /// Show what would change without writing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Print the data directory.
    Data,
    /// Stats: the exercise you last played as a wpm histogram; `stats sprint|master|lit` for the long
    /// view of one exercise, --full for all (runs the Python tool in analysis/.venv). Arguments pass
    /// through: --list X, --book X, --full, --tag X, --days N, --open.
    #[command(trailing_var_arg = true, allow_hyphen_values = true)]
    Stats { args: Vec<String> },
    /// Journal: the practice wall, a column per week and a cell per day shaded by the minutes typed
    /// (runs the Python tool in analysis/.venv).
    Journal,
    /// Prepare a book: `teepee book 1342 --name pride` (Gutenberg id or a text file).
    #[command(trailing_var_arg = true, allow_hyphen_values = true)]
    Book { args: Vec<String> },
    /// Delete the database (drills, keystrokes, tags). Books are kept.
    Reset {
        /// Skip the confirmation prompt.
        #[arg(long)]
        yes: bool,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let cmd = cli.cmd.unwrap_or(Cmd::Master { list: None, words: None, keep_case: None });
    let afk = std::time::Duration::from_secs_f64(cli.afk.max(0.5));
    match &cmd {
        Cmd::Reset { yes } => return reset(*yes), // before Store::open, which would recreate the file
        Cmd::Stats { args } => return python_tool("teepee-stats", args),
        Cmd::Journal => return python_tool("teepee-journal", &[]),
        Cmd::Book { args } => return python_tool("teepee-book", args),
        _ => {}
    }

    let store = Store::open(&paths::db_path())?;
    if let Err(e) = store.monthly_snapshot(&paths::snapshots_dir()) {
        eprintln!("monthly snapshot failed: {e:#}");
    }
    let tags = match cli.tag.is_empty() {
        true => store.current_tags()?,
        false => {
            let t = normalise_tags(cli.tag);
            store.set_current_tags(&t)?;
            t
        }
    };

    let no_backspace = match cli.no_backspace {
        Some(b) => {
            store.set_setting("no_backspace", if b { "1" } else { "0" })?;
            b
        }
        None => store.setting("no_backspace")?.as_deref() == Some("1"),
    };

    match cmd {
        Cmd::Data => {
            println!("{}", paths::data_dir().display());
            Ok(())
        }
        Cmd::Books => {
            list_books(&store)?;
            Ok(())
        }
        Cmd::Reset { .. } | Cmd::Stats { .. } | Cmd::Journal | Cmd::Book { .. } => unreachable!("handled above"),
        Cmd::Tags => {
            println!("{}", describe_tags(&tags));
            Ok(())
        }
        Cmd::Retag { tags: new, last, ids, dry_run } => {
            if last.is_none() && ids.is_none() {
                bail!("say which drills: --last 2h and/or --ids 12-30");
            }
            let new = normalise_tags(new);
            let from_ms = last.as_deref().map(parse_duration_ms).transpose()?.map(|d| now_ms() - d);
            let (lo, hi) = match ids.as_deref() {
                Some(r) => parse_id_range(r)?,
                None => (None, None),
            };
            let rows = store.drills_in(from_ms, None, lo, hi)?;
            if rows.is_empty() {
                println!("no drills matched");
                return Ok(());
            }
            let (first, last_row) = (&rows[0], &rows[rows.len() - 1]);
            println!(
                "{} drills, ids {}..{}, {} → {}",
                rows.len(),
                first.0,
                last_row.0,
                fmt_ms(first.2),
                fmt_ms(last_row.2)
            );
            let mut before: Vec<&str> = rows.iter().map(|r| r.3.as_str()).collect();
            before.sort();
            before.dedup();
            println!("current tags: {}    new tags: {}", before.join(" "), describe_tags(&new));
            if dry_run {
                println!("dry run, nothing written");
                return Ok(());
            }
            let ids: Vec<i64> = rows.iter().map(|r| r.0).collect();
            let n = store.set_drill_tags(&ids, &new)?;
            println!("retagged {n} drills");
            Ok(())
        }
        Cmd::Master { list, words, keep_case } => {
            let last = store.last_config("master")?;
            let (list, words) = word_settings(list, words, last.as_ref(), "10k");
            let keep_case = keep_case.or_else(|| last.as_ref()?.get("keep_case")?.as_bool()).unwrap_or(false);
            let list = words::list(&list, keep_case).expect("a list name from clap or the record");
            store.register_source(list.name, "wordlist", Some("en"), None, Some("monkeytype"))?;
            run(store, tags, afk, no_backspace, vec![Box::new(Master::new(list, words))])
        }
        Cmd::Sprint { list, words } => {
            let (list, words) = word_settings(list, words, store.last_config("sprint")?.as_ref(), "200");
            let list = words::list(&list, false).expect("a list name from clap or the record");
            store.register_source(list.name, "wordlist", Some("en"), None, Some("monkeytype"))?;
            run(store, tags, afk, no_backspace, vec![Box::new(Sprint::new(list, words))])
        }
        Cmd::Lit { book } => {
            let last = store.last_config("literature")?.and_then(|c| Some(c.get("book")?.as_str()?.to_string()));
            let book = choose_book(&store, book.or(last.filter(|b| book_names().contains(b))))?;   // none, or since deleted: the picker
            let (lines, mtime_ms) = read_book(&book)?;
            let meta = book_meta(&book);
            store.register_source(&book, "book", meta.lang.as_deref(), meta.title.as_deref(), meta.origin.as_deref())?;
            let start = store.last_completed_sentence(&book, mtime_ms)?.map_or(0, |i| i + 1);
            if start >= lines.len() {
                println!("{book}: all {} lines done", lines.len());
                return Ok(());
            }
            let mode = Literature::new(book, Arc::new(lines), start);
            run(store, tags, afk, no_backspace, vec![Box::new(mode)])
        }
    }
}

/// `--words`: a drill needs at least one.
fn positive(s: &str) -> Result<usize, String> {
    match s.parse() {
        Ok(n) if n > 0 => Ok(n),
        _ => Err("a whole number, 1 or more".to_string()),
    }
}

/// The word list (as typed: `1k`) and length of a word drill. What the command line left
/// unsaid comes from `last`, the config of the mode's last drill, and from the built-in
/// value where that has nothing usable: no drill yet, a key the config lacks, a list that no
/// longer exists. Each setting falls back on its own, and the record is the only memory:
/// nothing is stored on the side to drift from it.
fn word_settings(list: Option<String>, words: Option<usize>, last: Option<&Value>, first_list: &str) -> (String, usize) {
    let stored = |key: &str| last.and_then(|c| c.get(key));
    let list = list
        .or_else(|| stored("list")?.as_str().and_then(words::short_name).map(str::to_string))
        .unwrap_or_else(|| first_list.to_string());
    let words = words.or_else(|| stored("words")?.as_u64().filter(|n| *n > 0).map(|n| n as usize)).unwrap_or(50);
    (list, words)
}

/// Names of the prepared books, sorted.
fn book_names() -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(paths::books_dir())
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
    names
}

/// Print the books, numbered, with language, length and progress.
fn list_books(store: &Store) -> Result<Vec<String>> {
    let names = book_names();
    if names.is_empty() {
        println!("no books in {} (see `teepee book`)", paths::books_dir().display());
    }
    for (i, n) in names.iter().enumerate() {
        let (lines, mtime_ms) = read_book(n)?;
        let meta = book_meta(n);
        let done = store.last_completed_sentence(n, mtime_ms)?.map_or(0, |i| i + 1);
        println!(
            "{:>2}  {n:<12} {:<4} {:>5} lines, {done} done   {}",
            i + 1,
            meta.lang.as_deref().unwrap_or("?"),
            lines.len(),
            meta.title.as_deref().unwrap_or("")
        );
    }
    Ok(names)
}

/// A book name from the argument (exact name, a number from the list, or a
/// unique prefix), or, with no argument, from the list and a prompt.
fn choose_book(store: &Store, arg: Option<String>) -> Result<String> {
    let names = match arg {
        Some(a) => {
            let names = book_names();
            if let Some(name) = resolve_book(&names, &a) {
                return Ok(name);
            }
            println!("no book matches {a:?}; these are prepared:");
            list_books(store)?
        }
        None => list_books(store)?,
    };
    if names.is_empty() {
        bail!("no books");
    }
    loop {
        print!("book (number or name): ");
        std::io::Write::flush(&mut std::io::stdout())?;
        let mut line = String::new();
        if std::io::stdin().read_line(&mut line)? == 0 {
            bail!("no book chosen");
        }
        let line = line.trim();
        if line.is_empty() {
            bail!("no book chosen");
        }
        match resolve_book(&names, line) {
            Some(name) => return Ok(name),
            None => println!("no book matches {line:?}"),
        }
    }
}

/// Exact name, 1-based number, or unique case-insensitive prefix.
fn resolve_book(names: &[String], s: &str) -> Option<String> {
    if let Some(n) = names.iter().find(|n| *n == s) {
        return Some(n.clone());
    }
    if let Ok(i) = s.parse::<usize>()
        && (1..=names.len()).contains(&i)
    {
        return Some(names[i - 1].clone());
    }
    let s = s.to_lowercase();
    let hits: Vec<&String> = names.iter().filter(|n| n.to_lowercase().starts_with(&s)).collect();
    match hits.as_slice() {
        [one] => Some((*one).clone()),
        _ => None,
    }
}

/// The sentences of `<books>/<name>.txt` and the file's mtime in unix ms
/// (progress in a book only counts from when it was last prepared).
fn read_book(name: &str) -> Result<(Vec<String>, i64)> {
    let path = paths::books_dir().join(format!("{name}.txt"));
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("reading {} (see `teepee books`)", path.display()))?;
    let lines: Vec<String> = text.lines().map(str::trim).filter(|l| !l.is_empty()).map(String::from).collect();
    if lines.is_empty() {
        bail!("{} has no sentences", path.display());
    }
    let mtime_ms = std::fs::metadata(&path)?
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as i64);
    Ok((lines, mtime_ms))
}

/// What teepee-book wrote next to the text, if anything.
#[derive(Default, serde::Deserialize)]
struct BookMeta {
    lang: Option<String>,
    title: Option<String>,
    origin: Option<String>,
}

fn book_meta(name: &str) -> BookMeta {
    let path = paths::books_dir().join(format!("{name}.json"));
    std::fs::read_to_string(path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

/// Run one of the Python tools from the analysis venv next to this source
/// tree, passing arguments through and inheriting the terminal.
fn python_tool(name: &str, args: &[String]) -> Result<()> {
    let exe = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("analysis/.venv/bin").join(name);
    if !exe.exists() {
        bail!(
            "{} not found; set up the analysis venv:\n  cd {}/analysis && python3 -m venv .venv && .venv/bin/pip install .",
            exe.display(),
            env!("CARGO_MANIFEST_DIR")
        );
    }
    let status = std::process::Command::new(&exe)
        .args(args)
        .env("TEEPEE_DATA_DIR", paths::data_dir())
        .status()
        .with_context(|| format!("running {}", exe.display()))?;
    if !status.success() {
        bail!("{name} exited with {status}");
    }
    Ok(())
}

fn reset(yes: bool) -> Result<()> {
    let db = paths::db_path();
    if !db.exists() {
        println!("nothing to delete: {} does not exist", db.display());
        return Ok(());
    }
    let drills: i64 = rusqlite::Connection::open(&db)
        .and_then(|c| c.query_row("SELECT count(*) FROM drills", [], |r| r.get(0)))
        .unwrap_or(0);
    println!("delete {} ({drills} drills)? books are kept.", db.display());
    if !yes {
        print!("type yes to confirm: ");
        std::io::Write::flush(&mut std::io::stdout())?;
        let mut line = String::new();
        std::io::stdin().read_line(&mut line)?;
        if line.trim() != "yes" {
            println!("kept");
            return Ok(());
        }
    }
    for suffix in ["", "-journal", "-wal", "-shm"] {
        let p = db.with_file_name(format!("teepee.db{suffix}"));
        if p.exists() {
            std::fs::remove_file(&p).with_context(|| format!("removing {}", p.display()))?;
        }
    }
    println!("deleted. tags live in the database, so set them again: teepee --tag <keyboard>");
    Ok(())
}

/// Sorted, deduplicated; `none` means no tags.
fn normalise_tags(tags: Vec<String>) -> Vec<String> {
    let mut t: Vec<String> = tags
        .into_iter()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty() && s != "none")
        .collect();
    t.sort();
    t.dedup();
    t
}

fn describe_tags(tags: &[String]) -> String {
    if tags.is_empty() { "none".to_string() } else { tags.join(" ") }
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn fmt_ms(ms: i64) -> String {
    // Local-time rendering without a tz crate: show minutes ago.
    let ago = (now_ms() - ms).max(0) / 60_000;
    if ago < 120 { format!("{ago}m ago") } else { format!("{}h ago", ago / 60) }
}

/// "30m", "2h", "1d", or bare minutes.
fn parse_duration_ms(s: &str) -> Result<i64> {
    let s = s.trim();
    let (num, unit) = s.split_at(s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len()));
    let n: i64 = num.parse().with_context(|| format!("bad duration {s:?}"))?;
    let mult = match unit {
        "" | "m" | "min" => 60_000,
        "h" => 3_600_000,
        "d" => 86_400_000,
        _ => bail!("bad duration unit in {s:?} (use m, h or d)"),
    };
    Ok(n * mult)
}

/// "12-30", "12-", "-30" or "17".
fn parse_id_range(s: &str) -> Result<(Option<i64>, Option<i64>)> {
    let parse = |p: &str| -> Result<Option<i64>> {
        Ok(if p.is_empty() { None } else { Some(p.parse().with_context(|| format!("bad id {p:?}"))?) })
    };
    match s.split_once('-') {
        Some((a, b)) => Ok((parse(a.trim())?, parse(b.trim())?)),
        None => {
            let id = parse(s.trim())?;
            Ok((id, id))
        }
    }
}

fn run(
    store: Store,
    tags: Vec<String>,
    afk: std::time::Duration,
    no_backspace: bool,
    first: Vec<Box<dyn Mode>>,
) -> Result<()> {
    let terminal = ratatui::init();
    let rx = input::spawn();
    let mut kernel = Kernel { store, rx, terminal, tags, afk, no_backspace };
    let result = kernel.run(first);
    ratatui::restore();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn word_settings_fall_back_one_by_one() {
        let set = |l: Option<&str>, w, last: Option<Value>| word_settings(l.map(str::to_string), w, last.as_ref(), "200");
        assert_eq!(set(None, None, None), ("200".to_string(), 50));
        assert_eq!(set(None, None, Some(json!({"list": "english_1k", "words": 25}))), ("1k".to_string(), 25));
        assert_eq!(set(Some("5k"), None, Some(json!({"list": "english_1k", "words": 25}))), ("5k".to_string(), 25));
        assert_eq!(set(None, Some(10), Some(json!({"list": "english_1k", "words": 25}))), ("1k".to_string(), 10));
        // a list that no longer exists, a length that is no number, a config from another era
        assert_eq!(set(None, None, Some(json!({"list": "english_3k", "words": "many"}))), ("200".to_string(), 50));
        assert_eq!(set(None, None, Some(json!({"list": "english_10k", "words": 0}))), ("10k".to_string(), 50));
        assert_eq!(set(None, None, Some(json!({"book": "pride"}))), ("200".to_string(), 50));
    }
}
