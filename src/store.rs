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

CREATE TABLE IF NOT EXISTS keystrokes (
  drill_id   INTEGER NOT NULL REFERENCES drills(id),
  seq        INTEGER NOT NULL,
  t_ns       INTEGER NOT NULL,             -- monotonic ns since drill start
  target_idx INTEGER NOT NULL,
  pos        INTEGER NOT NULL,             -- index into the target's expected sequence
  expected   TEXT,                         -- NULL when typing past the end
  typed      TEXT,                         -- NULL for backspace
  kind       TEXT    NOT NULL,             -- char | backspace
  correct    INTEGER,                      -- NULL for backspace
  PRIMARY KEY (drill_id, seq)
);
CREATE INDEX IF NOT EXISTS keystrokes_by_target ON keystrokes(drill_id, target_idx);

CREATE VIEW IF NOT EXISTS drill_summary AS
SELECT d.id, d.mode, d.parent_drill, d.started_at_ms, d.outcome,
       COUNT(*) FILTER (WHERE k.kind = 'char')                          AS chars_typed,
       COALESCE(SUM(k.correct) FILTER (WHERE k.kind = 'char'), 0)       AS chars_correct,
       (MAX(k.t_ns) - MIN(k.t_ns)) / 1e9                                 AS typing_secs,
       CASE WHEN MAX(k.t_ns) > MIN(k.t_ns) THEN
         (SUM(k.correct) FILTER (WHERE k.kind = 'char')) / 5.0
           / ((MAX(k.t_ns) - MIN(k.t_ns)) / 60e9) END                    AS wpm,
       CASE WHEN COUNT(*) FILTER (WHERE k.kind = 'char') > 0 THEN
         100.0 * SUM(k.correct) FILTER (WHERE k.kind = 'char')
           / (COUNT(*) FILTER (WHERE k.kind = 'char')) END               AS accuracy
FROM drills d LEFT JOIN keystrokes k ON k.drill_id = d.id
GROUP BY d.id;
"#;

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
    Ok(())
}

pub struct KeyRow {
    pub seq: i64,
    pub t_ns: i64,
    pub target_idx: i64,
    pub pos: i64,
    pub expected: Option<char>,
    pub typed: Option<char>,
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
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             PRAGMA foreign_keys = ON;",
        )?;
        conn.execute_batch(SCHEMA)?;
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
                k.typed.map(String::from),
                k.kind,
                k.correct.map(|b| b as i64),
            ],
        )?;
        Ok(())
    }

    /// Highest sentence number of `book` that was typed to completion.
    pub fn last_completed_sentence(&self, book: &str) -> Result<Option<usize>> {
        let prefix = format!("{book}#");
        let v: Option<i64> = self
            .conn
            .query_row(
                "SELECT MAX(CAST(substr(source_ref, ?2) AS INTEGER))
                 FROM targets
                 WHERE kind = 'sentence' AND completed_at_ns IS NOT NULL
                   AND substr(source_ref, 1, ?3) = ?1",
                params![prefix, prefix.len() as i64 + 1, prefix.len() as i64],
                |r| r.get(0),
            )
            .optional()?
            .flatten();
        Ok(v.map(|n| n as usize))
    }
}
