"""teepee-stats: one report over the keystroke log.

Tables: data size, overview, recent master drills, missed words, then slow
words and slow trigrams per language (from the `sources` table). `--open` also draws the progress graph (wpm per master drill,
rolling per-word survival) to a PNG and opens it in the image viewer.

    --tag X    only drills tagged X          --days N   look back N days (default 90)
    --min N    minimum occurrences for the word and trigram tables (default 5)
    --open     also show the progress graph in the image viewer
"""
import argparse
import subprocess
import sys
from pathlib import Path

import polars as pl

from .db import connect

# ------------------------------------------------------------------ queries
# $days, $min, $tagfilter_d are substituted before running. The SQLite view
# drill_summary is not used because DuckDB's sqlite reader cannot parse it.

# A drill with fewer character keystrokes than this gets no wpm (same number
# as MIN_CHARS in src/store.rs): a two-key drill lasting 4 ms is not a speed.
MIN_CHARS = 10

PER_DRILL = """
WITH k AS (
  SELECT drill_id,
         count(*) FILTER (WHERE kind = 'char')                 AS chars_typed,
         coalesce(sum(correct) FILTER (WHERE kind = 'char'), 0) AS chars_correct,
         max(t_ns) FILTER (WHERE kind = 'char') - min(t_ns) FILTER (WHERE kind = 'char') AS span_ns
  FROM tp.keystrokes GROUP BY drill_id
),
w AS (
  SELECT drill_id, count(*) AS words FROM tp.targets WHERE completed_at_ns IS NOT NULL GROUP BY drill_id
)
SELECT d.id, d.mode, d.outcome, d.started_at_ms, d.failed_word,
       strftime(to_timestamp(d.started_at_ms / 1000), '%Y-%m-%d') AS day,
       coalesce(w.words, 0) AS words, coalesce(k.chars_typed, 0) AS chars_typed, coalesce(k.chars_correct, 0) AS chars_correct,
       CASE WHEN k.chars_typed >= $min_chars THEN k.chars_correct / 5.0 / nullif(k.span_ns, 0) * 60e9 END AS wpm,
       k.span_ns / 1e9 AS secs
FROM tp.drills d LEFT JOIN k ON k.drill_id = d.id LEFT JOIN w ON w.drill_id = d.id
WHERE d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
ORDER BY d.id
"""

# All time, no filters: the size of the record itself.
ALLTIME = """
SELECT (SELECT count(*) FROM tp.drills)                                   AS drills,
       (SELECT count(*) FROM tp.targets)                                  AS targets,
       (SELECT count(*) FROM tp.keystrokes)                               AS keys,
       (SELECT count(*) FROM tp.keystrokes WHERE kind = 'char')           AS chars,
       (SELECT count(DISTINCT strftime(to_timestamp(started_at_ms / 1000), '%Y-%m-%d')) FROM tp.drills) AS days,
       (SELECT min(started_at_ms) FROM tp.drills)                         AS first_ms,
       (SELECT sum(span) FROM (SELECT max(t_ns) - min(t_ns) AS span FROM tp.keystrokes GROUP BY drill_id)) AS typing_ns
"""

# A word is what sits between spaces, so a sentence's "hacienda." or
# "carnero," is stored and practised with its punctuation (the transition
# into the period is real typing). The word tables strip punctuation from
# the edges so those count with "hacienda" and "carnero"; trigrams keep it.
BARE = "regexp_replace({}, '^[^\\p{{L}}\\p{{N}}]+|[^\\p{{L}}\\p{{N}}]+$', '', 'g')"

MISSED = """
WITH seen AS (
  SELECT $bare_t AS word, count(*) AS seen
  FROM tp.targets t JOIN tp.drills d ON d.id = t.drill_id
  WHERE t.kind = 'word' AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
  GROUP BY 1
),
failed AS (
  SELECT $bare_f AS word, count(*) AS failed
  FROM tp.drills d
  WHERE failed_word IS NOT NULL AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
  GROUP BY 1
)
SELECT f.word, coalesce(s.seen, 0) AS seen, f.failed,
       round(100.0 * f.failed / greatest(coalesce(s.seen, 0), 1), 0) AS fail_pct
FROM failed f LEFT JOIN seen s ON s.word = f.word
ORDER BY f.failed DESC, fail_pct DESC, f.word LIMIT 12
"""

