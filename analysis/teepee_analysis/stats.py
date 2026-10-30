"""teepee-stats: quick looks at the keystroke log.

    teepee-stats summary [--days N]   recent drills: wpm, accuracy, outcome
    teepee-stats missed  [--days N]   words you have failed on: seen x times, failed y
    teepee-stats words   [--days N]   slowest words by ms per character
    teepee-stats trigrams [--min N]   slowest trigrams by median interval to their last key

Every command takes --tag X to keep only drills tagged X (see `teepee --tag`).
"""
import argparse

import polars as pl

from .db import connect

# Same arithmetic as the SQLite view drill_summary, written here because
# DuckDB's sqlite reader cannot parse that view's FILTER clauses.
SUMMARY = """
WITH k AS (
  SELECT drill_id,
         count(*) FILTER (WHERE kind = 'char')                 AS chars_typed,
         coalesce(sum(correct) FILTER (WHERE kind = 'char'), 0) AS chars_correct,
         (max(t_ns) - min(t_ns)) / 1e9                          AS secs
  FROM tp.keystrokes GROUP BY drill_id
)
SELECT d.id, d.mode, strftime(to_timestamp(d.started_at_ms / 1000), '%Y-%m-%d %H:%M') AS started, d.outcome,
       array_to_string(from_json(d.tags, '["VARCHAR"]'), ' ') AS tags,
       round(k.chars_correct / 5.0 / nullif(k.secs, 0) * 60, 1)        AS wpm,
       round(100.0 * k.chars_correct / nullif(k.chars_typed, 0), 1)    AS acc,
       k.chars_typed AS chars
FROM tp.drills d LEFT JOIN k ON k.drill_id = d.id
WHERE d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
ORDER BY d.id DESC LIMIT 40
"""

# Seen = every time the word was reached as a target (master words and
# practice reps alike); failed = every drill that ended on it.
MISSED = """
WITH seen AS (
  SELECT t.text AS word, count(*) AS seen
  FROM tp.targets t JOIN tp.drills d ON d.id = t.drill_id
  WHERE t.kind = 'word' AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
  GROUP BY 1
),
failed AS (
  SELECT failed_word AS word, count(*) AS failed
  FROM tp.drills d
  WHERE failed_word IS NOT NULL AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
  GROUP BY 1
)
SELECT f.word, coalesce(s.seen, 0) AS seen, f.failed,
       round(100.0 * f.failed / greatest(coalesce(s.seen, 0), 1), 0) AS fail_pct
FROM failed f LEFT JOIN seen s ON s.word = f.word
ORDER BY f.failed DESC, fail_pct DESC, f.word LIMIT 40
"""

# Word speed = (completion - first key on that word) / characters, completed words only.
WORDS = """
WITH first AS (
  SELECT drill_id, target_idx, min(t_ns) AS t_first
  FROM tp.keystrokes WHERE kind = 'char' GROUP BY 1, 2
)
SELECT t.text AS word, count(*) AS n,
       round(median((t.completed_at_ns - f.t_first) / 1e6 / length(t.text)), 0) AS ms_per_char
FROM tp.targets t
JOIN first f ON f.drill_id = t.drill_id AND f.target_idx = t.idx
JOIN tp.drills d ON d.id = t.drill_id
WHERE t.kind = 'word' AND t.completed_at_ns IS NOT NULL AND d.mode <> 'practice'
  AND d.outcome <> 'afk'
  AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
GROUP BY 1 HAVING n >= $min ORDER BY ms_per_char DESC LIMIT 40
"""

# Three consecutive correct keys in one drill; dt is the interval before the third.
TRIGRAMS = """
WITH k AS (
  SELECT drill_id, seq, typed, kind, correct, t_ns,
         lag(typed, 2) OVER w AS c1, lag(typed, 1) OVER w AS c2,
         lag(kind, 2) OVER w AS k1, lag(kind, 1) OVER w AS k2,
         lag(correct, 2) OVER w AS ok1, lag(correct, 1) OVER w AS ok2,
         t_ns - lag(t_ns, 1) OVER w AS dt
  FROM tp.keystrokes
  WHERE drill_id IN (SELECT id FROM tp.drills d WHERE coalesce(d.outcome, '') <> 'afk' $tagfilter_d)
  WINDOW w AS (PARTITION BY drill_id ORDER BY seq)
)
SELECT replace(c1 || c2 || typed, ' ', '␣') AS trigram, count(*) AS n,
       round(median(dt) / 1e6, 0) AS median_ms
FROM k
WHERE kind = 'char' AND correct = 1 AND k1 = 'char' AND k2 = 'char' AND ok1 = 1 AND ok2 = 1
GROUP BY 1 HAVING n >= $min ORDER BY median_ms DESC LIMIT 40
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["summary", "missed", "words", "trigrams"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min", type=int, default=5, help="minimum occurrences (words, trigrams)")
    ap.add_argument("--tag", help="only drills carrying this tag")
    a = ap.parse_args()
    sql = {"summary": SUMMARY, "missed": MISSED, "words": WORDS, "trigrams": TRIGRAMS}[a.what]
    tag = a.tag.replace("'", "''") if a.tag else None
    sql = (
        sql.replace("$days", str(int(a.days)))
        .replace("$min", str(int(a.min)))
        .replace("$tagfilter_d", f"AND list_contains(from_json(d.tags, '[\"VARCHAR\"]'), '{tag}')" if tag else "")
        .replace("$tagfilter", f"AND list_contains(from_json(tags, '[\"VARCHAR\"]'), '{tag}')" if tag else "")
    )
    rel = connect().sql(sql)
    df = pl.DataFrame(rel.fetchall(), schema=rel.columns, orient="row")
    with pl.Config(tbl_rows=50, tbl_cols=20, tbl_width_chars=120):
        print(df)


if __name__ == "__main__":
    main()
