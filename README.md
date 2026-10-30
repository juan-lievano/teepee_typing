# teepee_typing

Terminal typing trainer that records every keystroke to SQLite. The typing
app is a Rust binary (`teepee`); the stats are a small Python package over
the same database file.

## Use

```sh
teepee                       # master mode: 50 random words from english 10k
teepee master --words 30     # shorter drills
teepee master --list 1k      # english 200 / 1k / 5k / 10k (default)
teepee master --keep-case    # keep "February", "I"; default lowercases like Monkeytype
teepee lit pride             # a book, one sentence at a time (expert strictness)
teepee lit pride --strict lenient
teepee --tag voyager         # tag every drill from now on (sticky); --tag none clears
teepee tags                  # current tags
teepee retag laptop --last 2h        # fix the tags on the last two hours of drills
teepee retag none --ids 120-140 --dry-run
teepee books                 # books you have prepared
teepee data                  # where the database lives
teepee backup                # snapshot to ~/Documents/teepee_typing_backups (also on every exit)
teepee reset                 # delete the database (asks first); books are kept
```

Tags mark things the data can't know on its own, like which keyboard you
used. A tag set with `--tag` stays on for every later launch until you set a
different one or `--tag none`; the header always shows the current state.
`retag` rewrites past drills picked by `--last 2h` and/or `--ids 12-30`.
Stats take `--tag` to filter.

Esc quits. The first wrong key in master mode ends the drill; the missed word
is then practised, repeated until about 50 characters of it have been typed
(`PRACTICE_CHARS` in `src/modes/practice.rs`), and a fresh drill starts, with
no menu in between.
A pause longer than `--afk` seconds (default 3) between two keys voids the
drill: it is saved with outcome `afk`, excluded from stats, and the mode
restarts. Waiting before the first key costs nothing.
Keys that arrive in the first moments after a failure, before your hands have
been still for 400 ms, are ignored (and logged as `ignored`), so the tail of
the word you were typing cannot fail the practice drill.

The text sits in a padded navy box with an ice-blue block caret (colours
from the author's terminal palette, hardcoded in `src/runner.rs`); the rest
of the screen is your terminal.

## Modes and strictness

A **mode** decides what you type: master (random words), practice (one word
repeated), literature (a book's sentences in order). **Strictness** decides
how mistakes are judged, same three levels as Monkeytype:

| level   | behaviour |
|---------|-----------|
| lenient | wrong keys show in red, backspace fixes them |
| expert  | pressing space on a wrong or incomplete word fails the drill |
| master  | the first wrong key fails the drill |

The kernel (`src/runner.rs`) implements the strictness levels once; a mode
(`src/modes/*.rs`) implements the `Mode` trait in `src/mode.rs` and picks one.

## Data

`~/.local/share/teepee_typing/teepee.db` (override with `TEEPEE_DATA_DIR`).
Every exit writes a consistent snapshot to `~/Documents/teepee_typing_backups/teepee-YYYY-MM-DD.db`
(one per day, overwritten), where iCloud picks it up; `teepee backup` does it on demand.
The live database is not synced on purpose: a WAL database mid-write is not safe to sync.

| table        | one row per | notes |
|--------------|-------------|-------|
| `drills`     | drill       | mode, parent drill (for spawned practice), wall-clock start, outcome (completed / failed / afk / aborted), failed word, mode config as JSON, tags as JSON array |
| `targets`    | word or sentence reached | `shown_at_ns` and `completed_at_ns` since drill start, `source_ref` like `english_10k#412` or `pride#1037`; words after a failure get no row |
| `keystrokes` | key press   | `t_ns` since drill start, target index, position, expected char, typed char, kind (`char`, `backspace`, `ignored`, `modified` for ctrl/alt combos, `other` for enter/tab/arrows), correct |

`drill_summary` is a view deriving WPM and accuracy from keystrokes.
Timestamps are taken on a dedicated input thread the moment a key is read.

## Stats

```sh
cd analysis && python3 -m venv .venv && .venv/bin/pip install .   # re-run after editing (editable installs are skipped by Python 3.15)
.venv/bin/teepee-stats                  # the report: graph inline (wpm per drill, per-word survival over the last 100), then
                                        # overview, last 10/50/100 master drills, missed words, slow words, slow trigrams
.venv/bin/teepee-stats --open           # graph in the image viewer instead;  --no-graph for tables only
.venv/bin/teepee-stats --tag voyager --days 7
.venv/bin/teepee-book 1342 --name pride     # Pride and Prejudice from Gutenberg
```

The report's progress measure for master mode is per-word survival: words
completed divided by words attempted. It predicts finishing odds directly
(survival to the 50th power for a 50-word drill) and doesn't depend on one
lucky long run.

## Build

```sh
cargo install --path .
```

Word list: Monkeytype's `english_10k` (see `assets/README.md`).
