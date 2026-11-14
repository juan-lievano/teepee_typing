# teepee_typing

Terminal typing trainer that records every keystroke to SQLite. The typing
app is a Rust binary (`teepee`); the stats are a small Python package over
the same database file.

`CHEATSHEET.md` lists every command, flag and key in a line each, and is the
page to keep current: a change to a command updates it in the same commit.
That matters more than keeping this README current; the README explains how
things work and can lag, the cheat sheet is what gets read before typing.

## Use

```sh
teepee                       # master mode with the settings of the last master drill
                             # (at first: 50 random words from english 10k)
teepee master --words 30     # shorter drills
teepee master --list 1k      # english 200 / 1k / 5k / 10k
teepee master --keep-case    # keep "February", "I"; --keep-case=false lowercases again, like Monkeytype
teepee sprint                # random words at normal difficulty: wrong keys are let through, nothing
                             # is practised; for personal bests (at first: 50 words from english 200)
teepee sprint --list 1k --words 25
teepee lit                   # the book last typed (with none yet, pick from the list);
                             # or teepee lit quijote / qui / 3
                             # (a book, one sentence at a time; wrong words are let through,
                             # practised after the sentence, and the sentence is repeated if
                             # its accuracy was under 95%)
teepee --tag voyager         # tag every drill from now on (sticky); --tag none clears
teepee tags                  # current tags
teepee retag laptop --last 2h        # fix the tags on the last two hours of drills
teepee retag none --ids 120-140 --dry-run
teepee books                 # books you have prepared
teepee data                  # where the database lives (data/ in the repo)
teepee reset                 # delete the database (asks first); books are kept
```

A setting left off the command line is the one the mode's last drill had:
after `teepee sprint --list 1k`, plain `teepee sprint` is on 1k until another
`--list`. Nothing is stored for this; the last drill's row in the record is
read (`Store::last_config`), each setting on its own, with the built-in value
where the record has none.

Tags mark things the data can't know on its own, like which keyboard you
used. A tag set with `--tag` stays on for every later launch until you set a
different one or `--tag none`; the header always shows the current state.
`retag` rewrites past drills picked by `--last 2h` and/or `--ids 12-30`.
Stats take `--tag` to filter.

Esc quits; a drill quit before its first letter is typed is not kept. The first wrong key in master mode ends the drill; the missed word
is then practised, repeated until about 25 keystrokes of it have been typed
(an accented letter counts as three: dead key, vowel, and a slow chord)
but at most 10 times (`PRACTICE_CHARS`, `MAX_REPS` in `src/modes/practice.rs`),
and a fresh drill starts, with no menu in between.
A pause longer than `--afk` seconds (default 3) between two keys voids the
drill: it is saved with outcome `afk`, excluded from stats, and the mode
restarts. Waiting before the first key costs nothing.
Keys that arrive in the first moments after a failure, before your hands have
been still for 400 ms, are ignored (and logged as `ignored`), so the tail of
the word you were typing cannot fail the practice drill.

The text sits in a padded navy box (colours from the author's terminal
palette, hardcoded in `src/runner.rs`); the rest of the screen is your
terminal. There is no caret while typing: typed text is bright, untyped text faint, and
the first faint letter is the next one to type. Deleting brings up a
block caret there, until the next character typed. The one ambiguous spot, the
space after a finished word, shows as a faint dot once you have paused on it
for half a second.

## Modes and strictness

A **mode** decides what you type: master (random words), sprint (random words,
nothing is practised), practice (one word
repeated), literature (a book's sentences in order: after each sentence every
word that ended up wrong is practised (a word fixed before its space, or by
going back to it, is fine), then the sentence is repeated if its accuracy,
keys accepted over keys typed, was below 95% (`MIN_ACCURACY` in
`src/modes/literature.rs`), else the book moves on). **Strictness** decides how mistakes are judged, Monkeytype's
"normal" and "master":

| level   | behaviour |
|---------|-----------|
| lenient | wrong keys show in red; space moves on to the next word, right or wrong, and a missed word stays on screen as typed, in red, with letters never typed left faint. Backspace at the start of a word goes back into the word before it (in sprint, the previous word target, which is complete again once resubmitted), as typed, to correct it; a word is judged on what it ends up as. Alt/Ctrl+Backspace and Ctrl+W delete the word; at the start of a word, the one before it. The mode gets the missed words and each sentence's accuracy when the drill ends |
| master  | the first wrong key fails the drill |

The kernel (`src/runner.rs`) implements both levels once; a mode
(`src/modes/*.rs`) implements the `Mode` trait in `src/mode.rs` and picks one.

## Data

`data/teepee.db` in the repo checkout (gitignored; `teepee data` prints the
path, `TEEPEE_DATA_DIR` overrides it). Every keystroke is written as it
happens, in single-file journal mode, so the file is always consistent and
whatever syncs the checkout syncs the data. At the first launch of a new month
teepee copies the database as it ended the previous month to
`data/snapshots/teepee-YYYY-MM.db`; those are kept forever.

