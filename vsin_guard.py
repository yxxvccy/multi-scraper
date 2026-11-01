"""
vsin_guard.py -- request budget, pacing and lockout handling for data.vsin.com

WHY THIS EXISTS (2026-11-01 lockout)
------------------------------------
VSiN paused the Pro account for ~24h with:

    "Paused: you are requesting pages faster than a VSiN Pro subscription
     allows. The full board returns at Mon 11:29 AM ET."

The throttle is on the ACCOUNT, so it also blocks manual browsing on the
owner's phone. Every request made while paused risks extending it. Before
this module, nothing in the pipeline counted requests, nothing recognised
the pause page (it carries the same "more games on today's board" copy as
the paywall, so it was misreported as "session expired"), and history.yml
kept looping through the remaining sports after the first one hit the wall.

WHAT IT DOES
------------
1. Lockout file  data/vsin_guard/lockout.json
   Written the moment the pause page is seen, with the "returns at" time
   parsed from the banner (+ buffer). Every job checks it first and exits 0
   with a warning while it is active -- zero requests to VSiN.

2. Ledger        data/vsin_guard/ledger/<run>.json
   One small file per process (never appended to, so two workflows pushing
   at once cannot conflict). Rolling usage = sum of files in the window.

3. Budget        rolling-24h and rolling-1h caps on AUTOMATED requests,
   chosen to leave headroom for manual browsing. Tunable without a code
   change via repo variables (Settings -> Secrets and variables -> Actions
   -> Variables): VSIN_DAILY_BUDGET, VSIN_HOURLY_BUDGET, VSIN_SPORT_CAP,
   VSIN_MIN_GAP.

   VSiN does not publish the limit. The defaults are deliberately
   conservative guesses; the ledger exists so they can be calibrated from
   real numbers (lockout.json also records usage at the moment of a lockout).
"""
from __future__ import annotations

import atexit
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-5))

GUARD_DIR = Path("data") / "vsin_guard"
LEDGER_DIR = GUARD_DIR / "ledger"
LOCKOUT_FILE = GUARD_DIR / "lockout.json"

EXIT_RATE_LIMITED = 75          # EX_TEMPFAIL -- history.yml stops on this


def _env_num(name, default, cast=float):
    v = (os.environ.get(name) or "").strip()
    try:
        return cast(v) if v else default
    except ValueError:
        return default


DAILY_BUDGET = _env_num("VSIN_DAILY_BUDGET", 1000, int)   # rolling 24h, automated
HOURLY_BUDGET = _env_num("VSIN_HOURLY_BUDGET", 400, int)  # rolling 60 min
SPORT_CAP = _env_num("VSIN_SPORT_CAP", 300, int)          # per process (one sport)
MIN_GAP = _env_num("VSIN_MIN_GAP", 4.0)                   # seconds between requests
JITTER = _env_num("VSIN_JITTER", 2.0)                     # + uniform(0, JITTER)
LOCKOUT_BUFFER_MIN = 30

RATE_LIMIT_MARKERS = (
    "requesting pages faster than",
    "full board returns at",
)

_DOW = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- lockout

def is_rate_limited_text(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in RATE_LIMIT_MARKERS)


def parse_return_time(text: str, ref: datetime | None = None) -> datetime | None:
    """'The full board returns at Mon 11:29 AM ET.' -> aware datetime (UTC)."""
    m = re.search(r"returns at\s+([A-Za-z]{3})\w*\s+(\d{1,2}):(\d{2})\s*([AP])M",
                  text or "", re.I)
    if not m:
        return None
    dow = _DOW.get(m.group(1).lower())
    hh, mm = int(m.group(2)), int(m.group(3))
    if m.group(4).upper() == "P" and hh != 12:
        hh += 12
    elif m.group(4).upper() == "A" and hh == 12:
        hh = 0
    ref_et = (ref or now_utc()).astimezone(ET)
    for add in range(0, 8):
        d = ref_et + timedelta(days=add)
        if dow is not None and d.weekday() != dow:
            continue
        cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > ref_et:
            return cand.astimezone(timezone.utc)
    return None


