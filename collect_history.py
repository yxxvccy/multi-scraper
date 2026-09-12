#!/usr/bin/env python3
"""
collect_history.py — harvest VSiN splits_history.php into data/history/

    python collect_history.py --test vsin_history_DK.html   # offline, no writes
    python collect_history.py --sport cfb --dry-run         # fetch 3 games, print
    python collect_history.py --sport cfb                   # full harvest

WHY THIS EXISTS
---------------
GitHub's scheduler delays and drops this repo's scheduled runs (observed
2026-09-12: an 11:21 AM cron fired at 1:45 PM, the 2:53 PM cron never fired).
A closing-line capture that depends on a cron landing inside a 75-minute
pre-kick window therefore cannot be relied on.

VSiN records line changes only until kickoff. So ONE call per game AFTER the
slate returns the full pre-kick run-up at ~5-minute resolution, plus the true
opening line. Timing stops mattering: this job must run eventually, not
precisely. Run it late at night with several redundant cron attempts; whichever
one lands gets everything.

This does NOT replace the timeseries scrape. "Last 25 Changes" is a hard cap
and cannot reach the midweek window, and the timeseries is the only record of
what we actually saw at decision time.

CAUTION
-------
The payload is HTML. Today's outage came from a parser that kept going when
its assumptions broke. This one asserts structure and SKIPS a game rather than
emitting partial rows, saving the fragment for inspection.
"""
import argparse
import csv
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("pip install -r requirements.txt")

HIST_DIR = Path("data") / "history"
RAW_DIR = Path("data") / "raw_html"

SECTIONS = {
    "opening split": "open",
    "first full split": "first_full",
}
CHANGES_RE = re.compile(r"last\s+\d+\s+changes", re.I)

FIELDS = [
    "collected_at", "sport", "game_id", "gamecode", "source", "book",
    "kind", "entry_idx", "ts_iso", "ts_date", "ts_time", "ts_rel",
    "side", "team",
    "spread", "spread_handle_pct", "spread_bets_pct",
    "total", "total_handle_pct", "total_bets_pct",
    "ml", "ml_handle_pct", "ml_bets_pct",
]


