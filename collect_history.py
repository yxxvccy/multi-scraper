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
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import vsin_guard

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


def _derive_game_id(gamecode: str, sport: str, away: str, home: str) -> str:
    """Build the same game_id that multi_scraper writes to the timeseries.

    The history fragment carries both team names and the gamecode carries the
    authoritative date, so the id is derivable here -- no timeseries lookup, no
    dependency on whether the scrape ran before or after this harvest.

    Reuses make_game_id rather than reimplementing its normalisation. These ids
    are worth having only if they are byte-identical to the ones the dashboard
    already keys on, so a local copy that drifted from make_game_id would be
    worse than the empty column it replaces. Verified against the committed
    timeseries: 60/60 exact matches, 0 mismatches.

    Returns "" when the gamecode carries no date (the offline --test path
    passes "TEST"), so make_game_id's today-fallback never fires here -- an
    id stamped with the harvest date would silently fail to join.
    """
    if not re.match(r"^\d{8}", str(gamecode or "")):
        return ""
    # Imported lazily: main() already pulls multi_scraper in, but --test must
    # stay runnable without selenium installed.
    from multi_scraper import make_game_id
    return make_game_id(sport, str(gamecode)[:8], away, home)


def parse_history(html: str, gamecode: str, source: str, sport: str,
                  game_id: str = "") -> list[dict]:
    """Parse one splits_history.php fragment into long-format rows.

    Raises NotCovered when the book simply doesn't offer the game.
    Raises ValueError when the fragment IS a card but doesn't parse -- the
    caller saves that one for inspection rather than guessing.

    game_id is derived from the gamecode and team names when not supplied, so
    these rows join to the timeseries (and to the dashboard, which keys on
    game_id throughout) without a lookup table.
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

    if not game_id:
        # Every entry in a card restates the same two teams. When that holds,
        # the id is unambiguous. When it doesn't, the team-cell cleanup left
        # residue on some rows and picking whichever name landed first would
        # be a plausible wrong id -- so leave game_id empty instead.
        #
        # Deliberately NOT a ValueError: that would discard the whole card,
        # and a card is unrecoverable once the 25-change window rotates past
        # it, whereas a missing game_id can be backfilled from this same file
        # at any time. A blank cell is recoverable; a lost harvest is not.
        # main() counts the blanks so they don't sit unnoticed.
        aways = {r["team"] for r in out if r["side"] == "away"}
        homes = {r["team"] for r in out if r["side"] == "home"}
        if len(aways) == 1 and len(homes) == 1:
            gid = _derive_game_id(gamecode, sport, aways.pop(), homes.pop())
            if gid:
                for r in out:
                    r["game_id"] = gid

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


# ---------------------------------------------------------------------------
# Fetch planning (added 2026-11-01 after the VSiN lockout)
# ---------------------------------------------------------------------------
# Before: every run re-requested every game on the board from BOTH books --
# finished games whose history can no longer change, games a week out whose
# 25-change window is still wide, and Circa for games Circa never posts.
# Now each (gamecode, source) carries a little state and the plan skips
# calls that cannot return anything new.
#
#   done        game date is past AND we fetched it after that ET day ended.
#               VSiN stops recording at kickoff, so that fetch was final.
#   far out     game date > today: once per FUTURE_REFETCH_H is enough while
#               the window is still wide.
#   today       at most once per TODAY_MIN_GAP_H (midday + post-slate).
#   Circa n/c   Circa did not post it last time: recheck once per FUTURE_REFETCH_H.
#
# Priority when the budget binds: overdue (about to drop off the board) >
# today > tomorrow > later; DK before Circa within each.

STATE_FILE = HIST_DIR / "_fetch_state.json"
FUTURE_REFETCH_H = 20
TODAY_MIN_GAP_H = 1
FUTURE_CAP = int(os.environ.get("VSIN_FUTURE_CAP") or 80)   # per sport per run
RECOVER_DAYS = 1           # look back this many game-days in our own records
_RATE_LIMITED = [None]


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    cutoff = (datetime.now(vsin_guard.ET) - timedelta(days=10)).strftime("%Y%m%d")
    state = {k: v for k, v in state.items() if k[:8] >= cutoff}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _mark(state, gc, src, not_covered=False):
    state[f"{gc}|{src}"] = {"last": vsin_guard.now_utc().isoformat(timespec="seconds"),
                            "nc": bool(not_covered)}


def _game_date(gc: str):
    m = re.match(r"(\d{8})", gc or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _plan(codes, sources, state, circa_card=None):
    now = vsin_guard.now_utc()
    today = now.astimezone(vsin_guard.ET).date()
    plan, skipped = [], Counter()
    for gc in codes:
        gd = _game_date(gc)
        for src in sources:
            st = state.get(f"{gc}|{src}") or {}
            last = None
            if st.get("last"):
                try:
                    last = datetime.fromisoformat(st["last"])
                except ValueError:
                    last = None
            age_h = (now - last).total_seconds() / 3600 if last else None
            if gd is None:
                bucket = 2
            elif gd < today:
                last_et_date = last.astimezone(vsin_guard.ET).date() if last else None
                if last_et_date and last_et_date > gd:
                    skipped["done"] += 1
                    continue
                bucket = 0
            elif gd == today:
                bucket = 1
                if age_h is not None and age_h < TODAY_MIN_GAP_H:
                    skipped["fetched <1h ago"] += 1
                    continue
            else:
                bucket = 2 if (gd - today).days == 1 else 3
                if age_h is not None and age_h < FUTURE_REFETCH_H:
                    skipped["future, fresh"] += 1
                    continue
            if src == "CIRCA" and circa_card and gc not in circa_card:
                skipped["not on Circa card"] += 1
                continue
            if st.get("nc") and age_h is not None and age_h < FUTURE_REFETCH_H \
                    and bucket != 0:
                skipped[f"{src} not posted"] += 1
                continue
            plan.append((bucket, 0 if src == "DK" else 1, gc, src))
    plan.sort(key=lambda t: (t[0], t[1]))
    # Future games rotate through: their 25-change window is still wide, so
    # a game deferred today loses nothing. This keeps a big midweek CFB board
    # from eating the budget that today's games (other sports) need.
    out, n_future = [], 0
    for b, _, gc, src in plan:
        if b >= 2:
            if n_future >= FUTURE_CAP:
                skipped["future, deferred"] += 1
                continue
            n_future += 1
        out.append((gc, src))
    return out, skipped


def _circa_card(sport: str) -> set:
    """Gamecodes the scraper actually saw on Circa's board (today/yesterday).
    Empty set = unknown, so the planner falls back to probing."""
    today = datetime.now(vsin_guard.ET).date()
    card = set()
    for d in (0, 1):
        f = Path("data") / "timeseries" / f"{sport}_{(today - timedelta(days=d)):%Y%m%d}.csv"
        if not f.exists():
            continue
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("source") == "vsin_circa" and r.get("gamecode"):
                        card.add(r["gamecode"].strip())
        except Exception:
            pass
    return card


def _recent_codes_from_records(sport: str):
    """Gamecodes dated within RECOVER_DAYS before today, from our own history
    and timeseries CSVs -- so a game that already fell off VSiN's board still
    gets its final post-kickoff harvest."""
    today = datetime.now(vsin_guard.ET).date()
    lo = today - timedelta(days=RECOVER_DAYS)
    out = []
    files = []
    for d in range(RECOVER_DAYS + 2):
        day = (today - timedelta(days=d)).strftime("%Y%m%d")
        files += [HIST_DIR / f"{sport}_{day}.csv",
                  Path("data") / "timeseries" / f"{sport}_{day}.csv"]
    for f in files:
        if not f.exists():
            continue
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    gc = (r.get("gamecode") or "").strip()
                    gd = _game_date(gc)
                    if gd and lo <= gd < today:
                        out.append(gc)
        except Exception as e:
            print(f"  could not read {f} for recovery: {e}")
    return list(dict.fromkeys(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="FILE",
                    help="parse a saved fragment offline; writes nothing")
    ap.add_argument("--sport", default="cfb")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch only the first 3 games and print, no CSV")
    ap.add_argument("--sources", default="DK,CIRCA")
    ap.add_argument("--throttle", type=float, default=None,
                    help="override VSIN_MIN_GAP: min seconds between requests "
                         f"(default {vsin_guard.MIN_GAP}). Was 1.0 until the "
                         "2026-11-01 lockout.")
    ap.add_argument("--skip-if-exists", action="store_true",
                    help="DEPRECATED and ignored. Writes are now deduplicated on "
                         "(gamecode, source, kind, ts_iso, side), so repeat runs "
                         "are idempotent and top up rather than double-append. "
                         "Existence-based skipping was the wrong primitive: a "
                         "pre-kickoff test run created the file, which then made "
                         "the post-slate run skip and permanently lock in "
                         "incomplete histories. Flag kept so existing workflows "
                         "do not break.")
    args = ap.parse_args()
    if args.throttle is not None:
        vsin_guard.MIN_GAP = args.throttle

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
    if args.skip_if_exists:
        print("note: --skip-if-exists is deprecated and ignored; writes are "
              "deduplicated instead, so repeat runs top up safely.")

    # Exits 0 here, before any request, while a VSiN lockout is active.
    vsin_guard.start_run("history", args.sport)

    from multi_scraper import (SOURCES, fetch_vsin, get_driver,
                               _gamecode_cross_league_check)
    from selenium.webdriver.common.by import By

    url = SOURCES["vsin_dk"]["url_fn"](args.sport)
    if not url:
        sys.exit(f"no vsin URL for {args.sport}")

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    state = _load_state()
    is_foreign, _, _ = _gamecode_cross_league_check(args.sport)
    sources = [s.strip().upper() for s in args.sources.split(",") if s.strip()]

    driver = get_driver()
    all_rows, failures = [], []
    not_covered = Counter()
    rate_limited = None
    try:
        print(f"loading {url} ...")
        fetch_vsin(url, driver, args.sport, source_key="vsin_dk")
        btns = driver.find_elements(By.CSS_SELECTOR, "button.sp-act-history")
        board = [b.get_attribute("data-gamecode") for b in btns]
        board = [c for c in dict.fromkeys(board) if c]
        # 2026-11-01: the epl/ucl/wcbb harvests were reading the WHOLE
        # multi-league board (CFB/NBA/NHL/NFL gamecodes) and re-requesting
        # every one of those games -- roughly tripling request volume and
        # writing other sports' history into the wrong files. Apply the same
        # cross-league filter the scraper uses.
        own = [c for c in board if not is_foreign(c)]
        if len(own) < len(board):
            print(f"  dropped {len(board) - len(own)} other-league gamecode(s) "
                  f"from the {args.sport} board")
        # Recovery: games from our own records that may already be off the
        # live board (VSiN drops finished games ~9am next day, and a dropped
        # or lockout-skipped run would otherwise lose them for good).
        recovered = [c for c in _recent_codes_from_records(args.sport)
                     if c not in own and not is_foreign(c)]
        if recovered:
            print(f"  + {len(recovered)} recent gamecode(s) from our own records")
        codes = own + recovered
        print(f"gamecodes: {len(codes)} ({len(own)} on board)")

        card = _circa_card(args.sport)
        if card:
            print(f"  Circa card from timeseries: {len(card)} game(s)")
        plan, skipped = _plan(codes, sources, state, card)
        print(f"  plan: {len(plan)} call(s); skipped {sum(skipped.values())} "
              f"({', '.join(f'{k} {v}' for k, v in sorted(skipped.items())) or 'none'})")
        if args.dry_run:
            plan = plan[:6]
            print(f"dry run -- first {len(plan)} call(s)")
        cap = vsin_guard.remaining()
        if len(plan) > cap:
            print(f"::warning title=VSiN budget::{args.sport}: {len(plan)} calls planned, "
                  f"budget allows {cap}. Deferring the lowest-priority "
                  f"{len(plan) - cap} (furthest-out games).")
            plan = plan[:cap]

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

        consecutive_bad = 0
        for i, (gc, src) in enumerate(plan, 1):
            vsin_guard.pace()
            vsin_guard.note("history")
            try:
                res = driver.execute_async_script(js, gc, src)
            except Exception as e:
                res = {"status": -2, "body": f"script error: {e}"}
            body = res.get("body") or ""
            status = res.get("status")
            # Pause page or HTTP 429: stop at once. Rows already collected are
            # still written below -- the exit happens after the save.
            if status == 429 or vsin_guard.is_rate_limited_text(body):
                rate_limited = body or "HTTP 429"
                break
            _mark(state, gc, src)
            if status != 200:
                failures.append((gc, src, f"HTTP {status}" if status != -2 else body))
                consecutive_bad += 1
                if consecutive_bad >= 3:
                    print("::error title=VSiN harvest::3 consecutive failed calls -- "
                          "stopping this sport rather than keep hitting VSiN")
                    break
                continue
            consecutive_bad = 0
            try:
                rows = parse_history(body, gc, src, args.sport)
            except NotCovered:
                not_covered[src] += 1          # expected; no dump, no noise
                _mark(state, gc, src, not_covered=True)
                continue
            except ValueError as e:
                failures.append((gc, src, str(e)))
                bad = RAW_DIR / f"history_fail_{gc}_{src}.html"
                bad.write_text(body, encoding="utf-8")
                continue
            all_rows.extend(rows)
            if i % 40 == 0:
                print(f"  {i}/{len(plan)} calls, {len(all_rows)} rows, "
                      f"{sum(not_covered.values())} not-covered, "
                      f"{len(failures)} failures")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        if not args.dry_run:
            _save_state(state)
    _RATE_LIMITED[0] = rate_limited

    print(f"\ncollected {len(all_rows)} rows")
    for src, n in sorted(not_covered.items()):
        print(f"  {src}: {n} game(s) not posted by this book (expected)")
    print(f"  real parse failures: {len(failures)}")
    # A row with no game_id cannot be joined to the timeseries or shown in the
    # dashboard, which keys on game_id. Count it rather than let it slip
    # through as a blank column nobody notices -- that is how this column sat
    # empty through three harvests.
    no_gid = {r["gamecode"] for r in all_rows if not r.get("game_id")}
    if no_gid:
        print(f"  WARNING: {len(no_gid)} game(s) produced no game_id "
              f"(unjoinable): {sorted(no_gid)[:5]}")
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

    # ---- dedupe against whatever is already in the file ----
    #
    # A history entry is uniquely identified by
    #   (gamecode, source, kind, ts_iso, side)
    # and its values never change once written: VSiN stops recording changes
    # at kickoff, so an entry is immutable the moment it exists. That makes
    # append-with-dedupe safe and makes every run idempotent -- a pre-slate
    # run is topped up by a post-slate one instead of blocking it, and the
    # redundant cron attempts cost nothing.
    def key(r):
        return (r["gamecode"], r["source"], r["kind"], r["ts_iso"], r["side"])

    out = out_path
    existing = set()
    if out.exists():
        try:
            with open(out, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    existing.add((r.get("gamecode", ""), r.get("source", ""),
                                  r.get("kind", ""), r.get("ts_iso", ""),
                                  r.get("side", "")))
        except Exception as e:
            print(f"could not read existing {out.name} for dedupe: {e}")
            print("refusing to append blind -- fix or remove the file first.")
            return

    # Dedupe within this batch too; a game can appear twice across sources.
    seen, fresh = set(), []
    for r in all_rows:
        k = key(r)
        if k in existing or k in seen:
            continue
        seen.add(k)
        fresh.append(r)

    dupes = len(all_rows) - len(fresh)
    if not fresh:
        print(f"nothing new -- all {len(all_rows)} rows already in {out.name}")
        return

    exists = out.exists()
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(fresh)
    print(f"wrote {out} (+{len(fresh)} new rows"
          + (f", {dupes} already present)" if dupes else ")"))


def _main_with_lockout():
    try:
        main()
    finally:
        if _RATE_LIMITED[0] is not None:
            raise vsin_guard.VSiNRateLimited(_RATE_LIMITED[0], "history")


if __name__ == "__main__":
    _main_with_lockout()
