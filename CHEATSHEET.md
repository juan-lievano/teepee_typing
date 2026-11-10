# teepee

Repo `~/Documents/Developing/teepee_typing`, data in `data/` there.

## Type

| Command                      | Does                                   |
|------------------------------|----------------------------------------|
| `teepee`                     | same as `teepee master`                |
| `teepee master`              | first wrong key fails, word practised  |
| `--list 200\|1k\|5k\|10k`    | word list (last; 10k)                  |
| `--words 30`                 | drill length (last; 50)                |
| `--keep-case`                | keep capitals; `=false` undoes (last; off) |
| `teepee sprint`              | wrong keys let through, for bests      |
| `--list 200\|1k\|5k\|10k`    | word list (last; 200)                  |
| `--words 25`                 | drill length (last; 50)                |
| `teepee lit`                 | last book (none yet: pick)             |
| `teepee lit pride`           | name, prefix or number                 |

- (last; x) = the mode's last drill's, x at first.

| Any mode       | Does                                  |
|----------------|---------------------------------------|
| `--tag voyager` | sticky tag; repeatable; `none` clears |
| `--afk 5`      | pause that voids a drill (3 s)        |

| Key                              | Does                          |
|----------------------------------|-------------------------------|
| `Esc`                            | quit, drill saved as aborted  |
| `Backspace`                      | fix; goes back a word (sprint, lit) |
| `Alt/Ctrl+Backspace`, `Ctrl+W`   | delete the word (sprint, lit) |

- Master fail: the word for ~25 keystrokes, 10 reps at most, then a new drill.
- Lit: wrong words practised after the line; line repeats under 95%.
- Keys ignored until hands are still 0.4 s after a fail.
- Waiting before the first key is free.

## Stats

| Command                        | Does                                    |
|--------------------------------|-----------------------------------------|
| `teepee stats`                 | last exercise: wpm bars, median, best, trend / hour |
| `teepee stats sprint\|master\|lit` | long view of that exercise          |
| `--list english_1k`            | which list (last played)                |
| `--book quijote`               | which book (last played)                |
| `--full`                       | long view of every exercise             |
| `--open`                       | master progress graph as PNG            |
| `--days 7`                     | look back N days                        |
| `--tag voyager`                | only that tag                           |
| `teepee journal`               | minutes typed per day, a cell a day     |

- Exercise = mode + list or book.
- wpm: Monkeytype's, first key to last.
- Journal minutes: first key to last of every drill, practice included.

## Tags

| Command                           | Does                    |
|-----------------------------------|-------------------------|
| `teepee tags`                     | current tags            |
| `teepee retag macbook --last 2h`  | retag recent drills (`30m`, `1d`) |
| `teepee retag none --ids 120-140` | by id (`12-`, `-30`, `17`); `none` clears |
| `--dry-run`                       | show, don't write       |

## Books

| Command                           | Does                         |
|-----------------------------------|------------------------------|
| `teepee books`                    | books ready                  |
| `teepee book 1342 --name pride`   | Gutenberg id or `./file.txt` |
| `--from "It is a truth"`          | start there                  |
| `--to "THE END"`                  | stop before                  |
| `--lang es`                       | language (en)                |
| `--verse`                         | one verse per line           |
| `--min-chars N` `--max-chars N`   | drop shorter / longer lines  |

## Data

| Command              | Does                               |
|----------------------|------------------------------------|
| `teepee data`        | data directory                     |
| `teepee reset`       | delete the database; books kept    |
| `--yes`              | no question                        |
| `TEEPEE_DATA_DIR=…`  | another data directory             |

| Table        | Row per                                              |
|--------------|------------------------------------------------------|
| `drills`     | drill: mode, outcome, failed word, tags, config, parent |
| `targets`    | word or line: shown, completed, source               |
| `keystrokes` | key: ns since drill start, expected, typed, kind, correct |
| `sources`    | word list or book                                    |

- `sqlite3 "$(teepee data)/teepee.db"`; view `drill_summary` has wpm and accuracy.
- Month-end copies in `data/snapshots/`.
- Set the tag again after a reset.

## After editing

| Edited        | Run                                      |
|---------------|------------------------------------------|
| `src/`        | `cargo install --path .`                 |
| `analysis/`   | `cd analysis && .venv/bin/pip install .` |
