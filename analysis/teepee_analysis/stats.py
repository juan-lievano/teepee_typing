"""teepee-stats: the exercise you last played in one box, or one exercise in depth.

    teepee stats                       one box: the exercise of the last drill, its wpm per drill,
                                       oldest left (a bar per drill up to 40, then 40 groups)
    teepee stats sprint                the long view of one exercise, every table on its drills only:
    teepee stats master --list english_10k    trend by day, the three gaps, slow words, messy words,
    teepee stats lit --book quijote           word starts, slow keys / bigrams / trigrams
    teepee stats --full                the long view of every exercise played
    --days N   look back N days (default: all)      --tag X   only drills tagged X
    --open     also draw the master progress graph and open it in the image viewer

An exercise is a mode on one source: sprint or master on a word list, literature on a book.
Practice reps belong to the drill that spawned them and are left out of every table.
"""
import argparse
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import polars as pl

from .db import connect

# ------------------------------------------------------------------ per drill
# The SQLite view drill_summary is not used because DuckDB's sqlite reader
# cannot parse it; this is the same rule.

# A drill with fewer character keystrokes than this gets no wpm (same number
# as MIN_CHARS in src/store.rs): a two-key drill lasting 4 ms is not a speed.
MIN_CHARS = 10

PER_DRILL = """
-- Monkeytype's definitions. Accuracy counts every character keystroke. wpm
-- counts the characters of words that ended up right, the space after each
-- included, and nothing for a word that ended up wrong. A keystroke stands
-- until a later key lands at or before its position (a backspace, or the
-- character typed in its place); a word is right when no wrong key stands in
-- it, and is then worth its standing keys. Same rule as drill_summary in
-- src/store.rs.
WITH kk AS (
  SELECT k.drill_id, k.target_idx, k.kind, k.correct, k.t_ns, k.pos,
         length(substr(t.text, 1, k.pos)) - length(replace(substr(t.text, 1, k.pos), ' ', '')) AS word,
         min(k.pos) OVER (PARTITION BY k.drill_id, k.target_idx ORDER BY k.seq
                          ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS later_pos
  FROM tp.keystrokes k JOIN tp.targets t ON t.drill_id = k.drill_id AND t.idx = k.target_idx
  WHERE k.kind IN ('char', 'backspace')
),
ww AS (
  SELECT drill_id, target_idx, word,
         coalesce(sum(correct) FILTER (WHERE kind = 'char' AND (later_pos IS NULL OR later_pos > pos)), 0) AS standing_right,
         count(*) FILTER (WHERE kind = 'char' AND correct = 0 AND (later_pos IS NULL OR later_pos > pos)) AS standing_wrong
  FROM kk GROUP BY drill_id, target_idx, word
),
k AS (
  SELECT drill_id,
         count(*) FILTER (WHERE kind = 'char')                                        AS chars_typed,
         coalesce(sum(correct) FILTER (WHERE kind = 'char'), 0)                       AS chars_correct,
         max(t_ns) FILTER (WHERE kind = 'char') - min(t_ns) FILTER (WHERE kind = 'char') AS span_ns
  FROM kk GROUP BY drill_id
),
c AS (
  SELECT drill_id,
         sum(CASE WHEN standing_wrong = 0 THEN standing_right ELSE 0 END) AS chars_scored,
         count(*) FILTER (WHERE standing_wrong > 0)                      AS words_wrong
  FROM ww GROUP BY drill_id
),
w AS (
  SELECT drill_id, count(*) AS words FROM tp.targets WHERE completed_at_ns IS NOT NULL GROUP BY drill_id
)
SELECT d.id, d.mode, d.outcome, d.started_at_ms, d.failed_word,
       json_extract_string(d.config, '$.book') AS book,
       coalesce(json_extract_string(d.config, '$.book'), json_extract_string(d.config, '$.list')) AS source,
       strftime(to_timestamp(d.started_at_ms / 1000), '%Y-%m-%d') AS day,
       coalesce(w.words, 0) AS words, coalesce(k.chars_typed, 0) AS chars_typed, coalesce(k.chars_correct, 0) AS chars_correct,
       coalesce(c.chars_scored, 0) AS chars_scored, coalesce(c.words_wrong, 0) AS words_wrong,
       CASE WHEN k.chars_typed >= $min_chars THEN c.chars_scored / 5.0 / nullif(k.span_ns, 0) * 60e9 END AS wpm,
       k.span_ns / 1e9 AS secs
FROM tp.drills d LEFT JOIN k ON k.drill_id = d.id LEFT JOIN c ON c.drill_id = d.id LEFT JOIN w ON w.drill_id = d.id
WHERE d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter
ORDER BY d.id
"""