# Languages typed in the window: every target carries a source ref whose
# prefix names a row in `sources`; '?' when the source was never registered.
LANGS = """
SELECT coalesce(s.lang, '?') AS lang, count(*) AS targets
FROM tp.targets t JOIN tp.drills d ON d.id = t.drill_id
LEFT JOIN tp.sources s ON s.name = split_part(t.source_ref, '#', 1)
WHERE d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
GROUP BY 1 ORDER BY 2 DESC
"""

WORDS = """
-- A word is clocked from the space that ended the previous word to its own
-- space. Both ends are the same key, so kanata's tap-hold reporting delay
-- cancels exactly; the first word of a drill has no such start and is skipped.
SELECT $bare_t AS word, count(*) AS n,
       round(mean((t.completed_at_ns - p.completed_at_ns) / 1e6 / (length(t.text) + 1)), 0) AS ms_per_char
FROM tp.targets t
JOIN tp.targets p ON p.drill_id = t.drill_id AND p.idx = t.idx - 1 AND p.completed_at_ns IS NOT NULL
JOIN tp.drills d ON d.id = t.drill_id
LEFT JOIN tp.sources s ON s.name = split_part(t.source_ref, '#', 1)
WHERE t.kind = 'word' AND t.completed_at_ns IS NOT NULL AND d.mode <> 'practice' AND d.outcome <> 'afk'
  AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d $langfilter
GROUP BY 1 HAVING n >= $min ORDER BY ms_per_char DESC LIMIT 12
"""

# Keys kanata reports late: the Colemak-DH home-row mods and the space
# layer-tap. Their report comes when the tap is decided, up to a hold time
# after the physical press, so a gap ending on one reads too long by that
# tap's hold and a gap starting on one too short. The fix: clock an n-gram
# from the key BEFORE it to the key AFTER it, so the n-gram's own delays
# cancel, and take the mean delay (DELAY, estimated from the same keys as
# half the asymmetry between plain->hold and hold->plain gaps) off the two
# window ends. Sound as averages, not per keystroke.
HOLD_KEYS = "arstneio "

KEYS = """
WITH k AS (
  SELECT k.drill_id, k.seq, k.typed, k.kind, k.correct, k.t_ns
  FROM tp.keystrokes k
  JOIN tp.targets t ON t.drill_id = k.drill_id AND t.idx = k.target_idx
  LEFT JOIN tp.sources s ON s.name = split_part(t.source_ref, '#', 1)
  WHERE k.drill_id IN (SELECT id FROM tp.drills d WHERE coalesce(d.outcome, '') <> 'afk'
                       AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d)
    $langfilter
)
"""

DELAY = KEYS + """,
g AS (
  SELECT (k.t_ns - lag(k.t_ns) OVER w) / 1e6 AS dt,
         contains('$hold', lag(k.typed) OVER w) AS h1, contains('$hold', k.typed) AS h2,
         lag(k.kind) OVER w = 'char' AND lag(k.correct) OVER w = 1 AND k.kind = 'char' AND k.correct = 1 AS clean
  FROM k WINDOW w AS (PARTITION BY k.drill_id ORDER BY k.seq)
)
SELECT (mean(dt) FILTER (WHERE NOT h1 AND h2) - mean(dt) FILTER (WHERE h1 AND NOT h2)) / 2 AS delay_ms
FROM g WHERE clean AND dt < 1000
"""

