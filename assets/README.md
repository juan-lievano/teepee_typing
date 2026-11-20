# assets

Monkeytype word lists from `frontend/static/languages/` in
monkeytypegame/monkeytype (GPL-3.0), fetched 2026-10-30, vendored unchanged so
results stay comparable with Monkeytype's tests. All ordered by frequency.

| file               | Monkeytype name | words | `teepee master --list` |
|--------------------|-----------------|-------|------------------------|
| `english.json`     | english         |   200 | `200`                  |
| `english_1k.json`  | english_1k      |  1000 | `1k`                   |
| `english_5k.json`  | english_5k      |  5000 | `5k`                   |
| `english_10k.json` | english_10k     |  9944 | `10k` (default)        |

## Literary lists

Built here, not vendored: `teepee-literary` (`analysis/teepee_analysis/literary.py`) counts the
words of the books named in `literary_books.json` (28 authors who wrote in English, 366 Project
Gutenberg texts, fetched 2026-11-19 into `data/corpus/`) and writes the lists. Each author
counts the same however much they wrote; names are dropped. Each list is the top of one
ranking, so `literary_1k` starts with `literary_200`.

| file                | words | `teepee master --list` |
|---------------------|-------|------------------------|
| `literary_200.json` |   200 | `literary-200`         |
| `literary_1k.json`  |  1000 | `literary-1k`          |
| `literary_5k.json`  |  5000 | `literary-5k`          |
| `literary_10k.json` | 10000 | `literary-10k`         |
| `literary_25k.json` | 25000 | `literary-25k`         |