# ------------------------------------------------------------------ per exercise
# $drills is the judged (completed or failed) drills of one exercise in the
# window; every query below runs on those alone, so its numbers are
# comparable with each other and with nothing else.

# A word is what sits between spaces, so a sentence's "hacienda." or
# "carnero," is typed with its punctuation (the transition into the period is
# real typing). The word tables strip punctuation from the edges so those
# count with "hacienda" and "carnero".
BARE = "regexp_replace({}, '^[^\\p{{L}}\\p{{N}}]+|[^\\p{{L}}\\p{{N}}]+$', '', 'g')"

# Keys kanata reports late: the Colemak-DH home-row mods and the space
# layer-tap. Their report comes when the tap is decided, up to a hold time
# after the physical press, so a gap ending on one reads too long by that
# tap's hold and a gap starting on one too short. DELAY estimates the mean
# hold from the same keystrokes, as half the asymmetry between plain->hold
# and hold->plain gaps; GAPS and STARTS add it back per gap; NGRAMS clocks
# an n-gram from the key before it to the key after it, so its own delays
# cancel, and corrects the two window ends. Sound as medians, not per key.
HOLD_KEYS = "arstneio "

# Consecutive clean keystrokes (both accepted) of the exercise, with the gap between them.
GAP_PAIRS = """
WITH k AS (
  SELECT k.drill_id, k.seq, k.typed, k.kind, k.correct, k.t_ns
  FROM tp.keystrokes k WHERE k.drill_id IN ($drills)
),
g AS (
  SELECT typed, lag(typed) OVER w AS prev, (t_ns - lag(t_ns) OVER w) / 1e6 AS dt,
         lag(kind) OVER w = 'char' AND lag(correct) OVER w = 1 AND kind = 'char' AND correct = 1 AS clean
  FROM k WINDOW w AS (PARTITION BY drill_id ORDER BY seq)
),
c AS (
  SELECT prev, typed, dt + $delay * contains('$hold', prev)::INT - $delay * contains('$hold', typed)::INT AS ms
  FROM g WHERE clean AND dt < 1000
)
"""

DELAY = GAP_PAIRS + """
SELECT (mean(dt) FILTER (WHERE NOT contains('$hold', prev) AND contains('$hold', typed))
      - mean(dt) FILTER (WHERE contains('$hold', prev) AND NOT contains('$hold', typed))) / 2 AS delay_ms
FROM g WHERE clean AND dt < 1000
"""

GAPS = GAP_PAIRS + """
SELECT CASE WHEN prev = ' ' THEN 'start' WHEN typed = ' ' THEN 'space' ELSE 'letter' END AS kind,
       count(*) AS n, median(ms) AS ms
FROM c GROUP BY 1
"""

STARTS = GAP_PAIRS + """
SELECT typed AS letter, count(*) AS n, median(ms) AS ms
FROM c WHERE prev = ' ' AND regexp_matches(typed, '^\\p{Ll}$')
GROUP BY 1 HAVING n >= $min ORDER BY ms DESC
"""

