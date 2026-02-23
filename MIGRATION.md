# Scraper Migration Plan

## Objective
Reduce compute/memory/resource usage on GitHub Actions while preserving (and improving) the historical line + handle dataset for backtesting.

## Guiding Principle
Never change how data is **collected** and how data is **stored** in the same step.

---

## Phase 1 — Trim the Fat *(subtract only, no format changes)*
**Branch:** `phase1-trim`
**Status:** 🟡 IN PROGRESS
**Risk:** Low — pure removal, data format unchanged

### Changes
- [x] Remove `dk_network` and `sbd` from default sources (keep code for optional `--source` use)
- [x] Remove per-scrape snapshot file writes (`data/snapshots/`)
- [x] Gate raw HTML saving behind parse failures only (0 games returned)
- [x] Remove master file append (`data/{sport}_master.csv`)
- [x] Add driver recycling every N scrapes to fix the 3-hour gap bug

### What stays the same
- Timeseries CSV format and path (`data/timeseries/{sport}_{date}.csv`)
- Closing line files (`data/closing/{sport}_{date}.csv`)
- All column names and computed fields (COLUMNS list unchanged)
- Delta computation and sharp signal logic
- ESPN schedule integration for closing line capture

### Acceptance criteria
- [ ] Run one full gameday cycle on the branch — confirm timeseries CSVs are identical in format to current output
- [ ] Confirm no 3-hour gaps in scrape timestamps
- [ ] Verify GitHub Actions run completes without Selenium memory issues
- [ ] Spot-check: closing lines still captured correctly

### How to validate
```bash
# Compare column headers
head -1 data/timeseries/cbb_YYYYMMDD.csv  # should match COLUMNS list exactly

# Check scrape frequency (should see ~30 min intervals, no 3hr gaps)
cut -d',' -f1 data/timeseries/cbb_YYYYMMDD.csv | sort -u

# Confirm closing files written
ls data/closing/
```

---

## Phase 2 — API Migration *(replace Selenium with HTTP for VSiN)*
**Branch:** `phase2-api`
**Status:** ⬜ NOT STARTED
**Risk:** Medium — transport layer change, parser may need adjustment

### Changes
- [ ] Run API endpoint discovery for VSiN DraftKings and Circa
- [ ] Replace `fetch_vsin()` with direct HTTP requests to the JSON API
- [ ] Adapt `parse_vsin()` to consume JSON instead of HTML (if API returns structured data)
- [ ] Keep Selenium as fallback (`--use-browser` flag) for debugging
- [ ] Remove all Selenium sleep/wait logic for VSiN sources
- [ ] Reduce Chrome options and driver management code

### Acceptance criteria
- [ ] Run both old (Phase 1) and new (Phase 2) in parallel for one full day
- [ ] Diff timeseries output — same games, same data, same columns
- [ ] GitHub Actions run time reduced by 50%+ vs Phase 1
- [ ] No Selenium dependency required for default operation

### How to validate
```bash
# Parallel run: old branch saves to data/timeseries-old/, new to data/timeseries/
# Diff the outputs for the same sport + date
diff <(cut -d',' -f5-25 data/timeseries-old/cbb_YYYYMMDD.csv | sort) \
     <(cut -d',' -f5-25 data/timeseries/cbb_YYYYMMDD.csv | sort)
```

---

## Phase 3 — Archive Storage *(Parquet for backtesting)*
**Branch:** `phase3-parquet`
**Status:** ⬜ NOT STARTED
**Risk:** Low — additive, doesn't touch live pipeline

### Changes
- [ ] Add nightly rollup job: convert completed timeseries CSV → Parquet
- [ ] Store Parquet files in `data/archive/{sport}/{sport}_{date}.parquet`
- [ ] Add `--backtest-export` flag to produce a single merged Parquet per sport
- [ ] Add `--analyze` upgrade to read from Parquet archive
- [ ] Optionally delete timeseries CSVs older than N days

### Architecture
```
Live (today):     data/timeseries/cbb_20260222.csv    ← append during day
Archive (done):   data/archive/cbb/cbb_20260221.parquet  ← compressed, columnar
Closing:          data/closing/cbb_20260222.csv       ← unchanged
Backtest export:  data/backtest/cbb_master.parquet    ← on-demand merge
```

### Acceptance criteria
- [ ] Parquet files readable with `pd.read_parquet()` and columns match COLUMNS
- [ ] File size ~10x smaller than equivalent CSV
- [ ] Backtest query: "all games where spread_handle_divergence > 15" runs in <2s across full season

---

## Future Considerations
- **SQLite option:** If query patterns get complex, consider SQLite instead of/alongside Parquet
- **Incremental backfill:** Script to convert existing CSV master files into Parquet archive
- **Dashboard integration:** Live CSV tier feeds real-time dashboard; Parquet feeds analytical queries
- **Source re-addition:** If SBD or DK Network improve their data quality, re-enable with `--source` flag

---

*Last updated: 2026-02-22*
*To discuss next phase: paste this file into a new Claude conversation*