| table        | one row per | notes |
|--------------|-------------|-------|
| `drills`     | drill       | `git_commit` the binary was built from (`+dirty` if the code differed from it, and then every drill's header says so; empty for drills before 2026-11-14), mode, parent drill (for spawned practice), wall-clock start, outcome (completed / failed / afk / aborted), failed word, mode config as JSON, tags as JSON array |
| `sources`    | word list or book | `name` is the part of a `source_ref` before `#`; kind (`wordlist`/`book`), `lang` (`en`, `es`), title, origin. Word lists are seeded; a book is registered by `teepee lit` from its `.json` sidecar |
| `targets`    | word or sentence reached | `shown_at_ns` and `completed_at_ns` since drill start, `source_ref` like `english_10k#412` or `quijote#1037` (a practice word keeps the ref of the target it was missed in); words after a failure get no row |
| `keystrokes` | key press   | `t_ns` since drill start, target index, position, expected char, typed char, kind (`char`, `backspace` with typed `word` for a word delete, `ignored`, `modified` for ctrl/alt combos, `other` for enter/tab/arrows), correct (the key was accepted as progress; the key that ends a failed drill is always 0) |

`drill_summary` is a view deriving WPM and accuracy from keystrokes, with
Monkeytype's definitions: accuracy counts every character keystroke; WPM
counts the characters of the words that ended up right, the space after each
included, and nothing for a word that ended up wrong (a keystroke stands until
a later key lands at or before its position, and a word is right when no wrong
key stands in it). `words_wrong` is the count of the others. Both
are NULL for drills under 10 character keystrokes (`MIN_CHARS` in
`src/store.rs`, same number in `teepee-stats`), since two keys 4 ms apart is
not a speed. Short drills keep their keystrokes and outcome.

`config` holds only what defines a drill: `list`, `words`, `keep_case` for
master; `list`, `words` for sprint; `word`, `reps` for practice; `book`, `start`, `min_accuracy` for
literature. Keys are added, never renamed; a reader uses `json_extract`, which
gives NULL for a key an older drill does not have. Drills 2177–2322 were
literature under an earlier rule (expert strictness, several sentences per
drill) and say so in their config.
Timestamps are taken on a dedicated input thread the moment a key is read.

## Stats

```sh
teepee stats                 # one box: the exercise you last played (a mode on a list or a book),
                             # wpm per drill (40 groups once past 40), oldest left, all time
teepee stats sprint          # the long view of one exercise, every table on its drills alone: trend by
teepee stats master --list english_10k      # day, the three gaps (into a word, letter to letter, into
teepee stats lit --book quijote             # the space), slow words (clean attempts, excess over what
                             # the gaps predict for the length), messy words (wrong key or backspace;
                             # for master, drill-ending fails), word starts by first letter, slow keys /
                             # bigrams / trigrams (medians, lowercase letters, 30+ occurrences)
teepee stats --full          # the long view of every exercise played
teepee stats --open          # also the master progress graph (wpm per drill, per-word survival over the
                             # last 100) as a PNG in the image viewer
teepee stats --tag voyager --days 7
teepee journal               # the practice wall: a column per week, a cell per day, a dot in it for every
                             # 2 minutes typed (first key to last of every drill, practice reps included)
teepee book 2000 --name quijote --lang es --from "En un lugar de la Mancha"
                             # a Gutenberg text into data/books/<name>.txt (one target per
                             # line: a clause of prose, split at . ! ? , ; : so lines are
                             # verse-sized, or with --verse one verse of poetry) plus
                             # <name>.json (language, title); --from skips the front matter,
                             # --to stops before a phrase
```

Books prepared so far: `dante` (Longfellow's verse translation, Gutenberg 1004,
one verse per line), `iliad` (Samuel Butler's prose translation, 2199),
`pride` (1342), `quijote` (Spanish original, 2000; accented letters
and ¿¡ are kept, typed as the dead-key compositions the terminal sends).
Progress in a book counts from the file's mtime, so re-preparing it starts
over. Every table of the long view is on one exercise's drills alone, so a
word's time is never averaged across modes or lists, and pace tables use the
median of clean attempts: the mean of all attempts ranks error rate disguised
as slowness (see `notes.md`).

Timing caveat: kanata's tap-hold keys (the Colemak-DH home row a r s t n e i o
and the space layer-tap) are reported when the tap is decided, up to a hold
time after the physical press, so a gap ending on one reads too long and a
gap starting on one too short. Words are therefore clocked space to space,
where the delay cancels exactly, and keys, bigrams and trigrams are clocked
from the key before them to the key after them, so their own delays cancel
too; only the two context keys at the window ends get the mean delay taken
out, estimated from the same keystrokes as half the asymmetry between
plain→hold and hold→plain gaps (about 60 to 80 ms). Medians after that
correction are sound; single gaps are not.

Both run the Python package in `analysis/`, set up once with:

```sh
cd analysis && python3 -m venv .venv && .venv/bin/pip install .   # re-run `pip install .` after editing
```

The progress measure for master mode is per-word survival: words completed
divided by words attempted. It predicts finishing odds directly (survival to
the 50th power for a 50-word drill) and doesn't depend on one lucky long run.

## Build

```sh
cargo install --path .
```

Word list: Monkeytype's `english_10k` (see `assets/README.md`).