def _pct(t):
    m = re.search(r"(\d{1,3}(?:\.\d)?)\s*%", t or "")
    return float(m.group(1)) if m else ""


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _ts_iso(date_s: str, time_s: str, ref: datetime) -> str:
    """'Sep 6' + '12:20 PM ET' -> '2026-09-06T12:20:00'.

    VSiN omits the year. Infer it from the collection date, handling the
    Dec/Jan rollover the same way the scraper does.

    This column exists because the "Last N Changes" section is ordered
    NEWEST FIRST, so entry_idx runs backwards through time. Sorting on a real
    timestamp is the only safe way to identify the closing line -- it is
    max(ts_iso) among kind == 'change', which is the FIRST row in the file.
    """
    if not date_s or not time_s:
        return ""
    dm = re.match(r"([A-Za-z]{3})\w*\s+(\d{1,2})", date_s.strip())
    tm = re.match(r"(\d{1,2}):(\d{2})\s*([AaPp])[Mm]", time_s.strip())
    if not dm or not tm:
        return ""
    mon = _MONTHS.get(dm.group(1).title())
    if not mon:
        return ""
    day = int(dm.group(2))
    hh, mm = int(tm.group(1)), int(tm.group(2))
    if tm.group(3).lower() == "p" and hh != 12:
        hh += 12
    elif tm.group(3).lower() == "a" and hh == 12:
        hh = 0
    year = ref.year
    if mon == 12 and ref.month == 1:
        year -= 1
    elif mon == 1 and ref.month == 12:
        year += 1
    try:
        return datetime(year, mon, day, hh, mm).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _num(t):
    if not t:
        return ""
    if re.search(r"\b(PK|PICK|EVEN|EV)\b", t, re.I):
        return 0.0
    m = re.search(r"[+-]?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else ""


class NotCovered(Exception):
    """This book does not post this game. Expected, not a failure.

    Circa posts a selective card -- 46 of 121 CFB games on 2026-09-12. Their
    responses carry no card fragment. Treating that as a parse failure would
    bury a real break under ~75 false alarms and ~75 junk HTML dumps, which is
    precisely how this morning's outage stayed hidden.
    """


def parse_history(html: str, gamecode: str, source: str, sport: str,
                  game_id: str = "") -> list[dict]:
    """Parse one splits_history.php fragment into long-format rows.

    Raises NotCovered when the book simply doesn't offer the game.
    Raises ValueError when the fragment IS a card but doesn't parse -- the
    caller saves that one for inspection rather than guessing.
    """
    if "sp-game-card-wrap" not in html:
        raise NotCovered(f"{source} does not post {gamecode}")

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr", class_="sp-row")
    if len(rows) < 2:
        raise ValueError(f"expected >=2 sp-row, found {len(rows)}")
    if len(rows) % 2 != 0:
        raise ValueError(f"odd sp-row count ({len(rows)}); rows should pair by game")

    now = datetime.now().isoformat(timespec="seconds")
    book = "draftkings" if source.upper() == "DK" else "circa"

    # Walk the history table in document order so section headers and data
    # rows stay in sync. Header rows are plain <tr> without sp-row.
    table = None
    for t in soup.find_all("table"):
        if t.find("tr", class_="sp-row"):
            table = t
            break
    if table is None:
        raise ValueError("no table containing sp-row")

    out = []
    section = "unknown"
    entry_idx = 0
    pending = []          # the two team-rows of one timestamped entry
    meta = {"d": "", "t": "", "r": ""}

    for tr in table.find_all("tr"):
        classes = tr.get("class") or []
        text = tr.get_text(" ", strip=True)

        if "sp-row" not in classes:
            low = text.lower()
            for key, name in SECTIONS.items():
                if low.startswith(key):
                    section = name
                    break
            else:
                if CHANGES_RE.search(low):
                    section = "change"
            continue

        # A row that carries .sp-hist-meta starts a new timestamped entry.
        m = tr.find(class_="sp-hist-meta")
        if m is not None:
            if len(pending) == 1:
                raise ValueError("unpaired history row before a new entry")
            pending = []
            entry_idx += 1
            def g(cls):
                el = m.find(class_=cls)
                return el.get_text(" ", strip=True) if el else ""
            meta = {"d": g("sp-hist-date"), "t": g("sp-hist-time-pill"),
                    "r": g("sp-hist-rel")}

        cells = tr.find_all("td")
        # team cell + 9 data cells (spread, hnd, bet, total, hnd, bet, ml, hnd, bet)
        vals = [c.get_text(" ", strip=True) for c in cells]
        team_el = tr.find(class_="sp-cell-team")
        team = (team_el.get_text(" ", strip=True) if team_el else "").strip()
        team = re.sub(r"^(Sep|Oct|Nov|Dec|Jan|Aug)\s+\d+\s+\d+:\d+\s*[AP]M\s*ET\s*",
                      "", team).strip()
        if not team:
            raise ValueError("sp-row with no team cell")

        nums = vals[-9:] if len(vals) >= 9 else []
        if len(nums) != 9:
            raise ValueError(f"expected 9 data cells, got {len(nums)} for {team!r}")

        pending.append((team, nums))

        if len(pending) == 2:
            for side, (tm, n) in zip(("away", "home"), pending):
                out.append({
                    "collected_at": now, "sport": sport, "game_id": game_id,
                    "gamecode": gamecode, "source": source.upper(), "book": book,
                    "kind": section, "entry_idx": entry_idx,
                    "ts_iso": _ts_iso(meta["d"], meta["t"], datetime.now()),
                    "ts_date": meta["d"], "ts_time": meta["t"], "ts_rel": meta["r"],
                    "side": side, "team": tm,
                    "spread": _num(n[0]),
                    "spread_handle_pct": _pct(n[1]), "spread_bets_pct": _pct(n[2]),
                    "total": _num(n[3]),
                    "total_handle_pct": _pct(n[4]), "total_bets_pct": _pct(n[5]),
                    "ml": _num(n[6]),
                    "ml_handle_pct": _pct(n[7]), "ml_bets_pct": _pct(n[8]),
                })
            pending = []

    if not out:
        raise ValueError("parsed zero entries")
    return out


def _report(rows, label):
    print(f"\n  parsed {len(rows)} rows ({len(rows)//2} entries) from {label}")
    kinds = Counter(r["kind"] for r in rows)
    print(f"  sections: {dict(kinds)}")
    if "unknown" in kinds:
        print("  WARNING: some rows fell outside a recognised section header")
    print("\n  first entry:")
    for r in rows[:2]:
        print(f"    [{r['kind']:10}] {r['ts_date']} {r['ts_time']} {r['side']:4} "
              f"{r['team'][:26]:26} spr={r['spread']!s:>6} "
              f"hnd={r['spread_handle_pct']!s:>5} bet={r['spread_bets_pct']!s:>5} "
              f"tot={r['total']!s:>6}")
    print("  last entry:")
    for r in rows[-2:]:
        print(f"    [{r['kind']:10}] {r['ts_date']} {r['ts_time']} {r['side']:4} "
              f"{r['team'][:26]:26} spr={r['spread']!s:>6} "
              f"hnd={r['spread_handle_pct']!s:>5} bet={r['spread_bets_pct']!s:>5} "
              f"tot={r['total']!s:>6}")
    # sanity: complementary pairs should sum to ~100
    bad = 0
    for i in range(0, len(rows) - 1, 2):
        a, b = rows[i], rows[i + 1]
        for k in ("spread_handle_pct", "spread_bets_pct"):
            try:
                s = float(a[k] or 0) + float(b[k] or 0)
            except (TypeError, ValueError):
                continue
            if s and abs(s - 100) > 2:
                bad += 1
                break
    print(f"\n  pairs not summing to 100: {bad} / {len(rows)//2}")
    print("  (opening-split rows are placeholders -- 50/50 on Circa, 0% totals")
    print("   on DK -- so a few here are expected and are not a parse error)")

    # The change log runs newest-first, so identify the close by timestamp.
    stamps = [r["ts_iso"] for r in rows if r["kind"] == "change" and r["ts_iso"]]
    missing = sum(1 for r in rows if not r["ts_iso"])
    print(f"\n  ts_iso parsed: {len(rows)-missing}/{len(rows)}"
          + ("  <-- some failed, check date/time formats" if missing else ""))
    if stamps:
        print(f"  change window : {min(stamps)}  ->  {max(stamps)}")
        close = max(stamps)
        cr = [r for r in rows if r["ts_iso"] == close][:2]
        print(f"  CLOSING entry (max ts_iso, first row in file):")
        for r in cr:
            print(f"    {r['side']:4} {r['team'][:26]:26} spr={r['spread']!s:>6} "
                  f"hnd={r['spread_handle_pct']!s:>5} bet={r['spread_bets_pct']!s:>5} "
                  f"tot={r['total']!s:>6}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="FILE",
                    help="parse a saved fragment offline; writes nothing")
    ap.add_argument("--sport", default="cfb")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch only the first 3 games and print, no CSV")
    ap.add_argument("--sources", default="DK,CIRCA")
    ap.add_argument("--throttle", type=float, default=1.0,
                    help="seconds between requests (default 1.0)")
    ap.add_argument("--skip-if-exists", action="store_true",
                    help="exit 0 without fetching if today's output file already "
                         "exists. Lets several redundant cron attempts run safely: "
                         "whichever lands first does the work, the rest no-op "
                         "instead of double-appending.")
    args = ap.parse_args()

    # ---- offline test path: no Selenium, no network ----
    if args.test:
        p = Path(args.test)
        if not p.exists():
            sys.exit(f"{p} not found")
        src = "CIRCA" if "CIRCA" in p.name.upper() else "DK"
        rows = parse_history(p.read_text(encoding="utf-8"), "TEST", src, args.sport)
        _report(rows, p.name)
        print("\n  OK -- parser handles this fragment.")
        return

    out_path = HIST_DIR / f"{args.sport}_{datetime.now():%Y%m%d}.csv"
    if args.skip_if_exists and out_path.exists() and out_path.stat().st_size > 0:
        print(f"{out_path} already exists ({out_path.stat().st_size:,} bytes) "
              f"-- an earlier attempt succeeded. Nothing to do.")
        return

    from multi_scraper import SOURCES, fetch_vsin, get_driver
    from selenium.webdriver.common.by import By

    url = SOURCES["vsin_dk"]["url_fn"](args.sport)
    if not url:
        sys.exit(f"no vsin URL for {args.sport}")

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    driver = get_driver()
    all_rows, failures = [], []
    not_covered = Counter()
    try:
        print(f"loading {url} ...")
        fetch_vsin(url, driver, args.sport, source_key="vsin_dk")
        btns = driver.find_elements(By.CSS_SELECTOR, "button.sp-act-history")
        codes = [b.get_attribute("data-gamecode") for b in btns]
        codes = [c for c in dict.fromkeys(codes) if c]
        print(f"gamecodes: {len(codes)}")
        if args.dry_run:
            codes = codes[:3]
            print(f"dry run -- first {len(codes)}")

        driver.set_script_timeout(45)
        js = """
        const done = arguments[arguments.length - 1];
        const u = new URL(`splits_history.php?gamecode=${arguments[0]}&source=${arguments[1]}`,
                          window.location.href);
        fetch(u.toString(), {credentials:"same-origin",
                             headers:{"X-Requested-With":"XMLHttpRequest"}})
          .then(r => r.text().then(t => done({status:r.status, body:t})))
          .catch(e => done({status:-1, body:String(e)}));
        """
        sources = [s.strip().upper() for s in args.sources.split(",") if s.strip()]

        for i, gc in enumerate(codes, 1):
            for src in sources:
                try:
                    res = driver.execute_async_script(js, gc, src)
                except Exception as e:
                    failures.append((gc, src, f"script error: {e}"))
                    continue
                if res.get("status") != 200:
                    failures.append((gc, src, f"HTTP {res.get('status')}"))
                    continue
                try:
                    rows = parse_history(res["body"], gc, src, args.sport)
                except NotCovered:
                    not_covered[src] += 1          # expected; no dump, no noise
                    time.sleep(args.throttle)
                    continue
                except ValueError as e:
                    failures.append((gc, src, str(e)))
                    bad = RAW_DIR / f"history_fail_{gc}_{src}.html"
                    bad.write_text(res["body"], encoding="utf-8")
                    continue
                all_rows.extend(rows)
                time.sleep(args.throttle)
            if i % 20 == 0:
                print(f"  {i}/{len(codes)} games, {len(all_rows)} rows, "
                      f"{sum(not_covered.values())} not-covered, "
                      f"{len(failures)} failures")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print(f"\ncollected {len(all_rows)} rows")
    for src, n in sorted(not_covered.items()):
        print(f"  {src}: {n} game(s) not posted by this book (expected)")
    print(f"  real parse failures: {len(failures)}")
    for gc, src, why in failures[:10]:
        print(f"  FAIL {gc} {src}: {why}  -> fragment saved to data/raw_html/")
    if failures:
        print("  ^ these ARE cards that failed to parse. Investigate before"
              " trusting this file.")

    if args.dry_run:
        if all_rows:
            _report(all_rows, "dry run")
        return
    if not all_rows:
        print("nothing to write")
        return

    out = out_path
    exists = out.exists()
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(all_rows)
    print(f"wrote {out} (+{len(all_rows)} rows)")


if __name__ == "__main__":
    main()
