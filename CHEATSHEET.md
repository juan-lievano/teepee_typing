# teepee
Repo `~/Documents/Developing/teepee_typing`, data in `data/`. (last; x) = the mode's last, x at first.

|                   | Command                                     | Does                                            |
|-------------------|---------------------------------------------|-------------------------------------------------|
| **Type**          | `teepee`                                    | same as `teepee master`                         |
|                   | `teepee master`                             | first wrong key fails; the word ×~25 keys       |
|                   | &nbsp;&nbsp;`--list 200\|1k\|5k\|10k`       | word list (last; 10k)                           |
|                   | &nbsp;&nbsp;`--words 30`                    | drill length (last; 50)                         |
|                   | &nbsp;&nbsp;`--keep-case`                   | keep capitals; `=false` undoes (last; off)      |
|                   | `teepee sprint`                             | wrong keys let through, for bests               |
|                   | &nbsp;&nbsp;`--list 200\|1k\|5k\|10k`       | word list (last; 200)                           |
|                   | &nbsp;&nbsp;`--words 25`                    | drill length (last; 50)                         |
|                   | `teepee lit`                                | last book; a line repeats under 95%             |
|                   | `teepee lit pride`                          | name, prefix or number                          |
|                   | `--tag voyager`                             | any mode: sticky tag; repeatable; `none` clears |
|                   | `--afk 5`                                   | any mode: pause that voids a drill (3 s)        |
|                   | `Esc`                                       | quit; saved as aborted once a letter is typed   |
|                   | `Backspace`                                 | fix; goes back a word (sprint, lit); shows a caret |
|                   | `Alt/Ctrl+Backspace`, `Ctrl+W`              | delete the word, or the one before (sprint, lit) |
|                   | &nbsp;after a fail | keys ignored until hands are still 0.4 s        |
|   | ────────────────────────────────── | ─────────────────────────────────────────────── |
| **Stats**         | `teepee stats`                              | last exercise: wpm bars, median, best, trend    |
|                   | `teepee stats sprint\|master\|lit`          | long view of that exercise                      |
|                   | &nbsp;&nbsp;`--list english_1k`             | which list (last played)                        |
|                   | &nbsp;&nbsp;`--book quijote`                | which book (last played)                        |
|                   | &nbsp;&nbsp;`--full`                        | long view of every exercise                     |
|                   | &nbsp;&nbsp;`--open`                        | master progress graph as PNG                    |
|                   | &nbsp;&nbsp;`--days 7`                      | look back N days                                |
|                   | &nbsp;&nbsp;`--tag voyager`                 | only that tag                                   |
|                   | `teepee journal`                            | a cell a day, a dot per 2 min typed, key to key |
|                   | &nbsp;wpm | Monkeytype's, first key to last                 |
|   | ────────────────────────────────── | ─────────────────────────────────────────────── |
| **Tags**          | `teepee tags`                               | current tags                                    |
|                   | `teepee retag macbook --last 2h`            | retag recent drills (`30m`, `1d`)               |
|                   | `teepee retag none --ids 120-140`           | by id (`12-`, `-30`, `17`); `none` clears       |
|                   | &nbsp;&nbsp;`--dry-run`                     | show, don't write                               |
|   | ────────────────────────────────── | ─────────────────────────────────────────────── |
| **Books**         | `teepee books`                              | books ready                                     |
|                   | `teepee book 1342 --name pride`             | Gutenberg id or `./file.txt`                    |
|                   | &nbsp;&nbsp;`--from "It is a truth"`        | start there                                     |
|                   | &nbsp;&nbsp;`--to "THE END"`                | stop before                                     |
|                   | &nbsp;&nbsp;`--lang es`                     | language (en)                                   |
|                   | &nbsp;&nbsp;`--verse`                       | one verse per line                              |
|                   | &nbsp;&nbsp;`--min-chars N` `--max-chars N` | drop shorter / longer lines                     |
|   | ────────────────────────────────── | ─────────────────────────────────────────────── |
| **Data**          | `teepee data`                               | data directory; `TEEPEE_DATA_DIR=…` overrides   |
|                   | `teepee reset`                              | delete the database; books kept                 |
|                   | &nbsp;&nbsp;`--yes`                         | no question                                     |
|                   | `data/snapshots/`                           | month-end copies                                |
|                   | `drills`                                    | mode, outcome, failed word, tags, config        |
|                   | `targets`                                   | word or line: shown, completed, source          |
|                   | `keystrokes`                                | key: ns, expected, typed, kind, correct         |
|                   | `sources`                                   | word list or book                               |
|                   | `drill_summary`                             | view: wpm, accuracy per drill                   |
|                   | `sqlite3 $(teepee data)/teepee.db`          | open the database                               |
|   | ────────────────────────────────── | ─────────────────────────────────────────────── |
| **After editing** | `src/`                                      | `cargo install --path .`                        |
|                   | `analysis/`                                 | `cd analysis && .venv/bin/pip install .`        |