def lockout_until() -> datetime | None:
    try:
        data = json.loads(LOCKOUT_FILE.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(data["until_utc"])
    except Exception:
        return None
    return until if until > now_utc() else None


def exit_if_locked_out(job: str):
    """Call before the FIRST request. Exits 0 (with a warning) while paused."""
    until = lockout_until()
    if until:
        et = until.astimezone(ET)
        print(f"::warning title=VSiN lockout active::{job} skipped -- VSiN paused "
              f"this account until {et:%a %b %d %I:%M %p} ET. No requests made.")
        sys.exit(0)


class VSiNRateLimited(SystemExit):
    """VSiN served the pause page. Subclasses SystemExit for the same reason
    VSiNAuthError does: scrape_source() wraps fetches in `except Exception`."""

    def __init__(self, text: str = "", where: str = ""):
        until = parse_return_time(text)
        if until is None:
            until = now_utc() + timedelta(hours=24)
        until += timedelta(minutes=LOCKOUT_BUFFER_MIN)
        record = {
            "until_utc": until.isoformat(timespec="seconds"),
            "until_et": until.astimezone(ET).strftime("%Y-%m-%d %H:%M ET"),
            "detected_utc": now_utc().isoformat(timespec="seconds"),
            "where": where,
            # calibration data: what we had sent when VSiN drew the line
            "automated_requests_last_1h": usage(hours=1) + _RUN.total(),
            "automated_requests_last_24h": usage(hours=24) + _RUN.total(),
        }
        try:
            GUARD_DIR.mkdir(parents=True, exist_ok=True)
            LOCKOUT_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  could not write {LOCKOUT_FILE}: {e}")
        msg = (f"[{where}] VSiN rate-limited this account until {record['until_et']} "
               f"(incl. {LOCKOUT_BUFFER_MIN}m buffer). Stopping all requests. "
               f"Usage at detection: {record['automated_requests_last_1h']}/1h, "
               f"{record['automated_requests_last_24h']}/24h automated.")
        print(f"::error title=VSiN rate limit::{msg}")
        print(f"  *** {msg} ***")
        super().__init__(EXIT_RATE_LIMITED)


def check_rate_limited(text: str, where: str = ""):
    if is_rate_limited_text(text):
        raise VSiNRateLimited(text, where)


# ---------------------------------------------------------------- ledger

def usage(hours: float = 24) -> int:
    """Automated requests recorded in the last `hours` (closed runs only)."""
    cutoff = now_utc() - timedelta(hours=hours)
    total = 0
    if not LEDGER_DIR.exists():
        return 0
    for p in LEDGER_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            end = datetime.fromisoformat(d["end_utc"])
        except Exception:
            continue
        if end >= cutoff:
            total += int(d.get("pages", 0)) + int(d.get("history", 0))
    return total


def _prune_ledger(days: int = 7):
    cutoff = now_utc() - timedelta(days=days)
    for p in LEDGER_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if datetime.fromisoformat(d["end_utc"]) < cutoff:
                p.unlink()
        except Exception:
            pass


class _Run:
    def __init__(self):
        self.job = ""
        self.sport = ""
        self.start = None
        self.counts = {"pages": 0, "history": 0}
        self._last = 0.0
        self._started = False

    def total(self) -> int:
        return sum(self.counts.values())

    def flush(self):
        if not self._started or self.total() == 0:
            return
        try:
            LEDGER_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{self.start:%Y%m%dT%H%M%S}_{self.job}_{self.sport or 'batch'}_{os.getpid()}.json"
            (LEDGER_DIR / name).write_text(json.dumps({
                "job": self.job, "sport": self.sport,
                "start_utc": self.start.isoformat(timespec="seconds"),
                "end_utc": now_utc().isoformat(timespec="seconds"),
                **self.counts,
            }), encoding="utf-8")
            _prune_ledger()
        except Exception as e:
            print(f"  could not write ledger: {e}")
        print(f"[vsin_guard] {self.job}/{self.sport or 'batch'}: "
              f"{self.counts['pages']} page loads + {self.counts['history']} history "
              f"calls this run; rolling 24h now {usage(24)}/{DAILY_BUDGET}")
        self._started = False


_RUN = _Run()


def start_run(job: str, sport: str = ""):
    """Register this process. Exits early if locked out. Flushes on exit."""
    exit_if_locked_out(job)
    _RUN.job, _RUN.sport, _RUN.start = job, sport, now_utc()
    _RUN._started = True
    atexit.register(_RUN.flush)
    used24, used1 = usage(24), usage(1)
    print(f"[vsin_guard] budget: {used24}/{DAILY_BUDGET} used in 24h, "
          f"{used1}/{HOURLY_BUDGET} in 1h; per-sport cap {SPORT_CAP}; "
          f"gap {MIN_GAP}-{MIN_GAP + JITTER}s")


def remaining() -> int:
    """Requests this process may still make."""
    used = _RUN.total()
    return max(0, min(DAILY_BUDGET - usage(24) - used,
                      HOURLY_BUDGET - usage(1) - used,
                      SPORT_CAP - used))


def note(kind: str = "pages"):
    """Record one request to VSiN. kind: 'pages' (driver.get) or 'history'."""
    _RUN.counts[kind] = _RUN.counts.get(kind, 0) + 1


def pace():
    """Sleep so consecutive requests are >= MIN_GAP (+ jitter) apart."""
    gap = MIN_GAP + random.uniform(0, JITTER)
    wait = _RUN._last + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _RUN._last = time.monotonic()
