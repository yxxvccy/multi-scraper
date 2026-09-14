#!/usr/bin/env python3
"""Check that every data/history/ row has a game_id that joins to the timeseries.

Run from the repo root, after a backfill or after a harvest:

    python verify_history_game_ids.py

Exit code 0 = PASS, 1 = FAIL. Reads only; writes nothing.

"not-in-timeseries" is not a failure. History files reach weeks ahead of the
slate, so plenty of gamecodes have no timeseries row committed yet. Those ids
are still well-formed and will join once the game reaches the board. Only a
MISMATCH (an id that disagrees with the timeseries) or an empty row is a
failure -- either one breaks the dashboard join.
"""
import glob
import sys
import warnings

warnings.filterwarnings("ignore")

try:
    import pandas as pd
except ImportError:
    sys.exit("pip install -r requirements.txt")


def main() -> int:
    ts_files = glob.glob("data/timeseries/*.csv")
    hist_files = sorted(glob.glob("data/history/*.csv"))
    if not hist_files:
        print("no data/history/*.csv found -- run from the repo root")
        return 1

    # on_bad_lines="skip": some older timeseries files have ragged headers from
    # the known save_data bug. They are only a lookup table here, so a skipped
    # row costs nothing; it is not this script's job to police them.
    ts = pd.concat([pd.read_csv(f, dtype=str, on_bad_lines="skip")
                    for f in ts_files])
    truth = dict(ts[["gamecode", "game_id"]].dropna().drop_duplicates().values)

    ok = bad = unknown = empty = 0
    for f in hist_files:
        h = pd.read_csv(f, dtype=str)
        n_empty = int(h.game_id.isna().sum())
        if n_empty:
            print(f"  EMPTY {f}: {n_empty} row(s) with no game_id")
        empty += n_empty
        pairs = h[["gamecode", "game_id"]].dropna().drop_duplicates().values
        for gamecode, gid in pairs:
            expected = truth.get(gamecode)
            if expected is None:
                unknown += 1
            elif expected == gid:
                ok += 1
            else:
                bad += 1
                print(f"  MISMATCH {gamecode}: history={gid} timeseries={expected}")

    print(f"\nmatch={ok}  MISMATCH={bad}  not-in-timeseries={unknown}  "
          f"empty-rows={empty}")
    if bad or empty:
        print("FAIL -- do not commit")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
