#!/usr/bin/env python3
"""
cleanup_contaminated.py

One-shot cleanup for cross-sport contamination already saved to disk.

Background: prior to the data-gamecode cross-league filter, the scraper
sometimes wrote rows from one sport's VSiN page into another sport's
timeseries CSV (e.g. MLB games landing in wcbb_*.csv, epl_*.csv, etc.).
This script walks every data/timeseries/<sport>_<date>.csv, checks each
row's teams against ESPN's schedule for that <sport>+<date>, and drops
rows whose teams unambiguously belong to a different tracked sport.

Behavior:
  - Conservative: a row is only dropped when its teams positively match
    a DIFFERENT sport's ESPN schedule on the same date. Rows that don't
    match anything stay in place.
  - Dry-run by default. Pass --apply to write the changes.
  - Prints a per-file summary of what would be removed.

Usage:
    python cleanup_contaminated.py             # dry run, print only
    python cleanup_contaminated.py --apply     # write changes in place
    python cleanup_contaminated.py --apply --backup    # also write *.bak
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from collections import defaultdict


DATA_DIR = Path("data/timeseries")

# Same endpoints the scraper / dashboard use.
ESPN_EP = {
    "cbb":   "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "wcbb":  "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard",
    "nba":   "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "wnba":  "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
    "nfl":   "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "nhl":   "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb":   "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "cfb":   "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "cbase": "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard",
    # EPL & UCL use a different ESPN path; their schedules are smaller and
    # less likely to be confused with US sports, so we just skip them in
    # cross-checks (the worst case is leaving a row that another loop
    # would also fail to identify).
}


def fetch_espn(sport: str, date: str) -> list[dict]:
    """Return list of {away_names, home_names} dicts for sport+date."""
    if sport not in ESPN_EP:
        return []
    url = f"{ESPN_EP[sport]}?dates={date}&limit=200"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except Exception as e:
        print(f"    [warn] ESPN fetch failed for {sport} {date}: {e}", file=sys.stderr)
        return []
    out = []
    for ev in data.get("events", []) or []:
        comps = (ev.get("competitions") or [{}])[0].get("competitors", []) or []
        away_names, home_names = [], []
        for c in comps:
            team = c.get("team", {}) or {}
            names = [team.get("displayName"), team.get("shortDisplayName"),
                     team.get("name"), team.get("location"), team.get("abbreviation")]
            names = [n for n in names if n]
            if c.get("homeAway") == "away":
                away_names = names
            elif c.get("homeAway") == "home":
                home_names = names
        if away_names and home_names:
            out.append({"away": away_names, "home": home_names})
    return out


def norm(s: str) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def fuzzy_match(a: str, b: str) -> bool:
    """Lightweight fuzzy match (mirrors the dashboard's fuzzy())."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    if x == y:
        return True
    xn, yn = x.replace(" ", ""), y.replace(" ", "")
    if len(xn) >= 4 and (xn in yn or yn in xn):
        return True
    if len(x) >= 4 and (x in y or y in x):
        return True
    return False


def row_matches_schedule(away: str, home: str, schedule: list[dict]) -> bool:
    for ev in schedule:
        if (any(fuzzy_match(away, n) for n in ev["away"])
                and any(fuzzy_match(home, n) for n in ev["home"])):
            return True
    return False


def parse_filename(name: str) -> tuple[str, str] | None:
    """timeseries filename â†’ (sport, date) or None."""
    m = re.match(r"^([a-z]+)_(\d{8})\.csv$", name)
    if not m:
        return None
    return m.group(1), m.group(2)


# Cache ESPN responses across the run
_espn_cache: dict[tuple[str, str], list[dict]] = {}

def get_schedule(sport: str, date: str) -> list[dict]:
    key = (sport, date)
    if key not in _espn_cache:
        _espn_cache[key] = fetch_espn(sport, date)
        time.sleep(0.2)  # be polite
    return _espn_cache[key]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to disk (default: dry run)")
    parser.add_argument("--backup", action="store_true",
                        help="Write *.bak alongside modified files")
    parser.add_argument("--data-dir", default=str(DATA_DIR),
                        help=f"Path to timeseries dir (default: {DATA_DIR})")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: {data_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    files = sorted(data_dir.glob("*.csv"))
    if not files:
        print(f"No CSVs found in {data_dir}")
        return

    total_in = total_kept = total_dropped = 0
    per_sport_drops: dict[str, int] = defaultdict(int)

    print(f"Scanning {len(files)} files in {data_dir} "
          f"({'APPLY mode' if args.apply else 'DRY RUN'})\n")

    for path in files:
        info = parse_filename(path.name)
        if not info:
            continue
        own_sport, date = info

        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames

        if not rows:
            continue

        own_schedule = get_schedule(own_sport, date)

        kept, dropped = [], []
        for r in rows:
            away = r.get("away_team", "")
            home = r.get("home_team", "")
            if not away or not home:
                kept.append(r)
                continue

            # 1) If teams match the row's OWN sport schedule â†’ keep.
            if own_schedule and row_matches_schedule(away, home, own_schedule):
                kept.append(r)
                continue

            # 2) Teams don't match own. Probe other sports' schedules on
            #    the same date. If a positive match is found in ANY other
            #    tracked sport â†’ this row is contaminated; drop it.
            contaminated_to = None
            for other_sport in ESPN_EP:
                if other_sport == own_sport:
                    continue
                other_schedule = get_schedule(other_sport, date)
                if other_schedule and row_matches_schedule(away, home, other_schedule):
                    contaminated_to = other_sport
                    break

            if contaminated_to:
                dropped.append((r, contaminated_to))
                per_sport_drops[own_sport] += 1
            else:
                # No evidence either way (rare team, minor schedule, off-season).
                # Keep, mirroring the dashboard's conservative behavior.
                kept.append(r)

        total_in += len(rows)
        total_kept += len(kept)
        total_dropped += len(dropped)

        if dropped:
            print(f"  {path.name}: drop {len(dropped)}/{len(rows)} rows")
            sample = defaultdict(list)
            for r, target in dropped[:50]:
                sample[target].append(f"{r.get('away_team')} @ {r.get('home_team')}")
            for tgt, pairs in sample.items():
                uniq = sorted(set(pairs))
                shown = uniq[:3]
                more = f" (+{len(uniq)-3} more)" if len(uniq) > 3 else ""
                print(f"      â†’ {tgt}: {', '.join(shown)}{more}")

            if args.apply:
                if args.backup:
                    bak = path.with_suffix(path.suffix + ".bak")
                    bak.write_bytes(path.read_bytes())
                with path.open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(kept)

    print(f"\nSummary: scanned {total_in} rows â†’ kept {total_kept}, "
          f"dropped {total_dropped}")
    if per_sport_drops:
        print("By contaminated source sport (the CSV being cleaned):")
        for sport, n in sorted(per_sport_drops.items(), key=lambda kv: -kv[1]):
            print(f"  {sport:8s} {n}")

    if not args.apply and total_dropped:
        print("\nDry run â€” pass --apply to write changes.")


if __name__ == "__main__":
    main()
