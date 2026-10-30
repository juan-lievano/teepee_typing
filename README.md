# teepee_typing

Terminal typing trainer that records every keystroke to SQLite. The typing
app is a Rust binary (`teepee`); the stats are a small Python package over
the same database file.

## Use

```sh
teepee                       # master mode: 50 random words from english 10k
teepee master --words 30     # shorter drills
teepee lit pride             # a book, one sentence at a time (expert strictness)
teepee lit pride --strict lenient
teepee --tag voyager         # tag every drill from now on (sticky); --tag none clears
teepee tags                  # current tags
teepee retag laptop --last 2h        # fix the tags on the last two hours of drills
teepee retag none --ids 120-140 --dry-run
teepee books                 # books you have prepared
teepee data                  # where the database lives
```

Tags mark things the data can't know on its own, like which keyboard you
used. A tag set with `--tag` stays on for every later launch until you set a
different one or `--tag none`; the header always shows the current state.
`retag` rewrites past drills picked by `--last 2h` and/or `--ids 12-30`.
Stats take `--tag` to filter.

Esc quits. The first wrong key in master mode ends the drill; the missed word
is then typed ten times and a fresh drill starts, with no menu in between.

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

| table        | one row per | notes |
|--------------|-------------|-------|
| `drills`     | drill       | mode, parent drill (for spawned practice), wall-clock start, outcome, failed word, mode config as JSON, tags as JSON array |
| `targets`    | word or sentence asked for | `shown_at_ns` and `completed_at_ns` since drill start, `source_ref` like `english_10k#412` or `pride#1037` |
| `keystrokes` | key press   | `t_ns` since drill start, target index, position, expected char, typed char, kind (`char`/`backspace`), correct |

`drill_summary` is a view deriving WPM and accuracy from keystrokes.
Timestamps are taken on a dedicated input thread the moment a key is read.

## Stats

```sh
cd analysis && python3 -m venv .venv && .venv/bin/pip install .   # re-run after editing (editable installs are skipped by Python 3.15)
.venv/bin/teepee-stats summary
.venv/bin/teepee-stats missed
.venv/bin/teepee-stats trigrams --min 10
.venv/bin/teepee-stats missed --tag voyager
.venv/bin/teepee-book 1342 --name pride     # Pride and Prejudice from Gutenberg
```

## Build

```sh
cargo install --path .
```

Word list: Monkeytype's `english_10k` (see `assets/README.md`).
