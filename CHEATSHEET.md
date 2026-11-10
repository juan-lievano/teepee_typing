# teepee

Typing trainer in the terminal. Every keystroke goes to one SQLite file;
WPM, misses and slow trigrams are queries over it later.
Repo: ~/Documents/Developing/teepee_typing. Data: `data/` inside it (gitignored, iCloud-synced).

## Typing

| Command                           | Does                                                              |
|-----------------------------------|-------------------------------------------------------------------|
| `teepee`                          | master mode: 50 random words from english 10k                     |
| `teepee master --words 30`        | shorter drills                                                    |
| `teepee master --list 1k`         | english 200 / 1k / 5k / 10k (default)                             |
| `teepee master --keep-case`       | keep capitals; default lowercases like Monkeytype                 |
| `teepee sprint`                   | 50 random words from english 200, for personal bests              |
| `teepee sprint --list 1k --words 25` | another list (200 / 1k / 5k / 10k), another length             |
| `teepee lit`                      | pick a book from a list                                           |
| `teepee lit pride`                | that book (a prefix like `pri` works), one line at a time         |
| `teepee --afk 5`                  | pause between keys that voids a drill (default 3 s)               |
| `Esc`                             | quit (the drill is saved as aborted)                              |

A pause over 3 s mid-drill voids it (outcome afk) and the mode restarts.
Waiting before the first key is free. Keys in the first moments after a
failure are ignored until your hands have been still for 0.4 s.

Master: the first wrong key ends the drill, the missed word comes back
repeated to ~25 keystrokes (10 times at most), then a fresh drill. No menu.
Sprint: wrong keys show red and are let through, space moves on, nothing is
practised. Backspace fixes, also back into the word before; Alt/Ctrl+Backspace
or Ctrl+W deletes the word. A fresh drill follows each one.
Lit: mistakes as in sprint; after each line the words left wrong are
practised, and the line repeats if under 95% of its keys were right.

## Tags (which keyboard)

A tag exists the moment you use it. It stays on until you change it.
The header always says `tags: voyager` or `no tags`.

| Command                              | Does                                            |
|--------------------------------------|-------------------------------------------------|
| `teepee --tag voyager`               | from now on every drill is tagged voyager       |
| `teepee --tag macbook`               | switch keyboard                                 |
| `teepee --tag none`                  | clear                                           |
| `teepee tags`                        | show the current tag                            |
| `teepee retag macbook --last 2h`     | fix the last two hours of drills                |
| `teepee retag none --ids 120-140`    | by drill id; `--dry-run` shows without writing  |

## Starting over

| Command                | Does                                                        |
|------------------------|-------------------------------------------------------------|
| `teepee reset`         | delete the database after you type `yes`; books are kept    |
| `teepee reset --yes`   | same, no question                                           |

Tags live in the database, so set the tag again after a reset.

## Books

| Command                                   | Does                                              |
|-------------------------------------------|---------------------------------------------------|
| `teepee books`                            | books ready to type                               |
| `teepee book 1342 --name pride`           | fetch a Gutenberg id, one clause per line         |
| `teepee book ./file.txt --name NAME`      | same from a local text                            |
| `... --from "It is a truth"`              | start there, skipping the front matter; `--to` stops before a phrase |
| `... --lang es`                           | the book's language (default en)                  |
| `... --verse`                             | poetry: one verse per line                        |

Progress is per book and resumes where you stopped.

## Stats

An exercise is a mode on one source: sprint or master on a word list, lit on a book.

| Command                          | Does                                                     |
|----------------------------------|----------------------------------------------------------|
| `teepee stats`                   | one box: the exercise you last played, wpm per drill oldest left (40 groups past 40 drills), median, best, trend per hour typing |
| `teepee stats sprint`            | the long view of one exercise: trend by day, the three gaps, slow words, messy words, word starts, slow keys / bigrams / trigrams |
| `teepee stats master --list english_10k` | same, on that list (default: the one last played) |
| `teepee stats lit --book quijote` | same, on that book                                      |
| `teepee stats --full`            | the long view of every exercise played                   |
| `teepee stats --open`            | also the master progress graph in the image viewer       |
| `teepee stats --tag voyager --days 7` | filter                                              |
| `teepee journal`                 | the practice wall: a cell per day shaded by minutes typed (first key to last of every drill, practice included) |

wpm is Monkeytype's: characters of words that ended up right, spaces included,
over five, per minute from first key to last.
Master progress = per-word survival (words completed / attempted); finishing
odds for 50 words are that number to the 50th power.
The Python side lives in the repo's `analysis/.venv`; after editing it:
`cd analysis && .venv/bin/pip install .`

## Data

| Table        | One row per                                                       |
|--------------|-------------------------------------------------------------------|
| `drills`     | drill: mode, outcome, failed word, tags, config, parent drill     |
| `targets`    | word or sentence shown: when shown, when completed, source        |
| `keystrokes` | key: ns since drill start, expected, typed, correct, backspace    |

Open it with `sqlite3 "$(teepee data)/teepee.db"` or DuckDB.
Month-end copies appear in `data/snapshots/teepee-YYYY-MM.db` automatically, kept forever.

## After changing the code

| Command                                   | Does                                   |
|-------------------------------------------|----------------------------------------|
| `cargo install --path .` (in the repo)    | rebuild the `teepee` binary you run    |
| `cd analysis && .venv/bin/pip install .`  | reinstall the stats package            |