NGRAMS = """
WITH k AS (
  SELECT k.drill_id, k.seq, k.typed, k.kind, k.correct, k.t_ns
  FROM tp.keystrokes k WHERE k.drill_id IN ($drills)
),
g AS (
  SELECT string_agg(k.typed, '') OVER (w ROWS BETWEEN $n - 1 PRECEDING AND CURRENT ROW) AS gram,
         bool_and(k.kind = 'char' AND k.correct = 1) OVER (w ROWS BETWEEN $n PRECEDING AND 1 FOLLOWING) AS clean,
         contains('$hold', lag(k.typed, $n) OVER w) AS hb, contains('$hold', lead(k.typed, 1) OVER w) AS ha,
         (lead(k.t_ns, 1) OVER w - lag(k.t_ns, $n) OVER w) / 1e6 AS span
  FROM k WINDOW w AS (PARTITION BY k.drill_id ORDER BY k.seq)
)
SELECT gram, count(*) AS n,
       median((span - $delay * ha::INT + $delay * hb::INT) / ($n + 1)) AS ms
FROM g WHERE clean AND span IS NOT NULL AND span < 1000 * ($n + 1) AND regexp_matches(gram, '^\\p{Ll}+$')
GROUP BY 1 HAVING n >= $min ORDER BY ms DESC LIMIT $limit
"""

# One row per word reached: a word target, or one word of a sentence target
# (keystrokes grouped by the spaces before their position). It is clocked
# from the key that ended the previous word to the key that ended it, so a
# word's time holds the gap into its first letter, its letters, and the gap
# into its own space when it has one (the last word of a sentence does not).
# Both ends are then a space, so the tap-hold delay cancels. `done` means the
# word was typed to its end or ended wrong; a drill aborted mid-word is not an
# attempt. `messy` is any wrong key or backspace on the way; `wrong` is a
# wrong key standing at the end (in master, the key that failed the drill).
WORDS = """
WITH k AS (
  SELECT k.drill_id, k.target_idx, k.seq, k.pos, k.kind, k.correct, k.t_ns, t.text, t.kind AS tkind,
         length(substr(t.text, 1, k.pos)) - length(replace(substr(t.text, 1, k.pos), ' ', '')) AS word,
         min(k.pos) OVER (PARTITION BY k.drill_id, k.target_idx ORDER BY k.seq
                          ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS later_pos
  FROM tp.keystrokes k JOIN tp.targets t ON t.drill_id = k.drill_id AND t.idx = k.target_idx
  WHERE k.drill_id IN ($drills) AND k.kind IN ('char', 'backspace')
),
w AS (
  SELECT drill_id, target_idx, word, any_value(text) AS text, any_value(tkind) AS tkind, max(t_ns) AS last_t,
         bool_or(kind = 'backspace' OR correct = 0) AS messy,
         count(*) FILTER (WHERE kind = 'char' AND correct = 0 AND (later_pos IS NULL OR later_pos > pos)) > 0 AS wrong,
         count(*) FILTER (WHERE kind = 'char' AND correct = 1 AND (later_pos IS NULL OR later_pos > pos)) AS standing
  FROM k GROUP BY 1, 2, 3
),
a AS (
  SELECT *, str_split(text, ' ')[word + 1] AS wtext,
         lag(last_t) OVER (PARTITION BY drill_id ORDER BY target_idx, word) AS prev_t,
         tkind = 'word' OR word < length(text) - length(replace(text, ' ', '')) AS has_sep
  FROM w
)
SELECT $bare AS word, length(wtext) AS letters, has_sep, messy, wrong,
       wrong OR standing = length(wtext) + has_sep::INT AS done,
       (last_t - prev_t) / 1e6 AS ms
FROM a WHERE prev_t IS NOT NULL AND length(wtext) > 0
"""

MIN_WORD = 8     # clean attempts a word needs to be ranked for pace
MIN_MESSY = 10   # attempts a word needs to be ranked for messiness
MIN_GRAM = (30, 30, 20)   # occurrences a key, bigram, trigram needs
TOP = 12         # rows per ranking


def run(con, sql, **sub):
    for key, val in sub.items():
        sql = sql.replace("$" + key, str(val))
    rel = con.sql(sql)
    return pl.DataFrame(rel.fetchall(), schema=rel.columns, orient="row", infer_schema_length=None)


def tagfilter(a):
    if not a.tag:
        return ""
    tag = a.tag.replace("'", "''")
    return f"AND list_contains(from_json(d.tags, '[\"VARCHAR\"]'), '{tag}')"


def days(a):
    return int(a.days if a.days is not None else 36500)


