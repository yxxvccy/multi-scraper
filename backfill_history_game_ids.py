#!/usr/bin/env python3
"""Fill the empty game_id column in already-collected data/history/*.csv files.

collect_history.py accepted a `game_id` parameter but main() never passed one,
so every history row written before 2026-09-14 carries an empty game_id. The
dashboard and the timeseries both key on game_id, which made those files
unjoinable to everything else in the repo.

The id is recoverable without a lookup table: the gamecode carries the
authoritative date and the rows carry the two team names, which is exactly what
make_game_id needs. Verified against the committed timeseries -- 60/60 exact
matches, 0 mismatches -- before this script was written.

Explicit paths only. Defaulting to a glob over data/history/ is how
repair_csv_header.py nearly rewrote uniform-width historical files that were
internally consistent; the same trap applies here.

    python backfill_history_game_ids.py data/history/cfb_20260912.csv ...
    python backfill_history_game_ids.py --check data/history/*.csv

Idempotent: rows that already carry a game_id are never touched, so re-running
is a no-op. --check reports what would change and writes nothing.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_history import FIELDS, _derive_game_id


def backfill(path: Path, check_only: bool) -> tuple[int, int, list[str]]:
    """Returns (rows_filled, rows_already_set, problems)."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FIELDS:
            return 0, 0, [f"header does not match FIELDS -- refusing to rewrite. "
                          f"got {reader.fieldnames}"]
        rows = list(reader)

    problems = []
    # Resolve one id per gamecode, not per row: the teams are a property of the
    # card, and doing it once makes an inconsistent card visible instead of
    # producing two different ids for one game.
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["gamecode"], []).append(r)

    ids: dict[str, str] = {}
    for gc, grp in by_code.items():
        aways = {r["team"] for r in grp if r["side"] == "away"}
        homes = {r["team"] for r in grp if r["side"] == "home"}
        if len(aways) != 1 or len(homes) != 1:
            # Same call as parse_history: report and leave empty rather than
            # guess. Re-runnable once the underlying naming is fixed.
            problems.append(f"{gc}: inconsistent teams away={sorted(aways)} "
                            f"home={sorted(homes)} -- left empty")
            continue
        sport = grp[0]["sport"] or path.name.split("_")[0]
        gid = _derive_game_id(gc, sport, aways.pop(), homes.pop())
        if not gid:
            problems.append(f"{gc}: no date in gamecode -- left empty")
            continue
        ids[gc] = gid

    filled = already = 0
    for r in rows:
        if r["game_id"]:
            already += 1
            continue
        gid = ids.get(r["gamecode"])
        if gid:
            r["game_id"] = gid
            filled += 1

    if filled and not check_only:
        # Write via a temp file in the same directory, then replace. A partial
        # write here would corrupt a file that cannot be re-harvested -- the
        # "Last 25 Changes" window has long since rotated past these entries.
        tmp = path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(path)

    return filled, already, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="explicit history CSV paths")
    ap.add_argument("--check", action="store_true",
                    help="report only; write nothing")
    args = ap.parse_args()

    total = 0
    for p in args.paths:
        path = Path(p)
        if not path.exists():
            print(f"{p}: not found")
            continue
        filled, already, problems = backfill(path, args.check)
        verb = "would fill" if args.check else "filled"
        print(f"{path.name}: {verb} {filled}, already set {already}")
        for msg in problems:
            print(f"  PROBLEM {msg}")
        total += filled

    if args.check:
        print(f"\ncheck only -- nothing written ({total} rows would change)")
    else:
        print(f"\n{total} rows updated")


if __name__ == "__main__":
    main()
