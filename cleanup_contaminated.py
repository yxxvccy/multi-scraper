#!/usr/bin/env python3
"""
cleanup_contaminated.py

One-shot cleanup for two kinds of contamination already saved to disk:

  1. Cross-sport contamination: rows from one sport's VSiN page accidentally
     written into another sport's CSV (e.g. MLB games landing in wcbb_*.csv,
     epl_*.csv, etc.). Caused by VSiN's tab-switch silently failing.

  2. Wrong-date contamination: rows where game_id encodes a date that
     doesn't match when ESPN says the game is actually scheduled. Caused
     by VSiN's stale/lingering page (yesterday's games still visible the
     next morning) or premature listing (tomorrow's games under today).

For each row we walk ESPN's schedule for the row's own sport across a small
window of dates (date_in_filename ± 2 days). If we find the team pair, the
row is "verified" and either kept (date matches) or migrated/dropped (date
mismatch). If we don't find the team pair on the row's own sport, we probe
other tracked sports â€” a positive match elsewhere is cross-contamination.

Behavior:
  - Conservative: rows with no positive ESPN evidence stay in place.
  - Dry-run by default. Pass --apply to write changes.

Usage:
    python cleanup_contaminated.py                  # dry run, print only
    python cleanup_contaminated.py --apply          # write changes
    python cleanup_contaminated.py --apply --backup # also write *.bak
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
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

# How many days on either side of the file's nominal date to search ESPN.
# We need a window because VSiN's stale-page bug can put a game in a CSV
# named after a different date than the game's actual ET date.
DATE_WINDOW_DAYS = 2


def fetch_espn(sport: str, date: str) -> list[dict]:
    """Return list of {away, home} dicts for sport+date."""
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


def date_window(center: str, days: int) -> list[str]:
    """Return ['YYYYMMDD'] for center Â± days."""
    try:
        c = datetime.strptime(center, "%Y%m%d")
    except ValueError:
        return [center]
    return [(c + timedelta(days=d)).strftime("%Y%m%d")
            for d in range(-days, days + 1)]


# Cache ESPN responses across the run
_espn_cache: dict[tuple[str, str], list[dict]] = {}

def get_schedule(sport: str, date: str) -> list[dict]:
    key = (sport, date)
    if key not in _espn_cache:
        _espn_cache[key] = fetch_espn(sport, date)
        time.sleep(0.2)  # be polite to ESPN
    return _espn_cache[key]


def extract_game_id_date(game_id: str) -> str:
    """game_id of form 'wnba_20260518_a_b' â†’ '20260518'. Empty if absent."""
    if not game_id:
        return ""
    m = re.search(r"_(\d{8})_", game_id)
    return m.group(1) if m else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to disk (default: dry run)")
    parser.add_argument("--backup", action="store_true",
                        help="Write *.bak alongside modified files")
    parser.add_argument("--data-dir", default=str(DATA_DIR),
                        help=f"Path to timeseries dir (default: {DATA_DIR})")
    parser.add_argument("--skip-date-check", action="store_true",
                        help="Skip wrong-date checks; only catch cross-sport "
                             "contamination (faster, fewer ESPN calls)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: {data_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    files = sorted(data_dir.glob("*.csv"))
    if not files:
        print(f"No CSVs found in {data_dir}")
        return

    total_in = total_kept = 0
    drops_cross_sport = 0
    drops_wrong_date = 0
    per_sport_drops: dict[str, int] = defaultdict(int)

    print(f"Scanning {len(files)} files in {data_dir} "
          f"({'APPLY mode' if args.apply else 'DRY RUN'})\n")

    for path in files:
        info = parse_filename(path.name)
        if not info:
            continue
        own_sport, file_date = info

        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames

        if not rows:
            continue

        # Pre-fetch own-sport schedules across the date window
        date_candidates = (
            date_window(file_date, DATE_WINDOW_DAYS)
            if not args.skip_date_check
            else [file_date]
        )
        own_schedules = {d: get_schedule(own_sport, d) for d in date_candidates}

        kept = []
        dropped_cross = []   # (row, target_sport)
        dropped_date = []    # (row, actual_date, encoded_date)
        for r in rows:
            away = r.get("away_team", "")
            home = r.get("home_team", "")
            if not away or not home:
                kept.append(r)
                continue

            encoded_date = extract_game_id_date(r.get("game_id", ""))

            # 1) Look for the game on own-sport schedule in the date window.
            #    Record which date ESPN says it's on, if any.
            espn_date_for_game = None
            for d in date_candidates:
                if row_matches_schedule(away, home, own_schedules.get(d, [])):
                    espn_date_for_game = d
                    break

            if espn_date_for_game:
                # Verified on own sport. Compare with the row's encoded date.
                if (not args.skip_date_check
                        and encoded_date
                        and encoded_date != espn_date_for_game):
                    # Wrong-date contamination: drop the row. (We'd rather
                    # drop than rewrite â€” the row's `game_date` column may
                    # also be wrong, and silent rewrites are dangerous.)
                    dropped_date.append((r, espn_date_for_game, encoded_date))
                    per_sport_drops[own_sport] += 1
                else:
                    kept.append(r)
                continue

            # 2) Not on own-sport. Probe other sports for cross-contamination.
            contaminated_to = None
            for other_sport in ESPN_EP:
                if other_sport == own_sport:
                    continue
                for d in date_candidates:
                    if row_matches_schedule(away, home, get_schedule(other_sport, d)):
                        contaminated_to = other_sport
                        break
                if contaminated_to:
                    break

            if contaminated_to:
                dropped_cross.append((r, contaminated_to))
                per_sport_drops[own_sport] += 1
            else:
                # No evidence either way â€” keep (rare team, minor schedule,
                # off-season, preseason, etc.).
                kept.append(r)

        total_in += len(rows)
        total_kept += len(kept)
        drops_cross_sport += len(dropped_cross)
        drops_wrong_date += len(dropped_date)

        n_dropped = len(dropped_cross) + len(dropped_date)
        if n_dropped:
            print(f"  {path.name}: drop {n_dropped}/{len(rows)} rows "
                  f"(cross-sport: {len(dropped_cross)}, wrong-date: {len(dropped_date)})")
            if dropped_cross:
                samples = defaultdict(set)
                for r, target in dropped_cross[:50]:
                    samples[target].add(f"{r.get('away_team')} @ {r.get('home_team')}")
                for tgt, pairs in samples.items():
                    shown = sorted(pairs)[:3]
                    more = f" (+{len(pairs)-3} more)" if len(pairs) > 3 else ""
                    print(f"      cross-sport â†’ {tgt}: {', '.join(shown)}{more}")
            if dropped_date:
                samples = defaultdict(set)
                for r, actual, encoded in dropped_date[:50]:
                    samples[(actual, encoded)].add(f"{r.get('away_team')} @ {r.get('home_team')}")
                for (actual, encoded), pairs in samples.items():
                    shown = sorted(pairs)[:3]
                    more = f" (+{len(pairs)-3} more)" if len(pairs) > 3 else ""
                    print(f"      wrong-date (file says {encoded}, ESPN says {actual}): "
                          f"{', '.join(shown)}{more}")

            if args.apply:
                if args.backup:
                    bak = path.with_suffix(path.suffix + ".bak")
                    bak.write_bytes(path.read_bytes())
                with path.open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(kept)

    total_dropped = drops_cross_sport + drops_wrong_date
    print(f"\nSummary: scanned {total_in} rows â†’ kept {total_kept}, "
          f"dropped {total_dropped} "
          f"(cross-sport: {drops_cross_sport}, wrong-date: {drops_wrong_date})")
    if per_sport_drops:
        print("By contaminated source sport (the CSV being cleaned):")
        for sport, n in sorted(per_sport_drops.items(), key=lambda kv: -kv[1]):
            print(f"  {sport:8s} {n}")

    if not args.apply and total_dropped:
        print("\nDry run â€” pass --apply to write changes.")


if __name__ == "__main__":
    main()

