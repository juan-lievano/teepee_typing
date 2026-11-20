"""teepee-literary: build the literary word lists from the books in assets/literary_books.json.

    teepee-literary              fetch what is missing, write assets/literary_{200,1k,5k,10k,25k}.json
    teepee-literary --report     also print what was kept and dropped

Every author counts the same, however much they wrote: a word's score is its share of each
author's words, averaged over the authors. A word must be used by MIN_AUTHORS of them, which
is what keeps one writer's coinages and one book's characters out. Proper nouns go by how a
word is written: one that is capitalised in the middle of a sentence far more often than it
is not is a name. Each list is the top of the same ranking, so 1k starts with 200.

Texts are kept in data/corpus/ (not in git); a book already there is not fetched again.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

from .prep_book import strip_gutenberg

REPO = Path(sys.prefix).parent.parent      # the venv sits in analysis/, inside the repo
BOOKS = REPO / "assets" / "literary_books.json"
CORPUS = REPO / "data" / "corpus"
MIRROR = "https://mirror.csclub.uwaterloo.ca/gutenberg"
SIZES = [("200", 200), ("1k", 1000), ("5k", 5000), ("10k", 10000), ("25k", 25000)]
MIN_AUTHORS = 4
NAME_RATIO = 3          # capitalised mid-sentence this many times as often as lowercase: a name

WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
SENTENCE_END = re.compile(r"[.!?:;]['\"”’)\]]*$")
# 's is a possessive (counted as the word before it) except in these
IS_CONTRACTIONS = {"it's", "he's", "she's", "that's", "there's", "what's", "let's", "here's", "who's", "where's", "how's"}
# the only words of one or two letters; the rest are scraps (th', 'll, pp.) and other languages (de, le, et)
HAD = {"i'd", "he'd", "she'd", "you'd", "we'd", "they'd", "it'd", "who'd", "there'd"}     # other 'd is verse: seem'd, pass'd
SHORT = set("a i o of to in he it as is at on be my by me so or no we an if up do us go am oh ah ye ay ha eh lo ho ox ma pa hi".split())
ROMAN = re.compile(r"(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})")
# What four authors share and still is not a word to practise: French, German, Italian and Latin
# (James, Wharton, the Brontës and Forster quote them freely), dialect spellings, Gutenberg's own notes.
NOT_WORDS = set("""
vous que von qui une mais sich comme bonne classe nicht fac fait faut dei grande eau merci oui femme monde donc
mich peu cette aux sont cela parti nom allons wie deux voce dem vos porte ein trop jeune nez sehr mieux diable
coeur mia avec sala reine vitae froid maison deus mio liebe revoir terre vor avant homme enfant les des mon und
der est dans pas sur elle nous votre notre bien tout sans das ich sie cum mes ses ces tres peut etre suis avez
ont ete dit moi toi lui eux cet quel quelle chez ici aussi encore toujours jamais rien dieu ami amie petit
petite bon mot roi vie nuit jour temps fois trois faire dire voir tant mal chose quod sed priori modus bona
terra firma annum non signore signorina senor senora contadina cavaliere dramatis sotto bric brac pince faux
dat maister niver hev dinna lang tak hoo divil raight iver comin rayther nothin agin doin summat mornin willin
mak ony natur bein sperrit lookin feyther ane leetle noo drownded hee twould heigho roo
ebook ebooks etext http www online email gutenberg ing vol viz ccx
""".split())


def fetch(book_id: int) -> str:
    """The book's text: from data/corpus/ if it is there, else from Project Gutenberg, else from a mirror
    (which files a book as 2/7/0/2701/2701-0.txt, or 2701.txt, or 2701-8.txt in Latin-1)."""
    path = CORPUS / f"{book_id}.txt"
    if not path.exists():
        folder = "/".join(str(book_id)[:-1] or "0") + f"/{book_id}"
        urls = [f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt"]
        urls += [f"{MIRROR}/{folder}/{book_id}{suffix}.txt" for suffix in ("-0", "", "-8")]
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    raw = r.read()
            except Exception:           # the site down for maintenance, or the book not filed under this name
                continue
            path.write_text(raw.decode("latin-1" if url.endswith("-8.txt") else "utf-8-sig"), encoding="utf-8")
            break
        else:
            raise SystemExit(f"could not fetch book {book_id}: tried {' '.join(urls)}")
        time.sleep(0.5)                 # be gentle with the host
    return path.read_text(encoding="utf-8-sig")


def count(text: str):
    """Three counts per lowercased word: written lowercase, capitalised inside a sentence, and
    capitalised where anything would be (the start of a sentence or of a line)."""
    low, cap, start = Counter(), Counter(), Counter()
    for line in strip_gutenberg(text).splitlines():
        opens = True                    # a line's first word: verse capitalises it, a heading is all of it
        for chunk in line.split():
            for part in re.split(r"-+|—", chunk):
                m = WORD.search(part)
                if not m:
                    continue
                w = m.group().replace("’", "'")
                key = w.lower()
                if key.endswith("'s") and key not in IS_CONTRACTIONS:
                    w, key = w[:-2], key[:-2]
                if not key.isascii() or (len(key) <= 2 and key not in SHORT) or key in NOT_WORDS or (key.endswith("'d") and key not in HAD):
                    pass
                elif (w.isupper() and len(w) > 1) or (len(key) > 1 and ROMAN.fullmatch(key)):
                    pass                # a heading, a speaker's name, a roman numeral
                elif key == "i" or key.startswith("i'"):
                    low[key] += 1       # the pronoun is no name
                elif w[0].isupper():
                    (start if opens else cap)[key] += 1
                else:
                    low[key] += 1
                opens = False
            if SENTENCE_END.search(chunk):
                opens = True
    return low, cap, start


def build(report: bool):
    authors = json.loads(BOOKS.read_text())
    CORPUS.mkdir(parents=True, exist_ok=True)
    per_author, low_all, cap_all = {}, Counter(), Counter()
    for author, books in authors.items():
        total = Counter()
        for book in books:
            low, cap, start = count(fetch(book["id"]))
            low_all += low
            cap_all += cap
            total += low + cap + start
        per_author[author] = total
        print(f"  {author:22} {len(books):3} books {sum(total.values()):>10,} words", file=sys.stderr)

    names = {w for w, n in cap_all.items() if n > NAME_RATIO * low_all[w]}
    score, used_by = Counter(), Counter()
    for author, total in per_author.items():
        size = sum(n for w, n in total.items() if w not in names and low_all[w])
        for w, n in total.items():
            if w not in names and low_all[w]:      # never once in lowercase: a name seen only at line starts
                score[w] += n / size / len(per_author)
                used_by[w] += 1
    ranked = [w for w, _ in score.most_common() if used_by[w] >= MIN_AUTHORS]
    ranked = ["I" + w[1:] if w == "i" or w.startswith("i'") else w for w in ranked]

    for short, n in SIZES:
        if len(ranked) < n:
            print(f"only {len(ranked)} words: literary_{short} not written", file=sys.stderr)
            continue
        out = {"name": f"literary_{short}", "orderedByFrequency": True, "words": ranked[:n]}
        (REPO / "assets" / f"literary_{short}.json").write_text(json.dumps(out, ensure_ascii=False, indent=0) + "\n")
    books = sum(len(b) for b in authors.values())
    print(f"{len(authors)} authors, {books} books, {sum(sum(t.values()) for t in per_author.values()):,} words; "
          f"{len(names):,} names dropped, {len(ranked):,} words used by {MIN_AUTHORS}+ authors", file=sys.stderr)
    if report:
        print("names dropped, most common:", " ".join(w for w, _ in cap_all.most_common(3000) if w in names)[:1500])
        lonely = [w for w, _ in score.most_common(8000) if used_by[w] < MIN_AUTHORS]
        print(f"\nleft out, under {MIN_AUTHORS} authors:", " ".join(lonely[:200]))
        lo = 0
        for short, n in SIZES:
            seg = ranked[lo:n]
            print(f"\n{short} (rank {lo + 1}-{n}), every {max(1, len(seg) // 60)}th:", " ".join(seg[:: max(1, len(seg) // 60)]))
            lo = n


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the literary word lists from the books in assets/literary_books.json.")
    ap.add_argument("--report", action="store_true", help="print what was kept and dropped")
    build(ap.parse_args().report)


if __name__ == "__main__":
    main()
