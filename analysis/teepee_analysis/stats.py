"""teepee-stats: one report over the keystroke log, or a progress graph.

    teepee-stats                 the report: overview, recent master drills, missed words, slow words, slow trigrams
    teepee-stats graph           PNG of master-mode progress over drills, opened in the default viewer
    options: --tag X  only drills tagged X      --days N  look back N days (default 90)
             --min N  minimum occurrences for words and trigrams (default 5)
"""
import argparse
import subprocess
from pathlib import Path

import polars as pl

from .db import connect, data_dir

# ------------------------------------------------------------------ queries
# $days, $min, $tagfilter_d are substituted before running. The SQLite view
# drill_summary is not used because DuckDB's sqlite reader cannot parse it.

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
       k.chars_correct / 5.0 / nullif(k.span_ns, 0) * 60e9 AS wpm,
       k.span_ns / 1e9 AS secs
FROM tp.drills d LEFT JOIN k ON k.drill_id = d.id LEFT JOIN w ON w.drill_id = d.id
WHERE d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
ORDER BY d.id
"""

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
ORDER BY f.failed DESC, fail_pct DESC, f.word LIMIT 12
"""

WORDS = """
WITH first AS (
  SELECT drill_id, target_idx, min(t_ns) AS t_first FROM tp.keystrokes WHERE kind = 'char' GROUP BY 1, 2
)
SELECT t.text AS word, count(*) AS n,
       round(median((t.completed_at_ns - f.t_first) / 1e6 / length(t.text)), 0) AS ms_per_char
FROM tp.targets t
JOIN first f ON f.drill_id = t.drill_id AND f.target_idx = t.idx
JOIN tp.drills d ON d.id = t.drill_id
WHERE t.kind = 'word' AND t.completed_at_ns IS NOT NULL AND d.mode <> 'practice' AND d.outcome <> 'afk'
  AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d
GROUP BY 1 HAVING n >= $min ORDER BY ms_per_char DESC LIMIT 12
"""

TRIGRAMS = """
WITH k AS (
  SELECT drill_id, seq, typed, kind, correct, t_ns,
         lag(typed, 2) OVER w AS c1, lag(typed, 1) OVER w AS c2,
         lag(kind, 2) OVER w AS k1, lag(kind, 1) OVER w AS k2,
         lag(correct, 2) OVER w AS ok1, lag(correct, 1) OVER w AS ok2,
         t_ns - lag(t_ns, 1) OVER w AS dt
  FROM tp.keystrokes
  WHERE drill_id IN (SELECT id FROM tp.drills d WHERE coalesce(d.outcome, '') <> 'afk'
                     AND d.started_at_ms >= epoch_ms(now() - INTERVAL ($days) DAY) $tagfilter_d)
  WINDOW w AS (PARTITION BY drill_id ORDER BY seq)
)
SELECT replace(c1 || c2 || typed, ' ', '␣') AS trigram, count(*) AS n,
       round(median(dt) / 1e6, 0) AS median_ms
FROM k
WHERE kind = 'char' AND correct = 1 AND k1 = 'char' AND k2 = 'char' AND ok1 = 1 AND ok2 = 1
GROUP BY 1 HAVING n >= $min ORDER BY median_ms DESC LIMIT 12
"""


def frame(con, sql, a):
    tag = a.tag.replace("'", "''") if a.tag else None
    sql = (
        sql.replace("$days", str(int(a.days)))
        .replace("$min", str(int(a.min)))
        .replace("$tagfilter_d", f"AND list_contains(from_json(d.tags, '[\"VARCHAR\"]'), '{tag}')" if tag else "")
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


def report(a):
    con = connect()
    drills = frame(con, PER_DRILL, a)
    if drills.is_empty():
        print("no drills yet")
        return
    judged = drills.filter(pl.col("outcome").is_in(["failed", "completed"]))
    minutes = judged["secs"].fill_null(0).sum() / 60
    section(f"overview · last {a.days} days" + (f" · tag {a.tag}" if a.tag else ""))
    print(f"  drills {drills.height}   master {(drills['mode'] == 'master').sum()}   practice {(drills['mode'] == 'practice').sum()}"
          f"   literature {(drills['mode'] == 'literature').sum()}   afk {(drills['outcome'] == 'afk').sum()}   aborted {(drills['outcome'] == 'aborted').sum()}")
    print(f"  time typing {minutes:.0f} min   keys {drills['chars_typed'].sum()}   correct {drills['chars_correct'].sum()}"
          f"   words done {drills['words'].sum()}")

    section("master mode, most recent drills · survival = words completed / words attempted")
    show(master_recent(drills))

    section("missed words · seen = times reached, failed = drills that ended on it")
    show(frame(con, MISSED, a))

    section(f"slowest words · ms per character, at least {a.min} completions, master only")
    show(frame(con, WORDS, a))

    section(f"slowest trigrams · median gap before the third key, at least {a.min} occurrences")
    show(frame(con, TRIGRAMS, a))
    print()


# ------------------------------------------------------------------ graph

def graph(a):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    con = connect()
    drills = frame(con, PER_DRILL, a)
    m = drills.filter((pl.col("mode") == "master") & pl.col("outcome").is_in(["failed", "completed"])).sort("id")
    if m.height < 2:
        print("need at least two master drills")
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

    out = Path(data_dir()) / "progress.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor=S)
    print(out)
    subprocess.run(["open", str(out)], check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs="?", choices=["report", "graph"], default="report")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min", type=int, default=5)
    ap.add_argument("--tag")
    a = ap.parse_args()
    graph(a) if a.what == "graph" else report(a)


if __name__ == "__main__":
    main()
