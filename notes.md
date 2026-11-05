# Notes

## 2026-11-04 · finding weak points in english 200 sprint

Everything below is from the 129 completed english 200 sprint drills typed
between 2026-10-31 and 2026-11-04 (about 30 attempts per word), unless said
otherwise. Numbers will drift; the methods are the point.

### Scoring

- wpm is now Monkeytype's: characters of words that ended up right, the space
  after each included, / 5 / minutes. A wrong word is worth nothing. Was: every
  accepted key, so correct letters inside wrong words counted. Changed in
  `drill_summary` (`src/store.rs`), `PER_DRILL` (`stats.py`) and the live
  banner (`live_scored` in `src/runner.rs`). Keystrokes are the record, wpm
  is derived, so every old drill was rescored.
- Accuracy is per keystroke (Monkeytype's too). It hides wrong words: 5 wrong
  words in 50 cost 8 keys of 239 and read as 96.7%. `words_wrong` is the
  honest companion; it is in the view and the sprint table.
- Typical gap between the two wpm definitions: 0 on a clean drill, 5–9 on a
  mangled one (drill 3651: 103.7 → 94.7).

### Where the time is (one 50-word drill, ~270 keys, ~32 s)

| lever                                                    | at stake            | ≈ wpm |
|----------------------------------------------------------|---------------------|-------|
| the 15 slowest words, excess over the mean erased        | ~1.0 s              | 3     |
| the slowest 50 words 20% faster (they hold 33% of time)  | 6.5% of time        | 6     |
| 2.5 wrong words per drill, each worth zero               | ~15 of 270 chars    | 5     |
| word starts at in-word pace (152 ms → 113 ms, 50×)       | ~2 s                | 6     |
| every word 10% faster                                    | 3.2 s               | 9     |

Every english 200 word comes up equally often (once per four drills), so a
word's priority is its excess time, not its rank per character.

### The pace is uniform

| grain (english 200 sprint)   | items | mean ms | p10 | p90 | p90/p10 | CV   |
|------------------------------|-------|---------|-----|-----|---------|------|
| words, ms per char           | 200   | 126     | 104 | 150 | 1.44    | 0.15 |
| keys, ms per gap             | 24    | 119     | 98  | 139 | 1.42    | 0.12 |
| bigrams                      | 196   | 118     | 96  | 143 | 1.49    | 0.15 |
| trigrams                     | 596   | 118     | 98  | 141 | 1.44    | 0.14 |
| words, clean median per char | 200   |         | 93  | 127 |         |      |

No fat tail at any grain: the worst item is ~1.6× the median. 120 wpm needs
~100 ms per character against a mean of 126, so it is a general-speed
problem; targeted drilling adds a few points on top.

Structure that does exist:

- slow keys are the reaches off the home row: `j l b p c f m`; fast are
  `n h t s d`. Slow bigrams leave home for those keys: `pa fa bl po fo be`.
- the gap into a word's first letter is 152 ms against 113 inside a word and
  93 into the space, the same for every word length: a per-word cost, paid
  50 times a drill. Slow starts: `␣y`, `␣k`, `␣pa`, words starting in `o`
  (`or` 204, `so` 174, `of` 164 vs `he` 120, `do` 123).

### Measuring a word (the `a` lesson)

A word is clocked from the previous word's space to its own, so `a` is two
gaps over two characters. Of 28 clean-or-not attempts, 25 took ~230 ms; three
had a mistake (`l⌫a`, `f⌫a`, a double space) and took 1.2–2.0 s. Mean 460,
median 230. The mean of all attempts ranks error rate disguised as slowness.

Three rankings, three different lists, all worth practising, and a word on
two of them is the priority:

| mean of all attempts (the `--full` table's rule) | median of clean attempts                       | messy rate (wrong key or backspace on the word)       |
|--------------------------------------------------|------------------------------------------------|-------------------------------------------------------|
| write a before problem public by eye govern …    | between eye govern follow look through by …    | world write present which head through consider …     |

`write`, `increase`, `public`, `problem`, `a` are slow only on average: they
are mangled 25–40% of the time. `between`, `eye`, `govern`, `follow`, `look`
are slow when clean. `govern` and `through` are on both.

Rules for the next version of these tables:

- median of clean attempts for pace; a separate messy-rate table; never the
  mean of everything.
- compare within one list and one mode. The `--full` tables mix master and
  sprint per language; that blurs exactly this reading.
- rank words by excess over what their length predicts, or split the
  word-start gap out as its own table; per character, short words hog the
  top because half their time is the start gap.
- ~30 attempts per word is noisy; shrink toward the mean, or regression to
  the mean will churn any rolling list.
- sprint measures, practice treats. Times from practice mode (a word repeated)
  say nothing about typing it in a sentence and must not feed a ranking.

### If a "weakest points" drill gets built

A rolling list, the slowest ~50 words by the rules above plus the messy ones,
rebuilt from the database at launch and typed as words (not n-grams): keeps
sweeping the 200 from the slow end up, and leaves when a word is fast in
real sprints. Keep sprinting the full list as the measurement.

### Housekeeping

The installed `teepee` is a compiled copy: after editing `src/`, run
`cargo install --path .`; after editing `analysis/`, `pip install .` in its
venv. It had been stale for four days before today.

Tap-hold delay (kanata home row + space) estimated at 62 ms from these
drills; the gap numbers above are corrected with it, as `stats.py` does.