NGRAMS = KEYS + """,
g AS (
  SELECT string_agg(k.typed, '') OVER (w ROWS BETWEEN $n - 1 PRECEDING AND CURRENT ROW) AS gram,
         bool_and(k.kind = 'char' AND k.correct = 1) OVER (w ROWS BETWEEN $n PRECEDING AND 1 FOLLOWING) AS clean,
         contains('$hold', lag(k.typed, $n) OVER w) AS hb, contains('$hold', lead(k.typed, 1) OVER w) AS ha,
         (lead(k.t_ns, 1) OVER w - lag(k.t_ns, $n) OVER w) / 1e6 AS span
  FROM k WINDOW w AS (PARTITION BY k.drill_id ORDER BY k.seq)
)
SELECT replace(gram, ' ', '␣') AS gram, count(*) AS n,
       round(mean((span - $delay * ha::INT + $delay * hb::INT) / ($n + 1)), 0) AS ms_per_gap
FROM g WHERE clean AND span IS NOT NULL AND span < 1000 * ($n + 1)
GROUP BY 1 HAVING n >= $min ORDER BY ms_per_gap DESC LIMIT $limit
"""


def frame(con, sql, a, lang=None, delay=0.0, n=1, limit=12):
    tag = a.tag.replace("'", "''") if a.tag else None
    sql = (
        sql.replace("$days", str(int(a.days)))
        .replace("$hold", HOLD_KEYS)
        .replace("$delay", repr(float(delay)))
        .replace("$n", str(int(n)))
        .replace("$limit", str(int(limit)))
        .replace("$min_chars", str(MIN_CHARS))
        .replace("$min", str(int(a.min)))
        .replace("$tagfilter_d", f"AND list_contains(from_json(d.tags, '[\"VARCHAR\"]'), '{tag}')" if tag else "")
        .replace("$langfilter", f"AND coalesce(s.lang, '?') = '{lang}'" if lang else "")
        .replace("$bare_t", BARE.format("t.text"))
        .replace("$bare_f", BARE.format("failed_word"))
    )
    rel = con.sql(sql)
    return pl.DataFrame(rel.fetchall(), schema=rel.columns, orient="row")


# ------------------------------------------------------------------ report

def section(title):
    print(f"\n{title}\n{'─' * len(title)}")


def show(df, **fmt):
    if df.is_empty():
        print("  (nothing yet)")
        return
    with pl.Config(tbl_rows=60, tbl_cols=20, tbl_width_chars=120, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, tbl_formatting="NOTHING", tbl_cell_alignment="RIGHT"):
        print(df)


WINDOW = 100  # drills in the rolling window for the graph

def master_recent(drills: pl.DataFrame) -> pl.DataFrame:
    """Master drills, most recent N for several N: how far you get, per-word survival, wpm."""
    m = drills.filter((pl.col("mode") == "master") & pl.col("outcome").is_in(["failed", "completed"])).sort("id")
    if m.is_empty():
        return m
    rows = []
    for n in [10, 50, 100, 500, m.height]:
        if n > m.height and n != m.height:
            continue
        r = m.tail(n)
        done = r["words"].sum(); fails = (r["outcome"] == "failed").sum()
        rows.append({
            "last": "all" if n == m.height else str(n),
            "drills": r.height,
            "finished": int((r["outcome"] == "completed").sum()),
            "median_words": r["words"].median(),
            "best": int(r["words"].max()),
            "survival_pct": round(100 * done / (done + fails), 1) if done + fails else None,
            "wpm": round(r["wpm"].drop_nulls().mean(), 1) if r["wpm"].drop_nulls().len() else None,
        })
    # dedupe when e.g. 10 == all
    seen, out = set(), []
    for row in rows:
        key = (row["drills"], row["last"] == "all")
        if row["drills"] in seen and row["last"] != "all":
            continue
        seen.add(row["drills"]); out.append(row)
    return pl.DataFrame(out)


def sprint_recent(drills: pl.DataFrame) -> pl.DataFrame:
    """Sprint drills, most recent N: best and typical wpm, accuracy."""
    m = drills.filter((pl.col("mode") == "sprint") & (pl.col("outcome") == "completed")).sort("id")
    if m.is_empty():
        return m
    rows = []
    for n in [10, 50, m.height]:
        if n > m.height:
            continue
        r = m.tail(n)
        rows.append({
            "last": "all" if n == m.height else str(n),
            "drills": r.height,
            "best_wpm": round(r["wpm"].max(), 1),
            "median_wpm": round(r["wpm"].median(), 1),
            "accuracy_pct": round(100 * r["chars_correct"].sum() / r["chars_typed"].sum(), 1),
        })
    return pl.DataFrame(rows).unique(subset=["drills"], keep="first", maintain_order=True)


