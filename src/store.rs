//! SQLite storage. Three tables, one view. Keystrokes are the primary record;
//! `drill_summary` derives WPM and accuracy from them.

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;

use crate::mode::{Outcome, Target};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS drills (
  id            INTEGER PRIMARY KEY,
  mode          TEXT    NOT NULL,
  parent_drill  INTEGER REFERENCES drills(id),
  started_at_ms INTEGER NOT NULL,          -- wall clock, unix milliseconds
  ended_at_ns   INTEGER,                   -- monotonic ns since drill start
  outcome       TEXT,                      -- completed | failed | aborted
  failed_word   TEXT,                      -- the word that ended a failed drill
  config        TEXT    NOT NULL,          -- JSON, mode-specific
  tags          TEXT    NOT NULL DEFAULT '[]'  -- JSON array, e.g. ["voyager"]
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS targets (
  drill_id        INTEGER NOT NULL REFERENCES drills(id),
  idx             INTEGER NOT NULL,
  text            TEXT    NOT NULL,
  kind            TEXT    NOT NULL,        -- word | sentence
  source_ref      TEXT,                    -- e.g. english_10k#412, pride#1037
  shown_at_ns     INTEGER NOT NULL,        -- when it appeared on screen
  completed_at_ns INTEGER,
  PRIMARY KEY (drill_id, idx)
);

-- One row per word list or book, keyed by the part of targets.source_ref
-- before '#'. Word lists are seeded here; a book is registered by `teepee lit`
-- from its <name>.json sidecar written by teepee-book.
CREATE TABLE IF NOT EXISTS sources (
  name   TEXT PRIMARY KEY,                -- english_10k, pride, quijote
  kind   TEXT NOT NULL,                   -- wordlist | book
  lang   TEXT,                            -- ISO 639-1 (en, es); NULL when unknown
  title  TEXT,
  origin TEXT                             -- monkeytype, gutenberg:2000, a file name
);

CREATE TABLE IF NOT EXISTS keystrokes (
  drill_id   INTEGER NOT NULL REFERENCES drills(id),
  seq        INTEGER NOT NULL,
  t_ns       INTEGER NOT NULL,             -- monotonic ns since drill start
  target_idx INTEGER NOT NULL,
  pos        INTEGER NOT NULL,             -- index into the target's expected sequence
  expected   TEXT,                         -- NULL when typing past the end
  typed      TEXT,                         -- the key; NULL for backspace; a name like ctrl+x for modified/other
  kind       TEXT    NOT NULL,             -- char | backspace | ignored (settle) | modified (ctrl/alt+key) | other (enter, tab, ...)
  correct    INTEGER,                      -- 1 if the kernel accepted the key as progress; the key that ends a failed drill is 0; NULL for backspace
  PRIMARY KEY (drill_id, seq)
);
CREATE INDEX IF NOT EXISTS keystrokes_by_target ON keystrokes(drill_id, target_idx);
"#;

/// Derived per-drill numbers. Recreated on every open so the definition in
/// the binary is the one in the file. WPM and accuracy are NULL for drills
/// shorter than MIN_CHARS characters: two keys in 4 ms is not a speed.
///
/// Monkeytype's split: accuracy counts every character keystroke, WPM only
/// the characters standing at the end. A keystroke stands until a later key
/// lands at or before its position (a backspace, or the character typed in
/// its place), so a word typed, deleted and typed again counts once. The
/// same rule is in teepee-stats.
const SUMMARY_VIEW: &str = r#"
DROP VIEW IF EXISTS drill_summary;
CREATE VIEW drill_summary AS
WITH k AS (
  SELECT drill_id, kind, correct, t_ns, pos,
         MIN(pos) OVER (PARTITION BY drill_id, target_idx ORDER BY seq
                        ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS later_pos
  FROM keystrokes WHERE kind IN ('char', 'backspace')
),
s AS (
  SELECT drill_id,
         COUNT(*) FILTER (WHERE kind = 'char')                                     AS chars_typed,
         COALESCE(SUM(correct) FILTER (WHERE kind = 'char'), 0)                    AS chars_correct,
         COALESCE(SUM(correct) FILTER (WHERE kind = 'char'
                                         AND (later_pos IS NULL OR later_pos > pos)), 0) AS chars_final,
         MAX(t_ns) FILTER (WHERE kind = 'char') - MIN(t_ns) FILTER (WHERE kind = 'char') AS span_ns
  FROM k GROUP BY drill_id
)
SELECT d.id, d.mode, d.parent_drill, d.started_at_ms, d.outcome,
       COALESCE(s.chars_typed, 0)   AS chars_typed,
       COALESCE(s.chars_correct, 0) AS chars_correct,
       COALESCE(s.chars_final, 0)   AS chars_final,
       s.span_ns / 1e9              AS typing_secs,
       CASE WHEN s.chars_typed >= MIN_CHARS AND s.span_ns > 0 THEN
         s.chars_final / 5.0 / (s.span_ns / 60e9) END                      AS wpm,
       CASE WHEN s.chars_typed >= MIN_CHARS THEN
         100.0 * s.chars_correct / s.chars_typed END                       AS accuracy
FROM drills d LEFT JOIN s ON s.drill_id = d.id;
"#;

/// A drill with fewer character keystrokes than this gets no WPM or
/// accuracy (its keystrokes and outcome still count). teepee-stats applies
/// the same number.
pub const MIN_CHARS: usize = 10;

/// Columns added after the first release; each is applied once.
fn migrate(conn: &Connection) -> Result<()> {
    let has_failed_word = conn
        .prepare("SELECT 1 FROM pragma_table_info('drills') WHERE name = 'failed_word'")?
        .exists([])?;
    if !has_failed_word {
        conn.execute_batch("ALTER TABLE drills ADD COLUMN failed_word TEXT;")?;
    }
    let has_tags = conn
        .prepare("SELECT 1 FROM pragma_table_info('drills') WHERE name = 'tags'")?
        .exists([])?;
    if !has_tags {
        conn.execute_batch("ALTER TABLE drills ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';")?;
    }
    for list in crate::words::LIST_NAMES {
        let name = crate::words::full_name(list);
        conn.execute(
            "INSERT OR IGNORE INTO sources (name, kind, lang, title, origin) VALUES (?1, 'wordlist', 'en', ?2, 'monkeytype')",
            params![name, format!("Monkeytype {}", name.replace('_', " "))],
        )?;
    }
    Ok(())
}

pub struct KeyRow {
    pub seq: i64,
    pub t_ns: i64,
    pub target_idx: i64,
    pub pos: i64,
    pub expected: Option<char>,
    pub typed: Option<char>,
    /// For non-character keys: the key's name, stored in the `typed` column.
    pub typed_name: Option<String>,
    pub kind: &'static str,
    pub correct: Option<bool>,
}

pub struct Store {
    conn: Connection,
}

impl Store {
    pub fn open(path: &Path) -> Result<Store> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        }
        let conn = Connection::open(path).with_context(|| format!("opening {}", path.display()))?;
        conn.execute_batch(
            "PRAGMA journal_mode = DELETE;
             PRAGMA synchronous = FULL;
             PRAGMA foreign_keys = ON;",
        )?;
        conn.execute_batch(SCHEMA)?;
        conn.execute_batch(&SUMMARY_VIEW.replace("MIN_CHARS", &MIN_CHARS.to_string()))?;
        migrate(&conn)?;
        Ok(Store { conn })
    }

    pub fn begin_drill(
        &self,
        mode: &str,
        parent: Option<i64>,
        config: &Value,
        tags: &[String],
    ) -> Result<i64> {
        let now_ms = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis() as i64;
        self.conn.execute(
            "INSERT INTO drills (mode, parent_drill, started_at_ms, config, tags)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![mode, parent, now_ms, config.to_string(), Value::from(tags).to_string()],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn end_drill(&self, drill: i64, outcome: &Outcome, ended_at_ns: i64) -> Result<()> {
        let failed_word = match outcome {
            Outcome::Failed { word, .. } => Some(word.as_str()),
            _ => None,
        };
        self.conn.execute(
            "UPDATE drills SET outcome = ?2, ended_at_ns = ?3, failed_word = ?4 WHERE id = ?1",
            params![drill, outcome.as_str(), ended_at_ns, failed_word],
        )?;
        Ok(())
    }

    pub fn add_target(&self, drill: i64, idx: usize, t: &Target, shown_at_ns: i64) -> Result<()> {
        self.conn.execute(
            "INSERT INTO targets (drill_id, idx, text, kind, source_ref, shown_at_ns)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![drill, idx as i64, t.text, t.kind.as_str(), t.source_ref, shown_at_ns],
        )?;
        Ok(())
    }

    pub fn complete_target(&self, drill: i64, idx: usize, completed_at_ns: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE targets SET completed_at_ns = ?3 WHERE drill_id = ?1 AND idx = ?2",
            params![drill, idx as i64, completed_at_ns],
        )?;
        Ok(())
    }

    /// A backspace went back into a finished word target: it will complete
    /// again, and its completion time is the later one.
    pub fn uncomplete_target(&self, drill: i64, idx: usize) -> Result<()> {
        self.conn.execute(
            "UPDATE targets SET completed_at_ns = NULL WHERE drill_id = ?1 AND idx = ?2",
            params![drill, idx as i64],
        )?;
        Ok(())
    }

    pub fn add_key(&self, drill: i64, k: &KeyRow) -> Result<()> {
        self.conn.execute(
            "INSERT INTO keystrokes (drill_id, seq, t_ns, target_idx, pos, expected, typed, kind, correct)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                drill,
                k.seq,
                k.t_ns,
                k.target_idx,
                k.pos,
                k.expected.map(String::from),
                k.typed.map(String::from).or_else(|| k.typed_name.clone()),
                k.kind,
                k.correct.map(|b| b as i64),
            ],
        )?;
        Ok(())
    }

    pub fn setting(&self, key: &str) -> Result<Option<String>> {
        Ok(self
            .conn
            .query_row("SELECT value FROM settings WHERE key = ?1", params![key], |r| r.get(0))
            .optional()?)
    }

    pub fn set_setting(&self, key: &str, value: &str) -> Result<()> {
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    /// Tags set by the last `--tag`; they stay until changed.
    pub fn current_tags(&self) -> Result<Vec<String>> {
        Ok(self.setting("tags")?.map(|j| serde_json::from_str(&j)).transpose()?.unwrap_or_default())
    }

    pub fn set_current_tags(&self, tags: &[String]) -> Result<()> {
        self.set_setting("tags", &Value::from(tags).to_string())
    }

    /// Drills started in `[from_ms, to_ms]` and/or with ids in `[id_lo, id_hi]`
    /// (either bound may be None), as (id, mode, started_at_ms, tags).
    pub fn drills_in(
        &self,
        from_ms: Option<i64>,
        to_ms: Option<i64>,
        id_lo: Option<i64>,
        id_hi: Option<i64>,
    ) -> Result<Vec<(i64, String, i64, String)>> {
        let mut st = self.conn.prepare(
            "SELECT id, mode, started_at_ms, tags FROM drills
             WHERE (?1 IS NULL OR started_at_ms >= ?1) AND (?2 IS NULL OR started_at_ms <= ?2)
               AND (?3 IS NULL OR id >= ?3) AND (?4 IS NULL OR id <= ?4)
             ORDER BY id",
        )?;
        let rows = st
            .query_map(params![from_ms, to_ms, id_lo, id_hi], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn set_drill_tags(&self, ids: &[i64], tags: &[String]) -> Result<usize> {
        let json = Value::from(tags).to_string();
        let mut n = 0;
        for id in ids {
            n += self.conn.execute("UPDATE drills SET tags = ?2 WHERE id = ?1", params![id, json])?;
        }
        Ok(n)
    }

    /// Month-end snapshots, kept forever in `<data>/snapshots/teepee-YYYY-MM.db`.
    /// When the newest drill belongs to an earlier month than today and that
    /// month has no snapshot yet, copy the database as it stands (nothing from
    /// this month has been written yet), so the copy is how that month ended.
    pub fn monthly_snapshot(&self, snapshots: &Path) -> Result<Option<std::path::PathBuf>> {
        let last: Option<String> = self
            .conn
            .query_row(
                "SELECT strftime('%Y-%m', max(started_at_ms) / 1000, 'unixepoch', 'localtime') FROM drills",
                [],
                |r| r.get(0),
            )
            .optional()?
            .flatten();
        let now: String = self.conn.query_row("SELECT strftime('%Y-%m', 'now', 'localtime')", [], |r| r.get(0))?;
        let Some(last) = last else { return Ok(None) };
        if last >= now {
            return Ok(None);
        }
        let dest = snapshots.join(format!("teepee-{last}.db"));
        if dest.exists() {
            return Ok(None);
        }
        std::fs::create_dir_all(snapshots)?;
        let tmp = snapshots.join(format!("teepee-{last}.db.part"));
        let mut out = Connection::open(&tmp)?;
        {
            let backup = rusqlite::backup::Backup::new(&self.conn, &mut out)?;
            backup.run_to_completion(256, std::time::Duration::from_millis(5), None)?;
        }
        drop(out);
        std::fs::rename(&tmp, &dest)?;
        Ok(Some(dest))
    }

    /// Record (or update) what a source ref prefix stands for. NULL fields
    /// never overwrite known values.
    pub fn register_source(
        &self,
        name: &str,
        kind: &str,
        lang: Option<&str>,
        title: Option<&str>,
        origin: Option<&str>,
    ) -> Result<()> {
        self.conn.execute(
            "INSERT INTO sources (name, kind, lang, title, origin) VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT(name) DO UPDATE SET kind = excluded.kind,
               lang = coalesce(excluded.lang, lang), title = coalesce(excluded.title, title),
               origin = coalesce(excluded.origin, origin)",
            params![name, kind, lang, title, origin],
        )?;
        Ok(())
    }

    /// Highest sentence number of `book` typed to completion in a drill
    /// started at or after `since_ms` (the book file's mtime: re-preparing a
    /// book renumbers its sentences, so older progress no longer applies).
    pub fn last_completed_sentence(&self, book: &str, since_ms: i64) -> Result<Option<usize>> {
        let prefix = format!("{book}#");
        let v: Option<i64> = self
            .conn
            .query_row(
                "SELECT MAX(CAST(substr(t.source_ref, ?2) AS INTEGER))
                 FROM targets t JOIN drills d ON d.id = t.drill_id
                 WHERE t.kind = 'sentence' AND t.completed_at_ns IS NOT NULL
                   AND substr(t.source_ref, 1, ?3) = ?1 AND d.started_at_ms >= ?4",
                params![prefix, prefix.len() as i64 + 1, prefix.len() as i64, since_ms],
                |r| r.get(0),
            )
            .optional()?
            .flatten();
        Ok(v.map(|n| n as usize))
    }
}
