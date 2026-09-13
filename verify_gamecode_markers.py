#!/usr/bin/env python3
"""
verify_gamecode_markers.py — check SPORT_INFO's gamecode_league against reality.

    python verify_gamecode_markers.py nfl
    python verify_gamecode_markers.py nfl cfb
    python verify_gamecode_markers.py all-major

Today's outage: SPORT_INFO["cfb"] declared gamecode_league "NCAAF" while VSiN
actually emits "CFB" in gamecodes like 20260912CFB00130. The cross-league
filter then rejected 100% of rows, parse_vsin returned nothing, and
parse_splits_page silently fell through to parse_generic -- which fabricated
121 plausible-looking games with wrong teams and percentages read as lines.

Every sport's marker is the same class of guess. This checks them against the
live page before a slate rather than during one. Read-only: no CSV is written,
nothing in data/ is touched.
"""
import re
import sys
from collections import Counter

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("pip install -r requirements.txt")

try:
    from multi_scraper import SPORT_INFO, SOURCES, get_driver, fetch_vsin
except Exception as e:
    sys.exit(f"Could not import multi_scraper: {e}\nRun from the repo root.")

args = sys.argv[1:] or ["nfl"]
sports = []
for a in args:
    if a == "all-major":
        sports += [k for k, v in SPORT_INFO.items() if v.get("category") == "major"]
    elif a == "all":
        sports += list(SPORT_INFO.keys())
    elif a in SPORT_INFO:
        sports.append(a)
    else:
        sys.exit(f"Unknown sport: {a}\nValid: {', '.join(SPORT_INFO)}")
seen = set()
sports = [s for s in sports if not (s in seen or seen.add(s))]

driver = get_driver()
results = []
try:
    for sport in sports:
        info = SPORT_INFO[sport]
        declared = info.get("gamecode_league", "")
        url = SOURCES["vsin_dk"]["url_fn"](sport)
        if not url:
            results.append((sport, declared, None, "no vsin_dk URL"))
            continue

        print(f"\n--- {sport}: {url}")
        try:
            html = fetch_vsin(url, driver, sport, source_key="vsin_dk")
        except Exception as e:
            results.append((sport, declared, None, f"fetch failed: {e}"))
            continue

        soup = BeautifulSoup(html, "html.parser")
        prefixes = Counter()
        for el in soup.find_all(attrs={"data-gamecode": True}):
            gc = el.get("data-gamecode") or ""
            m = re.match(r"^\d+([A-Z]+)", gc)
            if m:
                prefixes[m.group(1)] += 1

        if not prefixes:
            results.append((sport, declared, None, "no data-gamecode found (off-season? page empty?)"))
            continue

        actual, n = prefixes.most_common(1)[0]
        note = f"{dict(prefixes)}"
        results.append((sport, declared, actual, note))
        print(f"    declared={declared!r}  actual={actual!r}  counts={dict(prefixes)}")
finally:
    try:
        driver.quit()
    except Exception:
        pass

print("\n" + "=" * 74)
print(f"{'sport':8} {'declared':10} {'actual':10} verdict")
print("-" * 74)
bad = 0
for sport, declared, actual, note in results:
    if actual is None:
        verdict = f"UNKNOWN -- {note}"
    elif not declared:
        verdict = "no marker declared (lenient mode) -- OK"
    elif actual.startswith(declared):
        verdict = "OK"
    else:
        verdict = f"*** MISMATCH -- all rows will be rejected ***"
        bad += 1
    print(f"{sport:8} {declared or '-':10} {actual or '-':10} {verdict}")
print("=" * 74)
if bad:
    print(f"\n{bad} sport(s) MISCONFIGURED. Set gamecode_league to the 'actual'")
    print("value in SPORT_INFO, then re-run. Until then every row for that")
    print("sport is discarded and the generic parser invents replacements.")
else:
    print("\nAll checked sports agree with the live page.")
