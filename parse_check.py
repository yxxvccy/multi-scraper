#!/usr/bin/env python3
"""
parse_check.py — run the REAL parser against the page the probe just saved.

    python parse_check.py

No network, no Selenium. Answers one question: does the current parser
produce correct rows from the current VSiN page? If yes, the bad data in
today's CSV was written during a transient page state, not by a layout change.
"""
import sys
from pathlib import Path

SRC = Path("vsin_cfb_probe.html")
if not SRC.exists():
    sys.exit("vsin_cfb_probe.html not found -- run probe_vsin_local.py first.")

try:
    from multi_scraper import parse_vsin
except Exception as e:
    sys.exit(f"Could not import multi_scraper: {e}\nRun from the repo root.")

html = SRC.read_text(encoding="utf-8")
games = parse_vsin(html, "vsin_dk", "cfb")

print("=" * 78)
print(f"parsed games: {len(games)}")
print("=" * 78)

bad_pair, self_match = 0, 0
for g in games[:8]:
    a, h = g["away_team"], g["home_team"]
    if a == h:
        self_match += 1
    print(f"\n{a}  @  {h}")
    print(f"   spread {g['spread_line']!s:>6}   total {g['total_line']!s:>6}")
    print(f"   spread handle  away {g['spread_away_handle_pct']!s:>5} / "
          f"home {g['spread_home_handle_pct']!s:>5}")
    print(f"   spread bets    away {g['spread_away_bets_pct']!s:>5} / "
          f"home {g['spread_home_bets_pct']!s:>5}")
    print(f"   total  handle  over {g['total_over_handle_pct']!s:>5} / "
          f"under {g['total_under_handle_pct']!s:>5}")
    print(f"   divergence  spr {g['spread_bets_handle_divergence']!s:>6}   "
          f"o/u {g['total_bets_handle_divergence']!s:>6}")

# Health check across ALL parsed games, not just the printed sample.
for g in games:
    if g["away_team"] == g["home_team"]:
        self_match += 1
    for a_k, h_k in [("spread_away_bets_pct", "spread_home_bets_pct"),
                     ("spread_away_handle_pct", "spread_home_handle_pct"),
                     ("total_over_bets_pct", "total_under_bets_pct"),
                     ("total_over_handle_pct", "total_under_handle_pct")]:
        try:
            s = float(g[a_k] or 0) + float(g[h_k] or 0)
        except (ValueError, TypeError):
            continue
        if s and abs(s - 100) > 2:
            bad_pair += 1
            break

print("\n" + "=" * 78)
print(f"self-matchups (away == home)      : {self_match}")
print(f"games with a pair not summing 100 : {bad_pair} / {len(games)}")
print("=" * 78)
if self_match == 0 and bad_pair == 0:
    print("PARSER IS HEALTHY against the current page.")
    print("-> The bad rows in today's CSV came from a transient page state.")
    print("-> Re-trigger the workflow; new rows should be clean.")
else:
    print("PARSER IS BROKEN against the current page. Send me this output.")
