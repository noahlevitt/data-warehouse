"""Build records/names/first_names.csv.gz: US first names with how many girls and boys got them (1930–2017).

Source: the US Social Security Administration's baby names (every name given to at least 5 children of a sex in a
year), as published in the R package babynames 1.0.1 by Hadley Wickham (CC0, github.com/hadley/babynames, file
data/babynames.rda). Births from 1930 on are counted, so the shares fit today's adults; names are lower-cased;
placeholders such as Baby or Unknown are left out (:data:`NOT_NAMES`).
records.gender guesses Fb / Mb from these counts.

Usage (needs pyreadr: pip install pyreadr)::

    curl -LO https://raw.githubusercontent.com/hadley/babynames/master/data/babynames.rda
    python scripts/names/build_first_names.py babynames.rda records/names/first_names.csv.gz
"""

from __future__ import annotations

import csv
import gzip
import io
import sys

FROM_YEAR = 1930
# Not names: what birth records hold when there was none yet ("Baby", "Unknown"…), and words a name field could hold
# instead of a name. Leaving them out lets the email be used for such a record (records.gender.guess).
NOT_NAMES = {"unknown", "baby", "babygirl", "babyboy", "infant", "unnamed", "notnamed", "noname", "nameless", "male",
             "female", "boy", "girl", "none", "null", "unk", "child", "twin", "twins", "test", "mr", "mrs", "ms",
             "miss", "dr", "sir", "madam", "woman", "na", "blank", "other", "customer", "guest"}


def main(src: str, out: str) -> int:
    import pyreadr  # only needed to rebuild the table

    df = next(iter(pyreadr.read_r(src).values()))
    df = df[df["year"] >= FROM_YEAR]
    df = df.assign(name=df["name"].str.lower())
    df = df[~df["name"].isin(NOT_NAMES)]
    counts = df.pivot_table(index="name", columns="sex", values="n", aggfunc="sum", fill_value=0).astype(int)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["name", "female", "male"])
    for name, row in counts.sort_index().iterrows():
        w.writerow([name, int(row.get("F", 0)), int(row.get("M", 0))])
    with gzip.GzipFile(out, "wb", mtime=0) as fh:      # mtime 0: the same data gives the same file
        fh.write(buf.getvalue().encode())
    print(f"{len(counts):,} names written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