def drills_sql(a, mode, source):
    src = source.replace("'", "''")
    return (f"SELECT id FROM tp.drills d WHERE d.mode = '{mode}' AND d.outcome IN ('completed', 'failed')"
            f" AND coalesce(json_extract_string(d.config, '$.book'), json_extract_string(d.config, '$.list')) = '{src}'"
            f" AND d.started_at_ms >= epoch_ms(now() - INTERVAL ({days(a)}) DAY) {tagfilter(a)}")


# ------------------------------------------------------------------ drawing

BARS = 40   # most bars; past that many drills, that many groups
BAR = 2     # columns wide
FILL = "▓"  # dark shade: a full block is too bright in a light font
PAD = 2     # columns between the box's edge and its contents
MAX_ROWS = 20


def histogram(vals):
    """Bars of `vals` (None for an empty group) on a wpm axis clipped to the data: floor and ceiling at
    multiples of 10, the row step the smallest of 1/2/5/10 that fits in MAX_ROWS rows, labels every 10."""
    present = [v for v in vals if v is not None]
    lo = int(min(present) // 10 * 10)
    hi = int(-(-max(present) // 10) * 10)
    hi = max(hi, lo + 10)
    step = next(s for s in (1, 2, 5, 10, 20, 50) if (hi - lo) / s <= MAX_ROWS)
    lines = []
    y = hi
    while y > lo:
        cells = [FILL * BAR if v is not None and v >= y - 1e-9 else " " * BAR for v in vals]
        label = f"{y:>3.0f}" if y % (10 if step >= 5 else 5) == 0 else "   "
        lines.append(label + " │ " + " ".join(cells))
        y -= step
    lines.append(f"{lo:>3.0f} └─" + "─" * ((BAR + 1) * len(vals) - 1) + "  ")   # the slack keeps the right edge off the bars
    return "\n".join(lines)


def box_lines(title, body, note=None):
    """The body in a rounded box with the title set into its top edge; the note, if any, is word-wrapped
    under the body to the body's width (or the title's), so the box is as wide as its data and no wider."""
    lines = body.split("\n")
    inner = max(len(title) + 2, *(len(l) for l in lines))
    if note:
        lines += ["", *(w for para in note.split("\n") for w in textwrap.wrap(para, inner) or [""])]
    width = inner + 2 * PAD
    top = "╭─ " + title + " " + "─" * (width - len(title) - 3) + "╮"
    mid = ["│" + " " * PAD + l.ljust(width - 2 * PAD) + " " * PAD + "│" for l in ["", *lines, ""]]
    return [top, *mid, "╰" + "─" * width + "╯"]


def centred(lines):
    """Lines shifted right so the block sits in the middle of the terminal (left-aligned when piped)."""
    width = max(len(l) for l in lines)
    margin = " " * max(0, (shutil.get_terminal_size().columns - width) // 2) if sys.stdout.isatty() else ""
    return "\n".join(margin + l for l in lines)


def box(title, body, note=None):
    return centred(box_lines(title, body, note))


def boxes(parts, gap=2):
    """Several (title, body) boxes side by side when the terminal is wide enough, else one under another."""
    blocks = [box_lines(*part) for part in parts]
    columns = shutil.get_terminal_size().columns if sys.stdout.isatty() else 10 ** 6
    if sum(len(b[0]) for b in blocks) + gap * (len(blocks) - 1) <= columns:
        return centred(beside(["\n".join(b) for b in blocks], " " * gap).split("\n"))
    return "\n\n".join(centred(b) for b in blocks)


def table(cols, rows):
    """Plain columns: `cols` is (header, align, format) per column, align '<' or '>'; None cells print as '·'."""
    def cell(v, fmt):
        return "·" if v is None else format(v, fmt) if fmt else str(v)
    text = [[cell(v, c[2]) for v, c in zip(r, cols)] for r in rows]
    widths = [max(len(c[0]), *(len(t[i]) for t in text)) if text else len(c[0]) for i, c in enumerate(cols)]
    def line(cells):
        return "  ".join(f"{s:{c[1]}{w}}" for s, c, w in zip(cells, cols, widths))
    return "\n".join([line([c[0] for c in cols]), *(line(t) for t in text)])


def beside(blocks, gap="     "):
    """Several multi-line strings side by side, each padded to its own width."""
    split = [b.split("\n") for b in blocks]
    widths = [max(len(l) for l in s) for s in split]
    height = max(len(s) for s in split)
    out = []
    for i in range(height):
        out.append(gap.join((s[i] if i < len(s) else "").ljust(w) for s, w in zip(split, widths)).rstrip())
    return "\n".join(out)


def emit(title, body, note=None):
    print("\n" + box(title, body, note))


# ------------------------------------------------------------------ quick view
# One box: the exercise of the last drill played, and its wpm in the order
# the drills were typed: one bar per drill up to BARS drills, then BARS
# equal groups of the record with the mean of each.

def latest_exercise(con):
    """(mode, source) of the last drill that was not a practice rep; practice belongs to the drill that spawned it."""
    r = con.sql("SELECT mode, coalesce(json_extract_string(config, '$.book'), json_extract_string(config, '$.list'))"
                " FROM tp.drills WHERE mode <> 'practice' ORDER BY id DESC LIMIT 1").fetchall()
    return r[0] if r else (None, None)


def exercise_drills(drills: pl.DataFrame, mode, source):
    """The judged drills of one exercise, oldest first. A failed drill still has a speed."""
    return drills.filter((pl.col("mode") == mode) & (pl.col("source") == source)
                         & pl.col("outcome").is_in(["failed", "completed"])).sort("id")


def quick(a, con, drills):
    mode, source = latest_exercise(con)
    if mode is None:
        print("no drills yet")
        return
    name = f"{mode} · {source}"
    d = exercise_drills(drills, mode, source)
    wpm = d["wpm"].drop_nulls()
    if wpm.len() < 2:
        print(f"{name}: not enough drills yet")
        return
    n = d.height
    bars = min(n, BARS)
    bins = [[] for _ in range(bars)]
    for i, w in enumerate(d["wpm"].to_list()):
        if w is not None:
            bins[i * bars // n].append(w)
    means = [sum(b) / len(b) if b else None for b in bins]
    what = "lines" if mode == "literature" else "drills"
    per_bar = "a bar per drill" if n <= BARS else f"each bar the mean of ~{n / BARS:.0f}"
    title = f"{name} · {n} {what} · median {wpm.median():.0f} wpm · best {wpm.max():.0f} · {per_bar}"
    if a.days is not None:
        title += f" · last {a.days} days"
    if a.tag:
        title += f" · {a.tag}"
    print("\n" + box(title, histogram(means)) + "\n")


# ------------------------------------------------------------------ long view

def trend(d: pl.DataFrame, mode):
    """One row per day (per week once past 20 days): how much, how fast, how clean."""
    unit = "day"
    if d["day"].n_unique() > 20:
        unit = "week"
        d = d.with_columns(pl.col("day").str.to_date().dt.truncate("1w").dt.strftime("%Y-%m-%d").alias("day"))
    g = d.group_by("day").agg(
        pl.len().alias("drills"),
        pl.col("wpm").median().alias("median"),
        pl.col("wpm").max().alias("best"),
        pl.col("words_wrong").sum().alias("wrong"),
        pl.col("words").sum().alias("words"),
        (pl.col("outcome") == "failed").sum().alias("fails"),
        (pl.col("chars_correct").sum() / pl.col("chars_typed").sum() * 100).alias("acc"),
        (pl.col("secs").sum() / 60).alias("minutes"),
    ).sort("day")
    what = "lines" if mode == "literature" else "drills"
    if mode == "master":
        cols = [(unit, "<", ""), (what, ">", "d"), ("min", ">", ".0f"), ("median wpm", ">", ".0f"), ("best", ">", ".0f"),
                ("words/drill", ">", ".1f"), ("survival %", ">", ".1f")]
        rows = [(r["day"], r["drills"], r["minutes"], r["median"], r["best"], r["words"] / r["drills"],
                 100 * r["words"] / (r["words"] + r["fails"]) if r["words"] + r["fails"] else None) for r in g.iter_rows(named=True)]
        note = "survival = words completed / words attempted, the per-word odds a drill goes on"
    else:
        cols = [(unit, "<", ""), (what, ">", "d"), ("min", ">", ".0f"), ("median wpm", ">", ".0f"), ("best", ">", ".0f"),
                (f"wrong/{what[:-1]}", ">", ".1f"), ("acc %", ">", ".1f")]
        rows = [(r["day"], r["drills"], r["minutes"], r["median"], r["best"], r["wrong"] / r["drills"], r["acc"])
                for r in g.iter_rows(named=True)]
        note = "wrong = words that ended up wrong (worth nothing to wpm) · acc = keys accepted / keys typed"
    return table(cols, rows), note


def gaps_table(gaps: pl.DataFrame, delay):
    by = {r["kind"]: r for r in gaps.iter_rows(named=True)}
    rows = [(label, by[k]["n"] if k in by else None, by[k]["ms"] if k in by else None)
            for label, k in [("into a word's first letter", "start"), ("letter to letter", "letter"), ("into the space", "space")]]
    body = table([("gap", "<", ""), ("n", ">", "d"), ("median ms", ">", ".0f")], rows)
    return body, f"clean consecutive keys only · tap-hold delay of {delay:.0f} ms taken out (kanata home row and space)"


def pace_table(words: pl.DataFrame, S, L, E):
    """Words by how much slower they are than their length predicts from the exercise's three gaps."""
    clean = words.filter(pl.col("done") & ~pl.col("messy"))
    if clean.is_empty():
        return "(nothing yet)", None
    clean = clean.with_columns(
        (pl.col("ms") - (S + (pl.col("letters") - 1) * L + pl.col("has_sep").cast(pl.Int64) * E)).alias("excess"),
        (pl.col("ms") / (pl.col("letters") + pl.col("has_sep").cast(pl.Int64))).alias("per_char"),
    )
    g = (clean.group_by("word").agg(pl.len().alias("n"), pl.col("ms").median().alias("ms"),
                                    pl.col("excess").median().alias("excess"), pl.col("per_char").median().alias("per_char"))
         .filter(pl.col("n") >= MIN_WORD).sort("excess", descending=True).head(TOP))
    cols = [("word", "<", ""), ("n", ">", "d"), ("median ms", ">", ".0f"), ("excess", ">", "+.0f"), ("ms/char", ">", ".0f")]
    body = table(cols, [(r["word"], r["n"], r["ms"], r["excess"], r["per_char"]) for r in g.iter_rows(named=True)])
    return body, (f"clean attempts only, at least {MIN_WORD} · a word is clocked from the previous word's space to its own"
                  f" · excess = median ms over what the three gaps predict for its length ({S:.0f} + letters × {L:.0f} + {E:.0f})")


def messy_table(words: pl.DataFrame, mode):
    """Words by how often typing them went wrong at all."""
    done = words.filter(pl.col("done"))
    if done.is_empty():
        return "(nothing yet)", None
    g = done.group_by("word").agg(pl.len().alias("n"), (pl.col("messy").mean() * 100).alias("messy"),
                                  (pl.col("wrong").mean() * 100).alias("wrong"), pl.col("wrong").sum().alias("fails"))
    if mode == "master":
        # A master word fails the drill, and a 10k-list word is reached a few
        # times in all, so this ranks by how often it has ended a drill.
        g = g.filter(pl.col("fails") >= 2).sort(["fails", "wrong"], descending=True).head(TOP)
        cols = [("word", "<", ""), ("reached", ">", "d"), ("failed", ">", "d"), ("failed %", ">", ".0f")]
        rows = [(r["word"], r["n"], r["fails"], r["wrong"]) for r in g.iter_rows(named=True)]
        note = "failed = the wrong key that ended the drill came on this word, at least twice · practice reps not counted"
    else:
        g = g.filter(pl.col("n") >= MIN_MESSY).sort(["messy", "n"], descending=True).head(TOP)
        cols = [("word", "<", ""), ("attempts", ">", "d"), ("messy %", ">", ".0f"), ("wrong %", ">", ".0f")]
        rows = [(r["word"], r["n"], r["messy"], r["wrong"]) for r in g.iter_rows(named=True)]
        note = (f"attempts = times typed to the end, at least {MIN_MESSY} · messy = a wrong key or a backspace on the way"
                " · wrong = still wrong when the space came (worth nothing to wpm)")
    return table(cols, rows), note


def starts_table(starts: pl.DataFrame, S):
    if starts.is_empty():
        return "(nothing yet)", None
    cols = [("first letter", "<", ""), ("n", ">", "d"), ("median ms", ">", ".0f"), ("vs all", ">", "+.0f")]
    rows = [(r["letter"], r["n"], r["ms"], r["ms"] - S) for r in starts.iter_rows(named=True)]
    return table(cols, rows), f"the gap from a space into the word's first letter, by that letter · at least {MIN_GRAM[0]} words"


def grams_boxes(frames):
    """Three boxes, keys / bigrams / trigrams, with the method under each."""
    parts = []
    for what, f, m in zip(["key", "bigram", "trigram"], frames, MIN_GRAM):
        cols = [(what, "<", ""), ("n", ">", "d"), ("ms/gap", ">", ".0f")]
        body = table(cols, [(r["gram"], r["n"], r["ms"]) for r in f.iter_rows(named=True)]) if not f.is_empty() else "(nothing yet)"
        parts.append((f"slow {what}s", body, f"median ms per gap, clocked from the key before to the key after · {m}+ seen"))
    return parts


def long_view(a, con, drills, mode, source):
    name = f"{mode} · {source}"
    d = exercise_drills(drills, mode, source)
    if d.height < 2:
        print(f"\n{name}: not enough drills yet")
        return
    what = "lines" if mode == "literature" else "drills"
    minutes = d["secs"].fill_null(0).sum() / 60
    span = f"{d['day'].min()} → {d['day'].max()}" if d["day"].min() != d["day"].max() else d["day"].min()
    ndays = d["day"].n_unique()
    head = f"{name} · {d.height} {what} · {ndays} day{'s' if ndays != 1 else ''} · {minutes:.0f} min typing · {span}"
    if a.days is not None:
        head += f" · last {a.days} days"
    if a.tag:
        head += f" · {a.tag}"
    emit(head, *trend(d, mode))

    sub = dict(drills=drills_sql(a, mode, source), hold=HOLD_KEYS)
    delay = run(con, DELAY, **sub, delay=0.0)["delay_ms"][0] or 0.0
    sub["delay"] = repr(float(delay))
    gaps = run(con, GAPS, **sub)
    by = {r["kind"]: r["ms"] for r in gaps.iter_rows(named=True)}
    S, L, E = by.get("start", 0.0), by.get("letter", 0.0), by.get("space", 0.0)
    emit("the three gaps", *gaps_table(gaps, delay))

    words = run(con, WORDS, **sub, bare=BARE.format("wtext"))
    emit("slow words", *pace_table(words, S, L, E))
    emit("messy words" if mode != "master" else "failed words", *messy_table(words, mode))
    emit("word starts", *starts_table(run(con, STARTS, **sub, min=MIN_GRAM[0]), S))
    print("\n" + boxes(grams_boxes([run(con, NGRAMS, **sub, n=n, min=m, limit=40 if n == 1 else TOP) for n, m in zip((1, 2, 3), MIN_GRAM)])))


def exercises(drills: pl.DataFrame):
    """Every exercise with judged drills, most recently played first."""
    e = (drills.filter(pl.col("outcome").is_in(["failed", "completed"]) & (pl.col("mode") != "practice"))
         .group_by(["mode", "source"]).agg(pl.col("id").max().alias("last")).sort("last", descending=True))
    return [(r["mode"], r["source"]) for r in e.iter_rows(named=True)]


MODES = {"sprint": "sprint", "master": "master", "lit": "literature", "literature": "literature"}


def report(a):
    con = connect()
    drills = run(con, PER_DRILL, days=days(a), tagfilter=tagfilter(a), min_chars=MIN_CHARS)
    if drills.is_empty():
        print("no drills yet")
        return
    if a.open:
        graph(a, drills)
    if a.full:
        for mode, source in exercises(drills):
            long_view(a, con, drills, mode, source)
        print()
    elif a.exercise:
        mode = MODES[a.exercise]
        source = a.book if mode == "literature" else a.list
        if source is None:
            played = [s for m, s in exercises(drills) if m == mode]
            if not played:
                print(f"no {mode} drills yet")
                return
            source = played[0]
        long_view(a, con, drills, mode, source)
        print()
    else:
        quick(a, con, drills)


# ------------------------------------------------------------------ graph

WINDOW = 100  # drills in the rolling window for the graph

def graph(a, drills):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = drills.filter((pl.col("mode") == "master") & pl.col("outcome").is_in(["failed", "completed"])).sort("id")
    if m.height < 2:
        return
    n = m.height
    x = list(range(1, n + 1))
    wpm = m["wpm"].to_list()
    words = m["words"].to_list()
    failed = [o == "failed" for o in m["outcome"].to_list()]
    win = WINDOW

    def rolling(vals, k):
        out = []
        for i in range(len(vals)):
            seg = [v for v in vals[max(0, i - k + 1): i + 1] if v is not None]
            out.append(sum(seg) / len(seg) if seg else None)
        return out

    surv = []
    for i in range(n):
        lo = max(0, i - win + 1)
        done = sum(words[lo:i + 1]); fails = sum(failed[lo:i + 1])
        surv.append(100 * done / (done + fails) if done + fails else None)

    # validated dark palette: surface #1a1a19, series blue #3987e5, inks #f0efec / #c3c2b7
    S, INK, INK2, GRID, BLUE = "#1a1a19", "#f0efec", "#c3c2b7", "#33332f", "#3987e5"
    plt.rcParams.update({"figure.facecolor": S, "axes.facecolor": S, "text.color": INK, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "axes.edgecolor": GRID, "grid.color": GRID,
                         "font.family": "monospace", "font.size": 11})
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True, gridspec_kw={"hspace": 0.35})
    for ax in (ax1, ax2):
        ax.grid(True, axis="y", linewidth=0.6); ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(length=0)

    ax1.scatter(x, wpm, s=24, color=BLUE, alpha=0.45, linewidths=0, zorder=2)
    ax1.plot(x, rolling(wpm, win), color=BLUE, linewidth=2, zorder=3)
    ax1.set_title(f"wpm per master drill · dots are drills, line is the mean of the last {win} (fewer at the start)", loc="left", color=INK2, fontsize=11)
    ax1.set_ylabel("wpm")

    ax2.plot(x, surv, color=BLUE, linewidth=2, zorder=3)
    ax2.set_ylim(0, 100)
    ax2.set_title(f"per-word survival, last {win} drills · words completed / words attempted", loc="left", color=INK2, fontsize=11)
    ax2.set_ylabel("%"); ax2.set_xlabel("master drill number")
    tag = f" · {a.tag}" if a.tag else ""
    fig.suptitle(f"teepee · master mode · {n} drills{tag}", x=0.125, ha="left", color=INK, fontsize=13)

    # A temp file for the viewer; the data is the record, the PNG is disposable.
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "teepee-progress.png"
    fig.savefig(tmp, format="png", dpi=160, bbox_inches="tight", facecolor=S)
    print(tmp)
    subprocess.run(["open", str(tmp)], check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exercise", nargs="?", choices=sorted(MODES), help="the long view of this mode (default: the one box)")
    ap.add_argument("--list", help="word list for sprint/master (default: the one last played)")
    ap.add_argument("--book", help="book for lit (default: the one last played)")
    ap.add_argument("--full", action="store_true", help="the long view of every exercise played")
    ap.add_argument("--days", type=int, default=None, help="look back N days (default: all)")
    ap.add_argument("--tag")
    ap.add_argument("--open", action="store_true", help="also draw the master progress graph and open it in the image viewer")
    a = ap.parse_args()
    report(a)


if __name__ == "__main__":
    main()
