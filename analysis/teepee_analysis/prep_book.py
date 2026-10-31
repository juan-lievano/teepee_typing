"""teepee-book: turn a Project Gutenberg text into one sentence per line.

    teepee-book 2000 --name quijote --lang es --from "En un lugar de la Mancha"
    teepee-book ./book.txt --name pride --from "It is a truth"
    teepee-book 1004 --name dante --verse --from "Midway upon the journey"

Writes <data dir>/books/<name>.txt, one target per line, and <name>.json with
the language, title and origin, which `teepee lit` records in the database's
`sources` table. Prose becomes one sentence per line (a semicolon ends a
sentence too); with --verse, poetry becomes one verse per line.

The text starts at the first paragraph containing --from and stops before the
first containing --to (Gutenberg's own header and licence are always cut;
prefaces are not, so pass --from). Curly quotes, dashes and «» become ASCII;
accented Latin letters and ¿¡ are kept; anything else non-ASCII becomes a
space. Very short and very long sentences, headings and bracketed notes are
dropped.
"""
import argparse
import json
import re
import sys
import unicodedata
import urllib.request

from .db import data_dir

ABBREV = {
    "mr", "mrs", "ms", "dr", "st", "mt", "no", "vs", "etc", "jr", "sr", "prof", "capt", "col", "gen", "lt", "hon", "rev",
    # Spanish
    "sra", "srta", "d", "dña", "ud", "uds", "vd", "vds", "excmo", "núm",
}

# Characters a sentence may contain: printable ASCII, accented Latin letters
# (á é í ó ú ü ñ and friends, in their precomposed form) and ¿¡.
KEEP = {chr(c) for c in range(32, 127)} | {"¿", "¡"} | {chr(c) for c in range(0xC0, 0x100) if chr(c).isalpha()}


def fetch(src: str) -> str:
    if src.isdigit():
        url = f"https://www.gutenberg.org/cache/epub/{src}/pg{src}.txt"
        with urllib.request.urlopen(url) as r:
            return r.read().decode("utf-8-sig")
    with open(src, encoding="utf-8-sig") as f:
        return f.read()


def header(text: str, field: str) -> str | None:
    m = re.search(rf"^{field}:\s*(.+?)\s*$", text[:5000], re.M)
    return m.group(1) if m else None


def strip_gutenberg(text: str) -> str:
    start = re.search(r"\*\*\* ?START OF.*?\*\*\*", text)
    end = re.search(r"\*\*\* ?END OF.*?\*\*\*", text)
    return text[start.end() if start else 0 : end.start() if end else len(text)]


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n")
    for a, b in [("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("«", '"'), ("»", '"'),
                 ("—", " - "), ("–", "-"), ("…", "..."), (" ", " ")]:
        text = text.replace(a, b)
    text = re.sub(r"\[\d+\]", "", text)  # footnote markers
    text = unicodedata.normalize("NFC", text)  # what a keyboard produces: á is one char
    return "".join(c if c in KEEP or c == "\n" else " " for c in text)


def paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def verse_lines(text: str) -> list[str]:
    """One target per line of verse. A lone line that does not end a sentence
    is a heading (Inferno: Canto I) and is dropped."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) == 1 and not re.search(r"""[.!?;]["')]?$""", lines[0]):
            continue
        out.extend(lines)
    return out


# A sentence ends at . ! ? (plus a closing quote) before a capital or ¿¡, or
# at a semicolon regardless of what follows.
START = r'["\'(\[]?[¿¡]?["\'(\[]?[A-ZÁÉÍÓÚÜÑ]'
BOUNDARY = re.compile(r'[.!?]["\')\]]?(\s+)(?=' + START + r')|;(\s+)')


def split_sentences(par: str) -> list[str]:
    parts, prev = [], 0
    for m in BOUNDARY.finditer(par):
        g = 1 if m.group(1) is not None else 2
        parts.append(par[prev : m.start(g)])
        prev = m.end(g)
    parts.append(par[prev:])
    out: list[str] = []
    for p in parts:
        last = re.search(r"(\w+)\.$", out[-1]) if out else None
        if out and last and last.group(1).lower() in ABBREV:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return [re.sub(r"^- ", "", s) for s in out]  # a dialogue dash is not part of the sentence


def looks_like_text(s: str, verse: bool) -> bool:
    """Drop front matter, captions and headings that survive the split."""
    if "[" in s or "]" in s or "_" in s or "*" in s:
        return False
    if not verse and not re.search(r"""[.!?;]["')]?$""", s):
        return False
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) < 0.5


def window(pars: list[str], start: str | None, stop: str | None) -> list[str]:
    def find(phrase: str) -> int:
        hits = [i for i, p in enumerate(pars) if phrase in p]
        if not hits:
            sys.exit(f"no paragraph contains {phrase!r}")
        return hits[0]
    lo = find(start) if start else 0
    hi = find(stop) if stop else len(pars)
    return pars[lo:hi]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Gutenberg id or path to a .txt")
    ap.add_argument("--name", required=True, help="book name used by `teepee lit <name>`")
    ap.add_argument("--lang", default="en", help="ISO 639-1 code stored with the keystrokes (default en)")
    ap.add_argument("--from", dest="start", metavar="TEXT", help="begin at the first paragraph containing TEXT")
    ap.add_argument("--to", dest="stop", metavar="TEXT", help="stop before the first paragraph containing TEXT")
    ap.add_argument("--verse", action="store_true", help="poetry: one verse per line instead of one sentence")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=300)
    a = ap.parse_args()

    raw = fetch(a.source)
    title = header(raw, "Title")
    if translator := header(raw, "Translator"):
        title = f"{title} ({translator})"
    text = normalise(strip_gutenberg(raw))
    verse = a.verse
    if verse:
        units = window(verse_lines(text), a.start, a.stop)
    else:
        units = [s for p in window(paragraphs(text), a.start, a.stop) for s in split_sentences(p)]
    kept = [s for s in units if a.min_chars <= len(s) <= a.max_chars and looks_like_text(s, verse)]
    if not kept:
        sys.exit("nothing kept")
    out = data_dir() / "books" / f"{a.name}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + "\n")
    meta = {
        "name": a.name,
        "lang": a.lang,
        "title": title,
        "origin": f"gutenberg:{a.source}" if a.source.isdigit() else a.source,
        "from": a.start,
        "to": a.stop,
        "unit": "line" if verse else "sentence",
        "sentences": len(kept),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(f"{out}: {len(kept)} {meta['unit']}s kept of {len(units)} ({a.lang}, {title})", file=sys.stderr)
    for s in kept[:3]:
        print(f"  {s}", file=sys.stderr)


if __name__ == "__main__":
    main()
