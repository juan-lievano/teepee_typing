"""teepee-book: turn a Project Gutenberg text into one sentence per line.

    teepee-book 1342 --name pride          # fetch by Gutenberg id
    teepee-book ./book.txt --name pride    # or a local file

Writes <data dir>/books/<name>.txt. Normalises curly quotes, dashes and
accents to plain ASCII, drops very short and very long sentences.
"""
import argparse
import re
import sys
import unicodedata
import urllib.request

from .db import data_dir

ABBREV = {"mr", "mrs", "ms", "dr", "st", "mt", "no", "vs", "etc", "jr", "sr", "prof", "capt", "col", "gen", "lt", "hon", "rev"}


def fetch(src: str) -> str:
    if src.isdigit():
        url = f"https://www.gutenberg.org/cache/epub/{src}/pg{src}.txt"
        with urllib.request.urlopen(url) as r:
            return r.read().decode("utf-8-sig")
    with open(src, encoding="utf-8-sig") as f:
        return f.read()


def strip_gutenberg(text: str) -> str:
    start = re.search(r"\*\*\* ?START OF.*?\*\*\*", text)
    end = re.search(r"\*\*\* ?END OF.*?\*\*\*", text)
    return text[start.end() if start else 0 : end.start() if end else len(text)]


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n")
    for a, b in [("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
                 ("—", " - "), ("–", "-"), ("…", "..."), (" ", " ")]:
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c if 32 <= ord(c) < 127 or c == "\n" else " " for c in text)


def paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


BOUNDARY = re.compile(r'[.!?]["\')\]]?(\s+)(?=["\'(\[]?[A-Z])')


def split_sentences(par: str) -> list[str]:
    parts, prev = [], 0
    for m in BOUNDARY.finditer(par):
        parts.append(par[prev : m.start(1)])
        prev = m.end(1)
    parts.append(par[prev:])
    out: list[str] = []
    for p in parts:
        last = re.search(r"(\w+)\.$", out[-1]) if out else None
        if out and last and last.group(1).lower() in ABBREV:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


def looks_like_prose(s: str) -> bool:
    """Drop front matter, captions and headings that survive the split."""
    if "[" in s or "]" in s or "_" in s or "*" in s:
        return False
    if not re.search(r"""[.!?]["')]?$""", s):
        return False
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) < 0.5


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Gutenberg id or path to a .txt")
    ap.add_argument("--name", required=True, help="book name used by `teepee lit <name>`")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=300)
    a = ap.parse_args()

    text = normalise(strip_gutenberg(fetch(a.source)))
    sentences = [s for p in paragraphs(text) for s in split_sentences(p)]
    kept = [s for s in sentences if a.min_chars <= len(s) <= a.max_chars and looks_like_prose(s)]
    out = data_dir() / "books" / f"{a.name}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + "\n")
    print(f"{out}: {len(kept)} sentences kept of {len(sentences)}", file=sys.stderr)


if __name__ == "__main__":
    main()
