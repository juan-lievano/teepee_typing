"""teepee-journal: the practice wall. A column per week, a row per weekday, each cell a block
of braille dots, one for every MINUTES minutes typed that day in every mode together, practice
reps included. Blank is a day off.

A drill's minutes run from its first key to its last, whatever its outcome: the wait before
the first key and the time between drills are not typing.
"""
import math
import random
import shutil
import sys
from datetime import date, datetime, timedelta

from .db import connect
from .stats import PAD, box

MINUTES = 2          # a dot; a cell holds 64, so it is full at 128 minutes
CELL, TALL = 4, 2    # a day's cell: braille characters across, lines down
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LABEL = 5            # the weekday column, "Mon  "
GAP = 2              # columns between two weeks
BITS = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80)   # the eight dots of a braille character, U+2800 plus their sum


def minutes_by_day(con):
    """Date -> minutes typed in the drills started that day."""
    out = {}
    sql = ("SELECT d.started_at_ms, max(k.t_ns) - min(k.t_ns) FROM tp.drills d"
           " JOIN tp.keystrokes k ON k.drill_id = d.id GROUP BY d.id, d.started_at_ms")
    for started, span in con.sql(sql).fetchall():
        d = datetime.fromtimestamp(started / 1000).date()
        out[d] = out.get(d, 0) + span / 60e9
    return out


def cell(minutes, day):
    """A day's lines, a dot lit for every MINUTES minutes (any typing at all lights one): dealt
    out to the cell's characters evenly, give or take one, then at random inside each, so the
    cell reads as one even density. The date fixes the draw: a day looks the same every run."""
    if minutes < 1:
        return [" " * CELL] * TALL
    n = CELL * TALL
    lit = min(8 * n, math.ceil(minutes / MINUTES))
    rng = random.Random(str(day))
    order = list(range(n))
    rng.shuffle(order)
    chars = [0] * n
    for i, c in enumerate(order):
        chars[c] = sum(rng.sample(BITS, lit // n + (i < lit % n)))
    return ["".join(chr(0x2800 + b) for b in chars[r * CELL:(r + 1) * CELL]) for r in range(TALL)]


def wall(by_day, weeks):
    """Month names over the weeks they start in, then TALL lines per weekday."""
    step = CELL + GAP
    months = [" "] * (step * len(weeks))
    for i, w in enumerate(weeks):
        if i == 0 or w.month != weeks[i - 1].month:
            months[step * i:step * i + 3] = w.strftime("%b")
    lines = [" " * LABEL + "".join(months).rstrip()]
    for d, name in enumerate(DAYS):
        cells = [cell(by_day.get(w + timedelta(d), 0), w + timedelta(d)) for w in weeks]
        lines += [((name if r == 0 else "").ljust(LABEL) + (" " * GAP).join(c[r] for c in cells)).rstrip() for r in range(TALL)]
    return "\n".join([*lines, "", f"a dot is {MINUTES} min, a full cell {8 * CELL * TALL * MINUTES} or more"])


def main() -> None:
    by_day = minutes_by_day(connect())
    if not by_day:
        print("no drills yet")
        return
    first = min(by_day) - timedelta(days=min(by_day).weekday())
    last = date.today() - timedelta(days=date.today().weekday())
    weeks = [first + timedelta(weeks=i) for i in range((last - first).days // 7 + 1)]
    room = (shutil.get_terminal_size().columns - 2 - 2 * PAD - LABEL + GAP) // (CELL + GAP) if sys.stdout.isatty() else len(weeks)
    weeks = weeks[-max(1, room):]   # the oldest weeks scroll off the left
    title = f"journal · {sum(by_day.values()):.0f} min since {min(by_day)}"
    print("\n" + box(title, wall(by_day, weeks)) + "\n")


if __name__ == "__main__":
    main()
