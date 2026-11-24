"""teepee-stats: the exercise you last played in one box, or one exercise in depth.

    teepee stats                       one box: the exercise of the last drill, its wpm per drill,
                                       oldest left (a bar per drill up to 40, then 40 groups)
    teepee stats sprint                one exercise, every box on its drills only: the wpm bars, by day,
    teepee stats master --list english_10k    the three gaps, slow words, messy words, slowest and
    teepee stats lit --book quijote           fastest bigrams, the gap into each letter (off the home row)
    teepee stats --full                the whole record: every exercise in one table, then what holds across
                                       them (slow and messy words, bigrams), every word as a dot (times
                                       typed against pace), the same words in two exercises, pace by hour and
                                       by time into a session, records
    --days N   look back N days (default: all)      --tag X   only drills tagged X
    --bars N   at most N bars in the wpm diagram, in the same width: fewer are fatter (default: 40)
    --open     also draw the master progress graph and open it in the image viewer

An exercise is a mode on one source: sprint or master on a word list, literature on a book.
Practice reps belong to the drill that spawned them and are left out of every table.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import textwrap
from collections import Counter
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
         max(t_ns) FILTER (WHERE kind = 'char') - min(t_ns) FILTER (WHERE kind = 'char') AS span_ns,
         min(t_ns) AS first_ns, max(t_ns) AS last_ns
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
SELECT d.id, d.mode, d.outcome, d.started_at_ms, d.failed_word, d.tags,
       json_extract_string(d.config, '$.book') AS book,
       coalesce(json_extract_string(d.config, '$.book'), json_extract_string(d.config, '$.list')) AS source,
       -- started_at_ms is when the drill opened; it may then sit idle, so clock times are taken from its keys
       d.started_at_ms + k.first_ns / 1000000 AS first_key_ms, d.started_at_ms + k.last_ns / 1000000 AS last_key_ms,
       strftime(to_timestamp((d.started_at_ms + coalesce(k.first_ns, 0) / 1000000) / 1000), '%Y-%m-%d') AS day,
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
# and hold->plain gaps, and GAPS adds it back per gap. Rough: good enough for the
# three gaps as medians, wrong per key, which is why ANCHORED below exists.
HOLD_KEYS = "arstneio "
MODS = HOLD_KEYS + "z"   # every key kanata reports late; z is the angle-mod Shift

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

# The gap into a key, timed only between keys whose reported time is their
# press. A home-row mod (and z, and space) is reported when its tap is decided:
# at its release, or when a same-hand key off the home row goes down. So "th"
# reads 5 ms on the laptop (t waits for its release, h is let through right
# behind it) and "he" 157; no constant puts that right. A key is an anchor
# when it is not a mod and nothing held it back: the key before is no mod, or
# is a mod of the same hand (this press decided it), or was reported 15+ ms
# earlier (already decided). A gap is timed only from one anchor to the next
# key when that is an anchor too, so a bigram or a letter with a mod in it
# ("st", "e") is not timed at all. Sharing a mod's gap with its neighbours
# (anchor to anchor over 2-3 gaps) was tried and dropped: such a stretch only
# gets anchors at both ends when the typing was slow, so it read "ti" as 246 ms.
LEFT_HAND = "qwfpbarstgzxcdv"
SETTLED_MS = 15
MAX_SPAN = 1

ANCHORED = """
WITH k AS (
  SELECT k.drill_id, k.seq, k.typed, k.t_ns, coalesce(k.kind = 'char' AND k.correct = 1, false) AS ok,
         lag(k.typed) OVER w AS prev, (k.t_ns - lag(k.t_ns) OVER w) / 1e6 AS dt,
         row_number() OVER w AS rn
  FROM tp.keystrokes k WHERE k.drill_id IN ($drills)
  WINDOW w AS (PARTITION BY k.drill_id ORDER BY k.seq)
),
a AS (
  SELECT *, sum((NOT ok)::INT) OVER (PARTITION BY drill_id ORDER BY rn) AS dirty,
         ok AND typed IS NOT NULL AND NOT contains('$hold', typed) AND (
           prev IS NULL OR NOT contains('$hold', prev) OR dt > $settled
           OR (prev <> ' ' AND contains('$left', prev) = contains('$left', typed))) AS anchor
  FROM k
),
g AS (
  SELECT drill_id, prev, typed,
         last_value(CASE WHEN anchor THEN rn END IGNORE NULLS) OVER before AS l_rn,
         last_value(CASE WHEN anchor THEN t_ns END IGNORE NULLS) OVER before AS l_t,
         last_value(CASE WHEN anchor THEN dirty END IGNORE NULLS) OVER before AS l_dirty,
         first_value(CASE WHEN anchor THEN rn END IGNORE NULLS) OVER after AS r_rn,
         first_value(CASE WHEN anchor THEN t_ns END IGNORE NULLS) OVER after AS r_t,
         first_value(CASE WHEN anchor THEN dirty END IGNORE NULLS) OVER after AS r_dirty
  FROM a
  WINDOW before AS (PARTITION BY drill_id ORDER BY rn ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
         after AS (PARTITION BY drill_id ORDER BY rn ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)
)
SELECT drill_id, prev, typed, (r_t - l_t) / 1e6 / (r_rn - l_rn) AS ms, r_rn - l_rn AS span
FROM g
WHERE r_rn - l_rn <= $max AND r_dirty = l_dirty AND (r_t - l_t) / 1e6 < 1000 * (r_rn - l_rn)
  AND regexp_matches(prev, '^[a-z]$') AND regexp_matches(typed, '^[a-z]$')
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
# `slip` is any wrong key at all: unlike `messy` it does not count the
# backspaces that pass through a word typed right on the way back to mend
# the one before it. The messy words tables and the clean cloud go by `slip`;
# `messy` only decides which attempts a word's pace is read from, since a
# backspace anywhere in a word spoils its time. A drill's first word has no gap into it, so no time:
# `timed` is false for it. Whatever counts words keeps those rows; whatever
# reads `ms` (CLEAN_TIMED: slow words, the same words in two exercises, the
# speed cloud) leaves them out.
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
         bool_or(kind = 'char' AND correct = 0) AS slip,
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
SELECT drill_id, $bare AS word, length(wtext) AS letters, has_sep, messy, slip, wrong,
       wrong OR standing = length(wtext) + has_sep::INT AS done,
       prev_t IS NOT NULL AS timed, (last_t - prev_t) / 1e6 AS ms
FROM a WHERE length(wtext) > 0
"""

CLEAN_TIMED = pl.col("done") & ~pl.col("messy") & pl.col("timed")     # the attempts a word's pace is read from

MIN_WORD = 8     # clean attempts a word needs to be ranked for pace
MIN_MESSY = 10   # attempts a word needs to be ranked for messiness
MIN_GRAM = (30, 30)   # timed gaps a letter, a bigram needs
TOP = 10         # rows per ranking


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

BARS = 40   # most bars unless --bars says otherwise; past that many drills, that many groups
BAR = 2     # columns wide
WIDTH = BARS * (BAR + 1)   # columns the bars of a full diagram take, a gap after each; --bars cuts the same width otherwise
RAMP = " ▁▂▃▄▅▆▇█"  # a cell filled by eighths from the bottom: the top of a bar lands within an eighth of a row
PAD = 2     # columns between the box's edge and its contents
MAX_ROWS = 20


def slope(xs, ys):
    """Least-squares slope of ys over xs, fitted to every point (not the bars); None when the fit is undefined."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else None


def histogram(vals, bars=BARS):
    """Bars of `vals` (None for an empty group) on a wpm axis from 10 below the lowest bar to the
    highest, both rounded out to multiples of 10, the row step the smallest of 1/2/5/10 that fits in MAX_ROWS rows, labels every 10.
    The top cell of a bar is filled to the nearest eighth of a row. `bars` of them are as wide as BARS of BAR columns:
    fewer are fatter (to the column, so some one wider than others), more are thinner, and past WIDTH / 2 they touch."""
    edges = [round(i * WIDTH / bars) for i in range(len(vals) + 1)]
    gap = 1 if WIDTH // bars >= 2 else 0
    wide = [b - a - gap for a, b in zip(edges, edges[1:])]
    present = [v for v in vals if v is not None]
    lo = max(0, int((min(present) - 10) // 10 * 10))
    hi = int(-(-max(present) // 10) * 10)
    hi = max(hi, lo + 10)
    step = next(s for s in (1, 2, 5, 10, 20, 50) if (hi - lo) / s <= MAX_ROWS)
    lines = []
    y = hi
    while y > lo:
        cells = [RAMP[0 if v is None else round(8 * min(1, max(0, (v - y) / step + 1)))] * w for v, w in zip(vals, wide)]
        label = f"{y:>3.0f}" if y % (10 if step >= 5 else 5) == 0 else "   "
        lines.append(label + " │ " + (" " * gap).join(cells))
        y -= step
    lines.append(f"{lo:>3.0f} └─" + "─" * (edges[-1] - gap) + "  ")   # the slack keeps the right edge off the bars
    return "\n".join(lines)


def soft(text):
    """The bars in the terminal's dim colour, plain when piped: a block at full strength is too bright in a light font."""
    return re.sub("[▁-▐]+", lambda m: f"\x1b[2m{m[0]}\x1b[22m", text) if sys.stdout.isatty() else text


def columns():
    """The terminal's width; 120 when piped."""
    return shutil.get_terminal_size((120, 40)).columns


def box_lines(title, body, note=None, width=None, height=None):
    """The body in a rounded box with the title set into its top edge; the note, if any, is word-wrapped
    under the body to the body's width (or the title's), so the box is as wide as its data and no wider.
    `width` and `height` (outer) pad it out to sit in a grid."""
    lines = body.split("\n")
    inner = max(len(title) + 2, *(len(l) for l in lines))
    if note:
        inner = min(max(inner, 36), max(inner, columns() - 2 - 2 * PAD))
        lines += ["", *(w for para in note.split("\n") for w in textwrap.wrap(para, inner) or [""])]
    inner = max(inner, (width or 0) - 2 - 2 * PAD)
    lines += [""] * ((height or 0) - 4 - len(lines))
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


def drawn(part, n=None):
    """The part with its body drawn: a body may be a function of the rows it may rank, so it can fill its box."""
    title, body, *note = part
    return (title, body(n or TOP) if callable(body) else body, *note)


def grid(lead, parts, gap=1):
    """Boxes (title, body, note) as one block: each of `lead` on a row of its own, then `parts` in order,
    packed into as few rows as the terminal's width allows. Every row is stretched to the widest row and every box
    in a row to the tallest, so the edges line up. A ranking whose body is a function grows into the lines its row leaves it."""
    # Widest first, each box into the first row with room, gives the number of rows; of every way
    # to fill that many or one more, the one with the fewest rows under two thirds full, then the fewest lines (tall boxes
    # share a row), then the fullest emptiest row.
    # A row then keeps the order the boxes were given in, and the rows the order of their first boxes.
    firsts_ = [box_lines(*drawn(p)) for p in parts]
    natural = [len(b[0]) for b in firsts_]
    room = columns()
    def used(r):
        return sum(natural[k] for k in r) + gap * (len(r) - 1)
    packed = []
    for i in sorted(range(len(parts)), key=lambda i: -natural[i]):
        for r in packed:
            if used(r) + gap + natural[i] <= room:
                r.append(i); break
        else:
            packed.append([i])
    tall = [len(b) for b in firsts_]
    def cost(rows):          # rows left under two thirds full, then the lines the block takes, then how empty its emptiest row is
        return (sum(3 * used(r) < 2 * room for r in rows), sum(max(tall[k] for k in r) for r in rows), -min(map(used, rows), default=0))
    best = (cost(packed), [list(r) for r in packed])
    budget = 200_000          # placements tried; the search keeps the best it has seen when this runs out
    def place(i, rows):
        nonlocal best, budget
        budget -= 1
        if budget < 0 or (0, sum(max(tall[k] for k in r) for r in rows)) > best[0][:2]:    # rows only grow taller
            return
        if i == len(parts):
            if cost(rows) < best[0]:
                best = (cost(rows), [list(r) for r in rows])
            return
        for r in rows:
            if used(r) + gap + natural[i] <= room:
                r.append(i); place(i + 1, rows); r.pop()
        if len(rows) <= len(packed):
            rows.append([i]); place(i + 1, rows); rows.pop()
    if len(parts) <= 16:
        place(0, [])
    packed = best[1]
    spec = [[p] for p in lead] + [[parts[i] for i in sorted(r)] for r in sorted(packed, key=min)]
    rows = [[box_lines(*drawn(p)) for p in ps] for ps in spec]
    total = max(sum(len(b[0]) for b in r) + gap * (len(r) - 1) for r in rows)
    out = []
    for r, ps in zip(rows, spec):
        spare = total - sum(len(b[0]) for b in r) - gap * (len(r) - 1)
        widths = [len(b[0]) + spare // len(r) + (k < spare % len(r)) for k, b in enumerate(r)]
        height = max(len(b) for b in r)
        def fill(p, w, b):   # the most rows that keep the box inside the row's height and its own width
            for n in range(TOP + height - len(b), TOP, -1) if callable(p[1]) else ():
                more = box_lines(*drawn(p, n), width=w)
                if len(more) <= height and len(more[0]) == w:
                    return drawn(p, n)
            return drawn(p)
        ps = [fill(p, w, b) for p, w, b in zip(ps, widths, r)]
        boxes = [box_lines(*p, width=w, height=height) for p, w in zip(ps, widths)]
        height = max(len(b) for b in boxes)      # a wider box may wrap its note onto fewer lines
        boxes = [b[:-1] + ["│" + " " * (len(b[0]) - 2) + "│"] * (height - len(b)) + b[-1:] for b in boxes]
        out += [(" " * gap).join(line) for line in zip(*boxes)]
    return centred(out)


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


def wpm_box(a, d, name, mode):
    """(title, histogram) of an exercise's wpm in the order typed; None with fewer than two timed drills."""
    wpm = d["wpm"].drop_nulls()
    if wpm.len() < 2:
        return None
    n = d.height
    means = binned(d["wpm"].to_list(), a.bars)
    what = "lines" if mode == "literature" else "drills"
    per_bar = "a bar per drill" if n <= a.bars else f"each bar the mean of ~{n / a.bars:.0f}"
    title = f"{name} · {n} {what} · median {wpm.median():.0f} wpm · best {wpm.max():.0f} · {per_bar}"
    judged = d.filter(pl.col("wpm").is_not_null())
    trend = slope(judged["secs"].cum_sum().to_list(), judged["wpm"].to_list())   # secs: first key to last of each drill
    if trend is not None:
        title += f" · trend {3600 * trend:+.1f} wpm / hour typing"
    return title + window(a), histogram(means, a.bars)


def quick(a, con, drills):
    mode, source = latest_exercise(con)
    if mode is None:
        print("no drills yet")
        return
    name = f"{mode} · {source}"
    lead = wpm_box(a, exercise_drills(drills, mode, source), name, mode)
    if lead is None:
        print(f"{name}: not enough drills yet")
        return
    print("\n" + soft(box(*lead)) + "\n")


# ------------------------------------------------------------------ pieces of a view
# Every box below is (title, body, note). A ranking is at most TOP rows with a
# bar on its ranking number; a spread over many labels (keys, first letters)
# is a row of columns, so 26 letters take 8 lines and not 26.

def binned(vals, bins):
    """`vals` in order, cut into at most `bins` equal runs, the mean of each (None for a run with no value)."""
    n = len(vals)
    bins = min(n, bins)
    groups = [[] for _ in range(bins)]
    for i, v in enumerate(vals):
        if v is not None:
            groups[i * bins // n].append(v)
    return [sum(g) / len(g) if g else None for g in groups]


def hbar(frac, width=10):
    """A bar `frac` of `width` columns long, to the eighth of a column."""
    n = round(max(0.0, min(1.0, frac)) * width * 8)
    return ("█" * (n // 8) + (" ▏▎▍▌▋▊▉"[n % 8] if n % 8 else "")).ljust(width)


def sided(v, most, half=5):
    """A bar left of the axis for a negative v, right for a positive one, to half a column; `most` fills a side."""
    n = round(min(1.0, abs(v) / most) * half * 2) if most else 0
    full, odd = "█" * (n // 2), n % 2
    left = (("▐" if odd else "") + full).rjust(half) if v < 0 else " " * half
    right = (full + ("▌" if odd else "")).ljust(half) if v > 0 else " " * half
    return left + "│" + right


def with_excess(words: pl.DataFrame):
    """Clean attempts, each with its time over what its length predicts from the gaps S, L, E of its own exercise."""
    sep = pl.col("has_sep").cast(pl.Int64)
    return words.filter(CLEAN_TIMED).with_columns(
        (pl.col("ms") - (pl.col("S") + (pl.col("letters") - 1) * pl.col("L") + sep * pl.col("E"))).alias("excess"))


def slow_words(words: pl.DataFrame, top=TOP):
    """Words by how much slower they are, typed clean, than their length predicts."""
    g = (with_excess(words).group_by("word")
         .agg(pl.len().alias("n"), pl.col("excess").median().alias("excess"))
         .filter((pl.col("n") >= MIN_WORD) & (pl.col("excess") > 0))
         .sort("excess", descending=True).head(top))
    if g.is_empty():
        return "(nothing yet)"
    top = g["excess"].max()
    return table([("word", "<", ""), ("", "<", ""), ("ms over", ">", "+.0f"), ("n", ">", "d")],
                 [(r["word"], hbar(r["excess"] / top), r["excess"], r["n"]) for r in g.iter_rows(named=True)])


def messy_words(words: pl.DataFrame, top=TOP):
    """Words by how often a wrong key landed in them (a backspace alone, passing through, is no mistake)."""
    g = (words.filter(pl.col("done")).group_by("word")
         .agg(pl.len().alias("n"), (pl.col("slip").mean() * 100).alias("messy"))
         .filter((pl.col("n") >= MIN_MESSY) & (pl.col("messy") > 0))
         .sort(["messy", "n"], descending=True).head(top))
    if g.is_empty():
        return "(nothing yet)"
    return table([("word", "<", ""), ("", "<", ""), ("messy %", ">", ".0f"), ("n", ">", "d")],
                 [(r["word"], hbar(r["messy"] / 100), r["messy"], r["n"]) for r in g.iter_rows(named=True)])


def failed_words(words: pl.DataFrame):
    """Master: the words that have ended a drill most often (a 10k-list word is reached a few times in all)."""
    g = (words.filter(pl.col("done")).group_by("word")
         .agg(pl.len().alias("n"), pl.col("wrong").sum().alias("fails"))
         .filter(pl.col("fails") >= 2).sort(["fails", "n"], descending=[True, False]).head(TOP))
    if g.is_empty():
        return "(nothing yet)"
    top = g["fails"].max()
    return table([("word", "<", ""), ("", "<", ""), ("ended", ">", "d"), ("of", ">", "d")],
                 [(r["word"], hbar(r["fails"] / top), r["fails"], r["n"]) for r in g.iter_rows(named=True)])


def anchored(con, ids):
    """One row per letter-to-letter gap that could be timed (neither key a home-row mod): prev, typed, ms."""
    return run(con, ANCHORED, drills=ids, hold=MODS, left=LEFT_HAND, settled=SETTLED_MS, max=MAX_SPAN)


def bigram_box(gaps: pl.DataFrame):
    """The slowest and the fastest bigrams side by side, bars on one scale."""
    grams = (gaps.group_by((pl.col("prev") + pl.col("typed")).alias("gram"))
             .agg(pl.len().alias("n"), pl.col("ms").median().alias("ms"))
             .filter(pl.col("n") >= MIN_GRAM[1]).sort("ms", descending=True))
    if grams.height < 2:
        return ("bigrams", "(nothing yet)", None)
    k = min(TOP, grams.height // 2)
    top = grams["ms"].max()
    def side(f, head):
        return table([(head, "<", ""), ("", "<", ""), ("ms", ">", ".0f"), ("n", ">", "d")],
                     [(r["gram"], hbar(r["ms"] / top, 8), r["ms"], r["n"]) for r in f.iter_rows(named=True)])
    body = beside([side(grams.head(k), "slowest"), side(grams.tail(k).reverse(), "fastest")], "    ")
    return ("bigrams · off the home row", body, f"ms between two keys, typed clean {MIN_GRAM[1]}+ times · home-row keys report late, so pairs with one are left out")


def letters_box(gaps: pl.DataFrame):
    """A column per letter, slowest left: the gap into it from the letter before."""
    keys = (gaps.group_by("typed").agg(pl.len().alias("n"), pl.col("ms").median().alias("ms"))
            .filter(pl.col("n") >= MIN_GRAM[0]).sort("ms", descending=True))
    if keys.is_empty():
        return ("letters", "(nothing yet)", None)
    labels, vals = keys["typed"].to_list(), keys["ms"].to_list()
    lo = max(0, int((min(vals) - 10) // 10 * 10))
    hi = max(int(-(-max(vals) // 10) * 10), lo + 10)
    rows = 8
    step = (hi - lo) / rows
    lines = []
    for i in range(rows):
        y = hi - i * step
        lines.append((f"{hi:>4.0f}" if i == 0 else "    ") + " │ "
                     + " ".join(RAMP[round(8 * min(1, max(0, (v - y) / step + 1)))] * BAR for v in vals))
    lines.append(f"{lo:>4.0f} └─" + "─" * ((BAR + 1) * len(labels) - 1))
    lines.append("       " + " ".join(f"{l:<{BAR}}" for l in labels))
    return ("letters off the home row · ms into each, slowest left", "\n".join(lines),
            f"median ms from the letter before, also off the home row · typed clean {MIN_GRAM[0]}+ times")


def gaps_of(con, ids):
    """(tap-hold delay, {start, letter, space: median ms}) of the clean keystrokes of these drills."""
    sub = dict(drills=ids, hold=HOLD_KEYS)
    delay = run(con, DELAY, **sub, delay=0.0)["delay_ms"][0] or 0.0
    gaps = run(con, GAPS, **sub, delay=repr(float(delay)))
    by = {r["kind"]: r["ms"] for r in gaps.iter_rows(named=True)}
    return float(delay), {k: by.get(k) or 0.0 for k in ("start", "letter", "space")}


def id_list(d: pl.DataFrame):
    return ",".join(str(i) for i in d["id"].to_list()) or "NULL"


def window(a):
    return (f" · last {a.days} days" if a.days is not None else "") + (f" · {a.tag}" if a.tag else "")


# ------------------------------------------------------------------ one exercise
# The wpm histogram of the quick view, then that exercise alone: by day, the
# three gaps, slow and messy words, bigrams, keys and word starts.

def by_day(d: pl.DataFrame, mode):
    """One row per day (per week once past 14 days): how much, how fast, how clean."""
    unit = "day"
    if d["day"].n_unique() > 14:
        unit = "week of"
        d = d.with_columns(pl.col("day").str.to_date().dt.truncate("1w").dt.strftime("%Y-%m-%d").alias("day"))
    g = d.group_by("day").agg(
        pl.len().alias("drills"), pl.col("wpm").median().alias("median"), pl.col("wpm").max().alias("best"),
        pl.col("words_wrong").sum().alias("wrong"), pl.col("words").sum().alias("words"),
        (pl.col("outcome") == "failed").sum().alias("fails"),
        (pl.col("chars_correct").sum() / pl.col("chars_typed").sum() * 100).alias("acc"),
        (pl.col("secs").sum() / 60).alias("minutes"),
    ).sort("day")
    what = "lines" if mode == "literature" else "drills"
    cols = [(unit, "<", ""), (what, ">", "d"), ("min", ">", ".0f"), ("wpm", ">", ".0f"), ("best", ">", ".0f")]
    if mode == "master":
        cols += [("words/drill", ">", ".1f")]
        rows = [(r["day"][5:], r["drills"], r["minutes"], r["median"], r["best"], r["words"] / r["drills"]) for r in g.iter_rows(named=True)]
        note = "wpm = the median drill"
    else:
        cols += [("wrong", ">", ".1f"), ("acc %", ">", ".1f")]
        rows = [(r["day"][5:], r["drills"], r["minutes"], r["median"], r["best"], r["wrong"] / r["drills"], r["acc"])
                for r in g.iter_rows(named=True)]
        note = f"wpm = the median · wrong = words ended wrong per {what[:-1]}"
    return (f"by {unit.split()[0]}", table(cols, rows), note)


def gaps_box(gaps, delay):
    top = max(gaps.values()) or 1
    rows = [(label, hbar(gaps[k] / top), gaps[k])
            for label, k in [("into a word's first letter", "start"), ("letter to letter", "letter"), ("into the space", "space")]]
    return ("the three gaps", table([("gap", "<", ""), ("", "<", ""), ("ms", ">", ".0f")], rows),
            f"medians, typed clean · tap-hold delay {delay:.0f} ms taken out")


def long_view(a, con, drills, mode, source):
    name = f"{mode} · {source}"
    d = exercise_drills(drills, mode, source)
    lead = wpm_box(a, d, name, mode)
    if lead is None:
        print(f"\n{name}: not enough drills yet\n")
        return
    ids = id_list(d)
    delay, gaps = gaps_of(con, ids)
    sub = dict(drills=ids, hold=HOLD_KEYS, delay=repr(delay))
    words = run(con, WORDS, **sub, bare=BARE.format("wtext")).with_columns(
        pl.lit(name).alias("ex"), pl.lit(gaps["start"]).alias("S"), pl.lit(gaps["letter"]).alias("L"), pl.lit(gaps["space"]).alias("E"))
    gaps_in = anchored(con, ids)
    parts = [
        by_day(d, mode),
        gaps_box(gaps, delay),
        ("slow words", slow_words(words), f"typed clean {MIN_WORD}+ times · ms slower than a word that long usually takes"),
        ("failed words", failed_words(words), "ended a drill twice or more · of = times reached") if mode == "master" else
        ("messy words", messy_words(words), f"{MIN_MESSY}+ attempts · messy = a wrong key landed in the word"),
        bigram_box(gaps_in),
        letters_box(gaps_in),
        misses_box(con, ids),
    ]
    print("\n" + soft(grid([lead], [p for p in parts if p])) + "\n")


# ------------------------------------------------------------------ the whole record
# --full: every exercise side by side, then what holds across them. A word,
# bigram or key is pooled over every exercise it came up in; a word's excess
# is taken against the gaps of the exercise each attempt was typed in, so a
# slow exercise does not make its words slow.

SESSION_GAP_MIN = 20     # this long between one drill's last key and the next one's first starts a new session
MIN_SHARED = 30          # words two exercises must share to be compared
KEYBOARDS = ("macbook", "voyager")   # the two tags compared; a drill carries the one it was typed on
MIN_KEYBOARD = 5         # drills an exercise needs on each keyboard to be compared
MIN_SLOT = 10            # drills an hour or a stretch of a session needs to be shown


def exercises(drills: pl.DataFrame):
    """Every exercise with judged drills, most recently played first."""
    e = (drills.filter(pl.col("outcome").is_in(["failed", "completed"]) & (pl.col("mode") != "practice"))
         .group_by(["mode", "source"]).agg(pl.col("id").max().alias("last")).sort("last", descending=True))
    return [(r["mode"], r["source"]) for r in e.iter_rows(named=True)]


def exercise_table(per, practice):
    """A row per exercise, most typed first: how much, how fast, how clean, its three gaps."""
    cols = [("exercise", "<", ""), ("drills", ">", "d"), ("min", ">", ".0f"), ("wpm", ">", ".0f"), ("last 20", ">", ".0f"),
            ("best", ">", ".0f"), ("acc %", ">", ".1f"), ("wrong", ">", ".1f"), ("start", ">", ".0f"), ("letter", ">", ".0f"),
            ("space", ">", ".0f")]
    rows = []
    for name, e in per.items():
        d, wpm = e["d"], e["d"]["wpm"].drop_nulls()
        typed = d["chars_typed"].sum()
        rows.append((name, d.height, d["secs"].fill_null(0).sum() / 60, wpm.median(), wpm.tail(20).median(), wpm.max(),
                     100 * d["chars_correct"].sum() / typed if typed else None,
                     None if e["mode"] == "master" else d["words_wrong"].sum() / d.height,
                     e["gaps"]["start"], e["gaps"]["letter"], e["gaps"]["space"]))
    if practice.height:
        rows.append(("practice reps", practice.height, practice["secs"].fill_null(0).sum() / 60, *[None] * 8))
    note = "wpm = median · wrong = words ended wrong per drill · start, letter, space = median ms into each"
    return table(cols, rows), note


def same_words(words: pl.DataFrame, order):
    """Pairs of exercises that share words: the pace of those words, typed clean, in each."""
    per = (words.filter(CLEAN_TIMED)
           .with_columns((pl.col("ms") / (pl.col("letters") + pl.col("has_sep").cast(pl.Int64))).alias("pc"))
           .group_by(["ex", "word"]).agg(pl.col("pc").median().alias("pc")))
    rows = []
    for i, x in enumerate(order):
        for y in order[i + 1:]:
            j = per.filter(pl.col("ex") == x).join(per.filter(pl.col("ex") == y), on="word", suffix="_b")
            if j.height >= MIN_SHARED:
                ma, mb = j["pc"].median(), j["pc_b"].median()
                slow, fast = (x, y) if ma >= mb else (y, x)
                rows.append((j.height, fast, min(ma, mb), slow, max(ma, mb), 100 * (max(ma, mb) / min(ma, mb) - 1)))
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: -r[5])[:8]
    top = max(r[5] for r in rows) or 1
    body = table([("faster in", "<", ""), ("ms/key", ">", ".0f"), ("slower in", "<", ""), ("ms/key", ">", ".0f"),
                  ("", "<", ""), ("slower", ">", "+.0f"), ("words", ">", "d")],
                 [(f, a, s, b, hbar(p / top, 8), p, n) for n, f, a, s, b, p in rows])
    return ("the same words in two exercises", body,
            f"median ms per key of the words typed clean in both ({MIN_SHARED}+)")


def rhythm(drills: pl.DataFrame):
    """Two boxes: each judged drill's wpm against the median of its exercise that day, by hour of the day and by time into a session. Clock times are of keys, never of a drill opening."""
    from datetime import datetime
    d = drills.filter(pl.col("first_key_ms").is_not_null()).sort("first_key_ms")
    d = d.with_columns(
        ((pl.col("first_key_ms") - pl.col("last_key_ms").shift(1)).fill_null(0) > SESSION_GAP_MIN * 60_000).cum_sum().alias("session"),
        pl.col("first_key_ms").map_elements(lambda ms: datetime.fromtimestamp(ms / 1000).hour, return_dtype=pl.Int64).alias("hour"))
    d = d.with_columns(((pl.col("first_key_ms") - pl.col("first_key_ms").min().over("session")) / 60_000).alias("into"))
    j = d.filter(pl.col("outcome").is_in(["failed", "completed"]) & (pl.col("mode") != "practice") & pl.col("wpm").is_not_null())
    j = j.with_columns(pl.col("wpm").median().over(["mode", "source", "day"]).alias("usual"),
                       pl.len().over(["mode", "source", "day"]).alias("that_day"))
    j = j.filter(pl.col("that_day") >= 5).with_columns((100 * (pl.col("wpm") / pl.col("usual") - 1)).alias("rel"))
    if j.is_empty():
        return []
    edges = [5, 10, 20, 40]
    names = ["first 5 min", "5 – 10", "10 – 20", "20 – 40", "past 40"]
    j = j.with_columns(pl.col("into").map_elements(lambda m: names[sum(m >= e for e in edges)], return_dtype=pl.String).alias("stretch"),
                       (pl.col("hour") // 3 * 3).alias("hour"))
    def side(key, head, order, fmt):
        g = j.group_by(key).agg(pl.len().alias("n"), pl.col("rel").mean().alias("rel")).filter(pl.col("n") >= MIN_SLOT)
        by = {r[key]: r for r in g.iter_rows(named=True)}
        rows = [(fmt(k), by[k]["n"], by[k]["rel"]) for k in order if k in by]
        most = max((abs(r[2]) for r in rows), default=0)
        return table([(head, "<", ""), ("drills", ">", "d"), ("", "<", ""), ("%", ">", "+.1f")],
                     [(k, n, sided(v, most), v) for k, n, v in rows])
    how = "wpm against that day's median for the exercise"
    return [("pace by hour", side("hour", "hour", range(0, 24, 3), lambda h: f"{h:02d} – {h + 3:02d}"), how),
            ("pace into a session", side("stretch", "minutes in", names, str), f"{how} · {SESSION_GAP_MIN} min idle ends a session")]


def keyboards_box(judged: pl.DataFrame, gaps_in: pl.DataFrame):
    """The two keyboards on the exercises both have typed: wpm, accuracy, and the gap between keys off the home row."""
    a, b = KEYBOARDS
    d = judged.with_columns(pl.when(pl.col("tags").str.contains(f'"{a}"')).then(pl.lit(a))
                            .when(pl.col("tags").str.contains(f'"{b}"')).then(pl.lit(b)).alias("kb")).filter(pl.col("kb").is_not_null())
    gap = dict((r[:3], r[3]) for r in gaps_in.join(d.select(pl.col("id").alias("drill_id"), "mode", "source", "kb"), on="drill_id")
               .group_by(["mode", "source", "kb"]).agg(pl.col("ms").median()).iter_rows())
    rows = []
    for (mode, source), g in d.group_by(["mode", "source"], maintain_order=True):
        x, y = g.filter(pl.col("kb") == a), g.filter(pl.col("kb") == b)
        if min(x.height, y.height) < MIN_KEYBOARD or x["wpm"].drop_nulls().is_empty() or y["wpm"].drop_nulls().is_empty():
            continue
        acc = lambda f: 100 * f["chars_correct"].sum() / f["chars_typed"].sum()
        ms = lambda kb: gap.get((mode, source, kb))
        rows.append((f"{mode} · {source}", x.height, y.height, x["wpm"].median(), y["wpm"].median(), y["wpm"].median() - x["wpm"].median(),
                     acc(x), acc(y), ms(a), ms(b)))
    if not rows:
        return None
    rows.sort(key=lambda r: -(r[1] + r[2]))
    most = max(abs(r[5]) for r in rows)
    cols = [("exercise", "<", ""), ("drills", ">", ""), ("wpm", ">", ""), ("", "<", ""), (f"{b}", ">", "+.0f"), ("acc %", ">", ""), ("off-home ms", ">", "")]
    pair = lambda u, v, f: f"{u:{f}} / {v:{f}}" if u is not None and v is not None else None
    body = table(cols, [(n, pair(nx, ny, "d"), pair(wx, wy, ".0f"), sided(diff, most), diff, pair(ax, ay, ".1f"), pair(mx, my, ".0f"))
                        for n, nx, ny, wx, wy, diff, ax, ay, mx, my in rows])
    return (f"{a} / {b}", body, f"each pair {a} first · bar = {b}'s wpm over {a}'s · off-home ms = median gap between two keys off the home row")


MISSES = """
SELECT expected, typed, correct, count(*) AS n FROM tp.keystrokes
WHERE drill_id IN ($drills) AND kind = 'char' AND regexp_matches(expected, '^[a-z ]$') AND (correct = 1 OR regexp_matches(typed, '^[a-z ]$'))
GROUP BY 1, 2, 3
"""
MISS_ROWS = 12       # keys in the matrix: the ones missed at the highest rate
MISS_MIN = 40        # misses a key needs to get a row
MISS_SHARE = 0.08    # a key gets a column when it took this share of some row's misses; the rest go under "other"
CELL = 3             # a cell is this many braille characters wide: 24 dots
DOTS = (0x01, 0x02, 0x04, 0x40, 0x08, 0x10, 0x20, 0x80)


def scatter(part, whole, seed):
    """A cell CELL characters wide with part/whole of its dots lit (any at all lights one): dealt to its characters
    evenly, then at random inside each. `seed` fixes the draw, so a cell changes only with its share."""
    import random
    rng = random.Random(seed)
    lit = -(-8 * CELL * part // whole) if part else 0
    order = list(range(CELL))
    rng.shuffle(order)
    cells = [0] * CELL
    for i, c in enumerate(order):
        for bit in rng.sample(DOTS, lit // CELL + (i < lit % CELL)):
            cells[c] |= bit
    return "".join(chr(0x2800 + b) for b in cells)


def misses_box(con, ids):
    """The keys missed at the highest rate, a row each: where its misses went, each cell's dots against the row's commonest miss."""
    f = run(con, MISSES, drills=ids)
    if f.is_empty():
        return None
    pressed = dict(f.group_by("expected").agg(pl.col("n").sum()).iter_rows())
    wrong = {}
    for e, t, _, n in f.filter(pl.col("correct") == 0).iter_rows():
        wrong.setdefault(e, {})[t] = n
    keys = sorted((e for e in wrong if sum(wrong[e].values()) >= MISS_MIN), key=lambda e: -sum(wrong[e].values()) / pressed[e])[:MISS_ROWS]
    if not keys:
        return None
    total = {e: sum(wrong[e].values()) for e in keys}
    cols = sorted({t for e in keys for t, n in wrong[e].items() if n / total[e] >= MISS_SHARE})
    show = lambda k: "␣" if k == " " else k
    head = "     " + "  ".join(f"{show(t):<{CELL}}" for t in [*cols, "other"]) + "   misses  of presses"
    lines = [head, "  ───" + "─" * ((CELL + 2) * (len(cols) + 1) - 2)]
    for e in keys:
        other = total[e] - sum(wrong[e].get(t, 0) for t in cols)
        most = max(max(wrong[e].get(t, 0) for t in cols), 1)        # a row's fullest cell is its commonest miss; "other" is many keys, and may only tie it
        cells = [scatter(wrong[e].get(t, 0), most, f"{e}/{t}") for t in cols] + [scatter(min(other, most), most, f"{e}/other")]
        lines.append(f"{show(e)}  │ " + "  ".join(cells) + f"   {total[e]:>6}  {100 * total[e] / pressed[e]:>9.1f}%")
    return ("the key hit instead · rows wanted, columns typed", "\n".join(lines),
            f"the {len(keys)} keys missed most often ({MISS_MIN}+ misses) · a row's full cell is its commonest miss")


JOURNAL_WEEKS = 12     # weeks of the practice wall in the full view; `teepee journal` shows as many as fit


def journal_box(con):
    """The practice wall of `teepee journal`, its latest weeks: every drill ever, whatever the window."""
    from datetime import date, timedelta
    from . import journal            # journal imports this module
    by_day = journal.minutes_by_day(con)
    if not by_day:
        return None
    first = min(by_day) - timedelta(days=min(by_day).weekday())
    last = date.today() - timedelta(days=date.today().weekday())
    weeks = [first + timedelta(weeks=i) for i in range((last - first).days // 7 + 1)][-JOURNAL_WEEKS:]
    body, note = journal.wall(by_day, weeks).rsplit("\n\n", 1)
    return (f"journal · {sum(by_day.values()):.0f} min", body, note)


def streaks(days):
    """(longest run of consecutive days, the run ending on the last day) of sorted ISO dates."""
    from datetime import date
    ds = [date.fromisoformat(x) for x in days]
    best = run_ = 1
    for p, q in zip(ds, ds[1:]):
        run_ = run_ + 1 if (q - p).days == 1 else 1
        best = max(best, run_)
    return best, run_


def records(con, drills, words, ids):
    """One line per fact about the record as a whole."""
    judged = drills.filter(pl.col("outcome").is_in(["failed", "completed"]) & (pl.col("mode") != "practice"))
    rows = [("first drill", drills["day"].min(), f"last {drills['day'].max()}")]
    def top(d, col):
        return d.sort(col, descending=True).row(0, named=True) if d.height else None
    for mode, label in [("sprint", "fastest sprint"), ("literature", "fastest line"), ("master", "fastest master drill")]:
        m = judged.filter((pl.col("mode") == mode) & pl.col("wpm").is_not_null() & (pl.col("words") >= (5 if mode == "master" else 1)))
        if r := top(m, "wpm"):
            rows.append((label, f"{r['wpm']:.0f} wpm", f"{r['source']} · {r['day']}"))
    if r := top(judged.filter(pl.col("mode") == "master"), "words"):
        rows.append(("longest master run", f"{r['words']} words", f"{r['source']} · {r['day']}"))
    s = judged.filter((pl.col("mode") == "sprint") & (pl.col("outcome") == "completed"))
    if s.height:
        clean = s.filter(pl.col("chars_correct") == pl.col("chars_typed")).height
        rows.append(("flawless sprints", f"{clean} of {s.height}", "not one wrong key"))
    day = drills.group_by("day").agg((pl.col("secs").sum() / 60).alias("min"), pl.len().alias("n"))
    r = top(day, "min")
    rows.append(("longest day", f"{r['min']:.0f} min", f"{r['n']} drills · {r['day']}"))
    best, now = streaks(sorted(day["day"].to_list()))
    rows.append(("days in a row", f"{best}", "and counting" if now == best else f"{now} now"))
    done = words.filter(pl.col("done"))
    if done.height:
        r = top(done.group_by("word").agg(pl.len().alias("n")), "n")
        rows.append(("most typed word", r["word"], f"{r['n']} times"))
    f = judged.filter((pl.col("mode") == "master") & pl.col("failed_word").is_not_null()).group_by("failed_word").agg(pl.len().alias("n"))
    if (r := top(f, "n")) and r["n"] > 1:
        rows.append(("ended the most master drills", r["failed_word"], f"{r['n']} drills"))
    k = dict(con.sql(f"SELECT kind, count(*) FROM tp.keystrokes WHERE drill_id IN ({ids}) GROUP BY 1").fetchall())
    chars, back = k.get("char", 0), k.get("backspace", 0)
    if chars:
        rows.append(("backspaces", f"{back:,}".replace(",", " "), f"one per {chars / back:.0f} keys" if back else ""))
    return ("records", table([("", "<", ""), ("", ">", ""), ("", "<", "")], rows).split("\n", 1)[1], None)


def list_words(words: pl.DataFrame, name):
    """One sprint list: its slowest words beside the ones mistyped most."""
    if slow_words(words) == messy_words(words):     # neither has a word yet
        return None
    return (f"{name} · slow and messy words", lambda n: beside([slow_words(words, n), messy_words(words, n)], "    "),
            f"slow: clean {MIN_WORD}+ times, ms over the usual for its length · messy: {MIN_MESSY}+ attempts, % with a wrong key")


def list_words_of(name):
    """The words of a list, lowercased, from the repo's assets (the venv sits in analysis/, beside them); None if not there."""
    path = Path(sys.prefix).parent.parent / "assets" / ("english.json" if name == "english_200" else f"{name}.json")
    return sorted({w.lower() for w in json.loads(path.read_text())["words"]}) if path.exists() else None


def grams(word, k):
    return [word[i:i + k] for i in range(len(word) - k + 1)]


def coverage_box(words: pl.DataFrame, lists):
    """For each list: how many times over all of it has been typed, and how far the next time through has got,
    in its distinct words, trigrams and bigrams."""
    done = words.filter(pl.col("done")).with_columns(pl.col("word").str.to_lowercase())
    met = dict(done.group_by("word").agg(pl.len()).iter_rows())
    seen = {0: Counter(met), 3: Counter(), 2: Counter()}     # times each word, trigram, bigram was typed
    for w, n in met.items():
        for k in (3, 2):
            for g in grams(w, k):
                seen[k][g] += n
    rows = []
    for name in sorted(lists, key=lambda name: len(list_words_of(name) or [])):     # shortest list first
        if (ws := list_words_of(name)) is None:
            continue
        for k, what in ((0, "words"), (3, "trigrams"), (2, "bigrams")):      # a line each: the box is narrow and as tall as its row
            have = set(ws) if k == 0 else {g for w in ws for g in grams(w, k)}
            over = min(seen[k][g] for g in have)        # every one of them has been typed this many times
            share = sum(seen[k][g] > over for g in have) / len(have)
            rows.append((name if k == 0 else "", what, len(have), f"{over}×", hbar(share, 8), int(share * 100)))      # rounded down: 100 only when none is missing
    if not rows:
        return None
    cols = [("list", "<", ""), ("", "<", ""), ("", ">", "d"), ("all", ">", ""), ("", "<", ""), ("%", ">", "d")]
    return ("lists covered", table(cols, rows),
            "all = times every one has been typed · % = how many have been typed once more")


CLOUD = (20, 5)       # the word clouds' scale: a label every 20 wpm (or 20 %), 5 lines apart (the plot is as wide as the terminal)
CLOUD_SD = 100        # the clean cloud sets a word seen n times off its share by a normal draw of deviation CLOUD_SD / (n + 1)² points
CLOUD_TRIES = 256     # ... drawn again, up to this many times in all, while the dot it lands on is already lit: the bell has no
                      # edge, so each further try may set a word further from its share, and trying until one is free would not end


def count_ticks(at, most, across):
    """The line of labels under a doubling scale: 1, 2, 4, 8 ..."""
    line = [" "] * (across + 4)
    n = 1
    while n <= most:
        line[int(at(n) * across): int(at(n) * across) + len(str(n))] = str(n)
        n *= 2
    return "".join(line).rstrip()


def cloud_most(words: pl.DataFrame):
    """The count both clouds' scales run to, so their labels sit one above the other: the most times any word has
    come up, which no word's clean typings can pass."""
    return words.filter(pl.col("done")).group_by("word").len()["len"].max()


def cloud(g: pl.DataFrame, hi, most, spread=None):
    """Rows of (word, n, value) as dots: across, n up to `most` (each doubling the same step); up, the value, from 0 to `hi`.
    A word with count n is set at random between n and n + 1, so the low counts fill a band rather than stack in
    a line; up, with a `spread`, it is set off its value by a normal draw of deviation `spread(n)`, folded back at
    0 and `hi`. The words with the highest counts are placed first. A word whose dot is taken: without a `spread`
    it takes, at random, one of the dots still free at its value between n and n + 1, so it shares only when there
    is none; with one it is drawn again, across and up, CLOUD_TRIES times at most, and then shares its first.
    Returns the plot and how many dots are lit."""
    import math
    import random
    step, apart = CLOUD
    across = columns() - 12                         # the box fills the terminal: its edges, its padding and the scale take 12
    down = hi // step * apart + 1                   # a line for the top label, then `apart` lines to each label below it
    wide = across * 2                               # two dots across a character, four down
    span = math.log2(most + 1)
    at = lambda n: math.log2(n) / span                     # where a count sits, 0 at the left edge to 1
    lit = set()
    for word, n, value in g.sort(["n", "word"], descending=[True, False]).iter_rows():
        rng = random.Random(word)
        first = None
        for _ in range(CLOUD_TRIES if spread else 1):
            x, v = int(at(n + rng.random()) * wide), value
            if spread:
                v = abs(v + rng.gauss(0, spread(n))) % (2 * hi)
                v = min(v, 2 * hi - v)
            dot = (min(x, wide - 1), round((hi - min(v, hi)) / step * apart * 4 + 3))      # a labelled value is the bottom of its line
            first = first or dot
            if dot not in lit:
                break
        else:
            free = [] if spread else [(x, dot[1]) for x in range(int(at(n) * wide), min(int(at(n + 1) * wide), wide - 1) + 1)
                                      if (x, dot[1]) not in lit]
            dot = rng.choice(free) if free else first
        lit.add(dot)
    labels = {(hi - v) // step * apart: v for v in range(0, hi + 1, step)}
    lines = []
    for r in range(down):
        cells = (sum(DOTS[dx * 4 + dy] for dx in (0, 1) for dy in range(4) if (2 * c + dx, 4 * r + dy) in lit) for c in range(across))
        lines.append((f"{labels[r]:>4} │" if r in labels else "     │") + "".join(chr(0x2800 + b) if b else " " for b in cells))
    return "\n".join(lines + ["     └" + "─" * across, "      " + count_ticks(at, most, across)]), len(lit)


def cloud_box(words: pl.DataFrame):
    """Every word as a dot: across, how many times it has been typed clean (not as a drill's first word, which
    has no time); up, the wpm its keys were typed at
    (five keys to a word, its own space counted; the median of its clean typings)."""
    import math
    g = (words.filter(CLEAN_TIMED)
         .with_columns((pl.col("ms") / (pl.col("letters") + pl.col("has_sep").cast(pl.Int64))).alias("ms_per_key"))
         .group_by("word").agg(pl.len().alias("n"), (12000 / pl.col("ms_per_key").median()).alias("wpm")))    # 60 000 ms over 5 keys
    if g.height < 100:
        return None
    hi = math.ceil(g["wpm"].quantile(0.998) / CLOUD[0]) * CLOUD[0]     # the fastest words are the few typed most: all but a stray one fit
    body, dots = cloud(g, hi, cloud_most(words))
    return (f"{g.height} words on {dots} dots, the rest found none free · times typed clean against wpm over the word's own keys", body)


def clean_cloud_box(words: pl.DataFrame):
    """Every word as a dot: across, how many times it came up and was typed to its end or left wrong (a drill's
    first word too); up, the share of those times no wrong key landed in it. Backspaces do not count against it. A word seen n times can only have a
    share that is a multiple of 100/n, so it is spread on a bell curve: 25 points wide for a word seen once, where the share says little and the
    words pile up, and narrowing with the square of the sightings, so the well-seen words keep to their lines."""
    g = words.filter(pl.col("done")).group_by("word").agg(pl.len().alias("n"), ((~pl.col("slip")).mean() * 100).alias("clean"))
    if g.height < 100:
        return None
    body, dots = cloud(g, 100, cloud_most(words), lambda n: CLOUD_SD / (n + 1) ** 2)
    return (f"{g.height} words on {dots} dots · times seen (n) against % with no wrong key · "
            f"spread {CLOUD_SD}/(n+1)² points · up to {CLOUD_TRIES} tries for a free dot", body)


def full_view(a, con, drills):
    lit = drills.filter(pl.col("mode") == "literature").height
    drills = drills.filter(pl.col("mode") != "literature")   # another language: its words and pace do not pool with the lists
    judged = drills.filter(pl.col("outcome").is_in(["failed", "completed"]) & (pl.col("mode") != "practice"))
    if judged.height < 2:
        print("\nnot enough drills yet\n")
        return
    per = {}
    for mode, source in exercises(drills):
        d = exercise_drills(drills, mode, source)
        delay, gaps = gaps_of(con, id_list(d))
        per[f"{mode} · {source}"] = dict(mode=mode, d=d, gaps=gaps)
    per = dict(sorted(per.items(), key=lambda kv: -kv[1]["d"]["secs"].fill_null(0).sum()))

    ids = id_list(judged)
    delay, _ = gaps_of(con, ids)
    sub = dict(drills=ids, hold=HOLD_KEYS, delay=repr(delay))
    of = pl.DataFrame([dict(mode=e["mode"], source=e["d"]["source"][0], ex=name, S=e["gaps"]["start"], L=e["gaps"]["letter"],
                            E=e["gaps"]["space"]) for name, e in per.items()])
    words = (run(con, WORDS, **sub, bare=BARE.format("wtext"))
             .join(judged.select(pl.col("id").alias("drill_id"), "mode", "source"), on="drill_id").join(of, on=["mode", "source"]))
    gaps_in = anchored(con, ids)

    days = drills["day"].n_unique()
    hours = drills["secs"].fill_null(0).sum() / 3600
    keys = drills["chars_typed"].sum()
    # words typed right: a sprint also marks done the words it moved past wrong; master and practice only count exact ones
    right = drills["words"].sum() - drills.filter(pl.col("mode") == "sprint")["words_wrong"].sum()
    title = (f"teepee · {days} day{'s' if days != 1 else ''} · {hours:.1f} h at the keys · "
             f"{drills.height} drills · {right:,} words · {keys:,} keys".replace(",", " ") + window(a)
             + (" · literature left out" if lit else ""))
    lead = (title, *exercise_table(per, drills.filter(pl.col("mode") == "practice")))

    parts = [
        lead,
        records(con, drills, words, id_list(drills)),
        ("slow words · everywhere", lambda n: slow_words(words, n),
         f"typed clean {MIN_WORD}+ times · ms slower than a word that long usually takes"),
        ("messy words · everywhere", lambda n: messy_words(words, n),
         f"{MIN_MESSY}+ attempts · messy = a wrong key landed in the word"),
        bigram_box(gaps_in),
        same_words(words, list(per)),
        keyboards_box(judged, gaps_in),
        *rhythm(drills),
        coverage_box(words, [e["d"]["source"][0] for e in per.values() if e["mode"] == "sprint"]),
        *(list_words(words.filter(pl.col("ex") == name), name) for name, e in per.items() if e["mode"] == "sprint"),
        journal_box(con),
        cloud_box(words),
        clean_cloud_box(words),
    ]
    print("\n" + soft(grid([], [p for p in parts if p])) + "\n")


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
        full_view(a, con, drills)
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
    ap.add_argument("exercise", nargs="?", choices=sorted(MODES), help="this mode's exercise in depth (default: the one box)")
    ap.add_argument("--list", help="word list for sprint/master (default: the one last played)")
    ap.add_argument("--book", help="book for lit (default: the one last played)")
    ap.add_argument("--full", action="store_true", help="the whole record: exercises compared, and what holds across them")
    ap.add_argument("--days", type=int, default=None, help="look back N days (default: all)")
    ap.add_argument("--tag")
    ap.add_argument("--bars", type=int, default=BARS, help=f"most bars in the wpm diagram, in the same width (default: {BARS}, at most {WIDTH})")
    ap.add_argument("--open", action="store_true", help="also draw the master progress graph and open it in the image viewer")
    a = ap.parse_args()
    if not 1 <= a.bars <= WIDTH:
        ap.error(f"--bars must be between 1 and {WIDTH}")
    report(a)


if __name__ == "__main__":
    main()