def data_size(con):
    """How big the record is: the file on disk and what it holds, all time."""
    from .db import data_dir
    d = data_dir()
    db = d / "teepee.db"
    db_mb = db.stat().st_size / 1e6 if db.exists() else 0.0
    snaps = sorted((d / "snapshots").glob("*.db")) if (d / "snapshots").is_dir() else []
    snap_mb = sum(f.stat().st_size for f in snaps) / 1e6
    books = sorted((d / "books").glob("*.txt")) if (d / "books").is_dir() else []
    r = con.sql(ALLTIME).fetchall()[0]
    drills, targets, keys, chars, days, first_ms, typing_ns = r
    section("data · all time")
    print(f"  {db}  {db_mb:.1f} MB" + (f"   snapshots {len(snaps)} · {snap_mb:.1f} MB" if snaps else "   no snapshots yet")
          + (f"   books {len(books)}" if books else ""))
    if not keys:
        print("  empty")
        return
    import datetime as dt
    first = dt.datetime.fromtimestamp(first_ms / 1000).strftime("%Y-%m-%d")
    hours = (typing_ns or 0) / 3600e9
    print(f"  since {first} · {days} days typed · {drills} drills · {targets} targets · {keys} keys pressed"
          f" ({chars} characters) · {hours:.1f} h typing")
    print(f"  {db_mb * 1e6 / keys:.0f} bytes per key · {keys / db_mb / 1000:.1f} k keys per MB"
          f" · {keys / max(hours, 1e-9) / 60:.0f} keys per minute of typing")


def report(a):
    con = connect()
    data_size(con)
    drills = frame(con, PER_DRILL, a)
    if drills.is_empty():
        print("no drills yet")
        return
    if a.open:
        graph(a, drills)
    judged = drills.filter(pl.col("outcome").is_in(["failed", "completed"]))
    minutes = judged["secs"].fill_null(0).sum() / 60
    section(f"overview · last {a.days} days" + (f" · tag {a.tag}" if a.tag else ""))
    print(f"  drills {drills.height}   master {(drills['mode'] == 'master').sum()}   practice {(drills['mode'] == 'practice').sum()}"
          f"   literature {(drills['mode'] == 'literature').sum()}   sprint {(drills['mode'] == 'sprint').sum()}   afk {(drills['outcome'] == 'afk').sum()}   aborted {(drills['outcome'] == 'aborted').sum()}")
    print(f"  time typing {minutes:.0f} min   keys {drills['chars_typed'].sum()}   correct {drills['chars_correct'].sum()}"
          f"   words done {drills['words'].sum()}")

    section("master mode, most recent drills · survival = words completed / words attempted")
    show(master_recent(drills))

    section("sprint mode, most recent drills · normal difficulty, for personal bests")
    show(sprint_recent(drills))

    section("missed words · seen = times reached, failed = drills that ended on it")
    show(frame(con, MISSED, a))

    # Slow words and trigrams are a property of a language's letter patterns
    # (and of the keyboard's accents), so they are shown per language.
    langs = frame(con, LANGS, a)
    for lang in langs["lang"].to_list():
        section(f"slowest words · {lang} · ms per character, space to space, at least {a.min} completions, master and sprint")
        show(frame(con, WORDS, a, lang))
        delay = frame(con, DELAY, a, lang)["delay_ms"][0] or 0.0
        for n, what, limit in [(1, "keys", 40), (2, "bigrams", 12), (3, "trigrams", 12)]:
            section(f"slowest {what} · {lang} · ms per gap, clocked from the key before to the key after,"
                    f" at least {a.min} occurrences, tap-hold delay of {delay:.0f} ms taken out")
            show(frame(con, NGRAMS, a, lang, delay, n, limit))
    print()


# ------------------------------------------------------------------ graph

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
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min", type=int, default=5)
    ap.add_argument("--tag")
    ap.add_argument("--open", action="store_true", help="also draw the progress graph and open it in the image viewer")
    a = ap.parse_args()
    report(a)


if __name__ == "__main__":
    main()
