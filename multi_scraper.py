#!/usr/bin/env python3
"""
Multi-Source Betting Splits Scraper (v2 - Expanded Leagues)
=============================================================
Sources:
  1. VSiN / DraftKings splits  (data.vsin.com)
  2. VSiN / Circa splits        (data.vsin.com)
  3. DK Network splits          (dknetwork.draftkings.com)
  4. SportsBettingDime           (sportsbettingdime.com)

Supported Sports:
  Major:  nfl, nba, nhl, mlb, cfb, cbb
  Other:  wcbb (Women's College Basketball), cbase (College Baseball),
          chockey (College Hockey - Men's)
  Soccer: epl (English Premier League), ucl (Champions League)

Setup:
  pip install selenium pandas beautifulsoup4 schedule

Usage:
  python multi_scraper.py --sport nba --once          # Single scrape, all sources
  python multi_scraper.py --sport nfl --schedule       # Every 5 min, all sources
  python multi_scraper.py --sport cbb --once           # College basketball (men's)
  python multi_scraper.py --sport wcbb --once          # Women's college basketball
  python multi_scraper.py --sport cbase --once         # College baseball
  python multi_scraper.py --sport nhl --source vsin_dk # Single source only
  python multi_scraper.py --sport nfl --close          # Pre-game closing snapshot

  # Gameday mode: two-tier schedule with auto closing lines
  python multi_scraper.py --batch cbb wcbb --gameday
  python multi_scraper.py --batch cbb wcbb --gameday --early-start 07:30 --switch-time 11:30 --end-time 23:00
  python multi_scraper.py --batch cbb wcbb --gameday --early-interval 60 --late-interval 30
  python multi_scraper.py --batch cbb wcbb --gameday --timezone US/Eastern

  python multi_scraper.py --analyze nba                # Analyze master data
  python multi_scraper.py --find-api                   # Discover JSON API endpoints
  python multi_scraper.py --list-sports                # Show all supported sports
"""

import argparse, csv, json, os, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import pandas as pd
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Install: pip install pandas beautifulsoup4 selenium schedule")

# ============================================================
# TIMEZONE CONFIGURATION
# ============================================================
# All date logic uses Eastern Time by default. This ensures file names,
# game_ids, and closing-line timestamps align with the US sports calendar
# regardless of where the scraper runs (e.g. UTC GitHub Actions runners).

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("US/Eastern")


def now_eastern() -> datetime:
    """Return the current time in the configured timezone (US/Eastern)."""
    return datetime.now(DEFAULT_TZ)


def today_str() -> str:
    """Return today's date as YYYYMMDD in Eastern Time."""
    return now_eastern().strftime("%Y%m%d")


# ============================================================
# SPORT DEFINITIONS
# ============================================================
# Each sport has its own slug mapping per source.
# VSiN "main" sports use: data.vsin.com/{slug}/betting-splits/
# VSiN "other league" sports use: data.vsin.com/draftkings/betting-splits/?view={view_code}
# Circa uses the same main sports but adds ?bookid=circa
# "Other league" sports on Circa: data.vsin.com/circa/betting-splits/?view={view_code}

SPORT_INFO = {
    # --- Major Leagues ---
    "nfl": {
        "display": "NFL",
        "category": "major",
        "vsin_slug": "nfl",
        "sbd_slug": "nfl",
        "dk_network": True,
        "dk_dropdown_label": "NFL",
        "season": "Sep-Feb",
        "gamecode_league": "NFL",
    },
    "nba": {
        "display": "NBA",
        "category": "major",
        "vsin_slug": "nba",
        "sbd_slug": "nba",
        "dk_network": True,
        "dk_dropdown_label": "NBA",
        "season": "Oct-Jun",
        # VSiN's data-gamecode embeds the league marker (e.g. "20260518NBA00067").
        # Used to filter out cross-league rows that occasionally appear on the page.
        "gamecode_league": "NBA",
    },
    "wnba": {
        "display": "WNBA",
        "category": "major",
        "vsin_slug": "wnba",
        "sbd_slug": "wnba",         # SBD has /wnba/public-betting-trends/
        "dk_network": False,        # DK Network dropdown doesn't include WNBA
        "season": "May-Oct",
        # WNBA shares the page layout with NBA; gamecode marker is "WNBA"
        # which lets us reject any NBA rows the page might also show.
        "gamecode_league": "WNBA",
    },
    "nhl": {
        "display": "NHL",
        "category": "major",
        "vsin_slug": "nhl",
        "sbd_slug": "nhl",
        "dk_network": True,
        "dk_dropdown_label": "NHL",
        "season": "Oct-Jun",
        "gamecode_league": "NHL",
    },
    "mlb": {
        "display": "MLB",
        "category": "major",
        "vsin_slug": "mlb",
        "sbd_slug": "mlb",
        "dk_network": True,
        "dk_dropdown_label": "MLB",
        "season": "Mar-Oct",
        "gamecode_league": "MLB",
    },
    "cfb": {
        "display": "College Football",
        "category": "major",
        "vsin_slug": "college-football",
        "sbd_slug": "college-football",
        "dk_network": True,
        "dk_dropdown_label": "NCAA Football",
        "season": "Aug-Jan",
        "gamecode_league": "NCAAF",
    },
    "cbb": {
        "display": "College Basketball (Men's)",
        "category": "major",
        "vsin_slug": "college-basketball",
        "sbd_slug": "college-basketball",
        "dk_network": True,
        "dk_dropdown_label": "NCAA Basketball",
        "season": "Nov-Apr",
        "gamecode_league": "NCAAB",
    },
    # --- Other Leagues (VSiN dropdown) ---
    "wcbb": {
        "display": "College Basketball (Women's)",
        "category": "other",
        "vsin_view": "wcbb",
        "sbd_slug": None,          # SBD does not have WCBB splits page
        "dk_network": False,
        "season": "Nov-Apr",
    },
    "cbase": {
        "display": "College Baseball",
        "category": "other",
        "vsin_view": "cbase",
        "sbd_slug": None,          # SBD does not have college baseball splits
        "dk_network": False,
        "season": "Feb-Jun",
    },
    "chockey": {
        "display": "College Hockey (Men's)",
        "category": "other",
        "vsin_view": "chockey",
        "sbd_slug": None,          # SBD does not have college hockey splits
        "dk_network": False,
        "season": "Oct-Apr",
    },
    # --- Soccer Leagues (VSiN soccer dropdown) ---
    "epl": {
        "display": "English Premier League",
        "category": "soccer",
        "vsin_view": "soc518",
        "sbd_slug": None,          # SBD does not have EPL splits page
        "dk_network": False,
        "season": "Aug-May",
    },
    "ucl": {
        "display": "Champions League",
        "category": "soccer",
        "vsin_view": "soc550",
        "sbd_slug": None,          # SBD does not have UCL splits page
        "dk_network": False,
        "season": "Sep-Jun",
    },
}

ALL_SPORTS = list(SPORT_INFO.keys())


# ============================================================
# SOURCE DEFINITIONS
# ============================================================
# URL builders per source. Each returns a URL given a sport key,
# or None if that source doesn't support the sport.

def _vsin_dk_url(sport: str) -> str | None:
    info = SPORT_INFO.get(sport)
    if not info:
        return None
    if info["category"] == "major":
        # Sport-specific pages default to DraftKings book
        return f"https://data.vsin.com/{info['vsin_slug']}/betting-splits/"
    elif info["category"] in ("other", "soccer") and "vsin_view" in info:
        # "Other" and "Soccer" sports use the base betting-splits page with bookid + view params
        return f"https://data.vsin.com/betting-splits/?bookid=dk&view={info['vsin_view']}"
    return None


def _vsin_circa_url(sport: str) -> str | None:
    info = SPORT_INFO.get(sport)
    if not info:
        return None
    if info["category"] == "major":
        # Sport-specific page with Circa book selection
        return f"https://data.vsin.com/{info['vsin_slug']}/betting-splits/?bookid=circa"
    elif info["category"] in ("other", "soccer") and "vsin_view" in info:
        # "Other" sports: base URL with BOTH bookid=circa AND view param
        # This is the key fix â€” /circa/betting-splits/ was redirecting and
        # losing the book selection, producing DK data for both sources
        return f"https://data.vsin.com/betting-splits/?bookid=circa&view={info['vsin_view']}"
    return None


def _dk_network_url(sport: str) -> str | None:
    info = SPORT_INFO.get(sport)
    if not info or not info.get("dk_network"):
        return None
    # DK Network uses a single page; sport is selected via tab/JS
    # The base URL loads with NFL by default; other sports may need interaction
    return "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"


def _sbd_url(sport: str) -> str | None:
    info = SPORT_INFO.get(sport)
    if not info or not info.get("sbd_slug"):
        return None
    return f"https://www.sportsbettingdime.com/{info['sbd_slug']}/public-betting-trends/"


SOURCES = {
    "vsin_dk": {
        "name": "VSiN (DraftKings)",
        "book": "draftkings",
        "url_fn": _vsin_dk_url,
    },
    "vsin_circa": {
        "name": "VSiN (Circa)",
        "book": "circa",
        "url_fn": _vsin_circa_url,
    },
    "dk_network": {
        "name": "DK Network",
        "book": "draftkings",
        "url_fn": _dk_network_url,
    },
    "sbd": {
        "name": "SportsBettingDime",
        "book": "multi_book_consensus",
        "url_fn": _sbd_url,
    },
}


# ============================================================
# DATA DIRECTORIES
# ============================================================

DATA_DIR = Path("data")
for d in ["closing", "raw_html", "timeseries"]:
    (DATA_DIR / d).mkdir(parents=True, exist_ok=True)

COLUMNS = [
    "timestamp", "source", "book", "sport", "game_id",
    "game_date", "game_time",
    "away_team", "home_team",
    "spread_line",
    "spread_away_bets_pct", "spread_home_bets_pct",
    "spread_away_handle_pct", "spread_home_handle_pct",
    "total_line",
    "total_over_bets_pct", "total_under_bets_pct",
    "total_over_handle_pct", "total_under_handle_pct",
    "ml_away_bets_pct", "ml_home_bets_pct",
    "ml_away_handle_pct", "ml_home_handle_pct",
    # Derived signals (static â€” single-scrape)
    "spread_bets_handle_divergence",
    "total_bets_handle_divergence",
    "sharp_signal_spread",
    "sharp_signal_total",
    # Time-series tracking
    "scrape_num",
    # Deltas from previous scrape (same game_id + source)
    "d_spread_line",
    "d_spread_away_bets_pct", "d_spread_home_bets_pct",
    "d_spread_away_handle_pct", "d_spread_home_handle_pct",
    "d_total_line",
    "d_total_over_bets_pct", "d_total_under_bets_pct",
    "d_total_over_handle_pct", "d_total_under_handle_pct",
    "d_ml_away_bets_pct", "d_ml_home_bets_pct",
]

# Fields for which we compute deltas
DELTA_FIELDS = [
    "spread_line",
    "spread_away_bets_pct", "spread_home_bets_pct",
    "spread_away_handle_pct", "spread_home_handle_pct",
    "total_line",
    "total_over_bets_pct", "total_under_bets_pct",
    "total_over_handle_pct", "total_under_handle_pct",
    "ml_away_bets_pct", "ml_home_bets_pct",
]


# ============================================================
# SELENIUM DRIVER
# ============================================================

def get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--headless=new")          # Modern headless mode (more stable)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--mute-audio")
    opts.add_argument("--no-first-run")
    opts.add_argument("--safebrowsing-disable-auto-update")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")

    # Block images, fonts, and media to reduce memory/bandwidth
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.media_stream": 2,
        "profile.default_content_setting_values.notifications": 2,
    }
    opts.add_experimental_option("prefs", prefs)

    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # Try webdriver-manager first (easiest), then system paths
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
    except ImportError:
        driver = None
        for path in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver", None]:
            try:
                svc = Service(executable_path=path) if path else None
                driver = webdriver.Chrome(service=svc, options=opts) if svc else webdriver.Chrome(options=opts)
                break
            except Exception:
                continue
        if driver is None:
            raise RuntimeError(
                "chromedriver not found.\n"
                "  Easiest fix: pip install webdriver-manager\n"
                "  Or install manually: sudo apt-get install chromium-chromedriver"
            )

    # Set timeouts to prevent infinite hangs
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(20)

    # Block heavy third-party domains (ads, trackers) via CDP
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
            "*.doubleclick.net/*",
            "*.googlesyndication.com/*",
            "*.googleadservices.com/*",
            "*.google-analytics.com/*",
            "*.facebook.net/*",
            "*.facebook.com/tr*",
            "*.hotjar.com/*",
            "*.optimizely.com/*",
            "*.segment.com/*",
            "*.chartbeat.com/*",
            "*.scorecardresearch.com/*",
            "*.quantserve.com/*",
            "*.amazon-adsystem.com/*",
            "*.taboola.com/*",
            "*.outbrain.com/*",
            "*.piano.io/*",
            "*.tinypass.com/*",
        ]})
    except Exception:
        pass  # CDP commands may not be available in all Chrome versions

    return driver


# ============================================================
# SCRAPING CORE
# ============================================================

def fetch_page(url: str, driver, wait_seconds: int = 6, **kwargs) -> str:
    """Load a JS-rendered page and return HTML."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    try:
        driver.get(url)
    except Exception as e:
        # Page load timeout â€” page may still have partial content
        if "timeout" in str(e).lower():
            print(f"  Page load timed out, attempting to use partial content...")
            driver.execute_script("window.stop();")  # Stop loading
        else:
            raise

    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located(
            (By.CSS_SELECTOR,
             "table.freezetable, table, [class*='split'], [class*='game'], "
             "[class*='matchup'], [class*='event']")
        ))
    except Exception:
        pass
    time.sleep(wait_seconds)
    return driver.page_source


def fetch_vsin(url: str, driver, sport: str, source_key: str = "", wait_seconds: int = 4) -> str:
    """
    Fetch VSiN betting splits page with proper book + tab/view handling.

    VSiN's page has two independent JS controls:
    1. Book selector (DraftKings vs Circa) â€” controlled by ?bookid= param
    2. Sport tabs (for "other" sports like WCBB) â€” controlled by ?view= param

    Both are client-side JS. We verify both after page load.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    info = SPORT_INFO.get(sport, {})
    is_circa = "circa" in source_key or "bookid=circa" in url

    try:
        driver.get(url)
    except Exception as e:
        if "timeout" in str(e).lower():
            print(f"  [vsin] Page load timed out, using partial content...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        else:
            raise

    # Wait for the splits table to appear (new sp-table or legacy freezetable)
    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "table.sp-table, tr.sp-row, table.freezetable, table")
        ))
    except Exception:
        pass

    time.sleep(3)  # Let tabs and book selector initialize

    # --- Verify/switch book (DK vs Circa) ---
    if is_circa:
        _vsin_ensure_book(driver, "circa", source_key)

    # --- For "other" league sports, verify the correct sport tab is selected ---
    if info.get("category") == "other" and "vsin_view" in info:
        view_code = info["vsin_view"]
        _vsin_ensure_tab(driver, sport, view_code)
        time.sleep(wait_seconds)  # Wait for table data to reload
    else:
        time.sleep(wait_seconds)

    return driver.page_source


def _vsin_ensure_book(driver, book: str, source_key: str = ""):
    """Ensure the correct book (DraftKings vs Circa) is selected on VSiN page.
    The page has a DraftKings/Circa toggle near the top. Both names are always
    visible, so we must click the correct one rather than checking page text."""
    from selenium.webdriver.common.by import By

    target_texts = ["circa"] if book == "circa" else ["draftkings"]

    # Strategy 1: Find and click the book toggle link/tab
    try:
        for sel in ["a", "button", "span", "li", "[role='tab']",
                     "[class*='tab']", "[class*='book']", "[class*='toggle']"]:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                el_text = el.text.strip().lower()
                # Match the book name exactly or as a standalone word
                # Must be short text to avoid nav menu items
                if not el_text or len(el_text) > 40:
                    continue
                for target in target_texts:
                    if el_text == target or (target in el_text.split() and len(el_text) < 20):
                        # Check if this element looks like it's already active/selected
                        classes = (el.get_attribute("class") or "").lower()
                        is_active = any(c in classes for c in ["active", "selected", "current", "on"])
                        if is_active:
                            print(f"  [{source_key}] Book '{el.text.strip()}' already active.")
                            return

                        try:
                            el.click()
                            print(f"  [{source_key}] Clicked book toggle: '{el.text.strip()}'")
                            time.sleep(4)  # Wait for data to fully reload
                            return
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", el)
                                print(f"  [{source_key}] JS-clicked book toggle: '{el.text.strip()}'")
                                time.sleep(4)
                                return
                            except Exception:
                                continue
    except Exception:
        pass

    # Strategy 2: Try URL-based approach â€” navigate to the book-specific URL
    try:
        current_url = driver.current_url
        if book == "circa" and "bookid=circa" not in current_url:
            if "bookid=dk" in current_url:
                new_url = current_url.replace("bookid=dk", "bookid=circa")
            elif "bookid=" not in current_url:
                sep = "&" if "?" in current_url else "?"
                new_url = current_url + f"{sep}bookid=circa"
            else:
                new_url = current_url
            if new_url != current_url:
                driver.get(new_url)
                time.sleep(5)
                print(f"  [{source_key}] Reloaded page with bookid=circa")
                return
    except Exception:
        pass

    print(f"  [{source_key}] WARNING: Could not switch to {book} book. "
          f"Data may be from the default book (DraftKings).")


def _vsin_ensure_tab(driver, sport: str, view_code: str):
    """
    Ensure the correct VSiN tab/view is active for an 'other' league sport.
    Tries multiple strategies to switch to the right view.
    """
    from selenium.webdriver.common.by import By

    info = SPORT_INFO.get(sport, {})
    display = info.get("display", sport)

    # Map view codes to likely tab label text
    tab_labels = {
        "wcbb": ["women", "wcbb", "women's", "college basketball - women",
                  "w. college basketball", "ncaa women", "wbb",
                  "women's college", "w college basketball"],
        "cbase": ["baseball", "cbase", "college baseball", "ncaa baseball"],
        "chockey": ["hockey", "chockey", "college hockey", "ncaa hockey",
                     "men's college hockey", "m. college hockey", "ice hockey",
                     "college ice hockey"],
    }
    target_texts = tab_labels.get(view_code, [view_code])

    # Strategy 1: Look for a dropdown/select that controls the sport view
    try:
        from selenium.webdriver.support.ui import Select
        selects = driver.find_elements(By.TAG_NAME, "select")
        for sel_el in selects:
            options = sel_el.find_elements(By.TAG_NAME, "option")
            for opt in options:
                opt_text = opt.text.strip().lower()
                if any(t in opt_text for t in target_texts):
                    Select(sel_el).select_by_visible_text(opt.text.strip())
                    print(f"  [vsin] Switched to '{opt.text.strip()}' via <select> dropdown")
                    return True
    except Exception:
        pass

    # Strategy 2: Look for clickable tabs/links with matching text
    try:
        clickable_selectors = [
            "a", "button", "li", "span[class*='tab']", "div[class*='tab']",
            "[role='tab']", "[class*='league']", "[class*='sport']",
            "nav a", "nav li", ".nav-item", ".nav-link",
            "[class*='dropdown-item']", "[class*='menu-item']",
        ]
        for sel in clickable_selectors:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                el_text = el.text.strip().lower()
                if any(t in el_text for t in target_texts):
                    try:
                        el.click()
                        print(f"  [vsin] Clicked tab: '{el.text.strip()}'")
                        time.sleep(3)
                        return True
                    except Exception:
                        # Try JS click as fallback
                        try:
                            driver.execute_script("arguments[0].click();", el)
                            print(f"  [vsin] JS-clicked tab: '{el.text.strip()}'")
                            time.sleep(3)
                            return True
                        except Exception:
                            continue
    except Exception:
        pass

    # Strategy 3: Try manipulating the URL hash/fragment which some
    # VSiN pages use to control the active view
    try:
        current_url = driver.current_url
        if f"view={view_code}" not in current_url:
            # Navigate directly to the URL with the view parameter
            if "?" in current_url:
                new_url = current_url + f"&view={view_code}"
            else:
                new_url = current_url + f"?view={view_code}"
            driver.get(new_url)
            time.sleep(5)
            print(f"  [vsin] Reloaded with ?view={view_code}")
            return True  # Can't truly confirm, but worth trying
    except Exception:
        pass

    # Strategy 4: Try executing JS to find and trigger the view switch
    # VSiN's widget may expose a JS API or use data attributes
    try:
        js_attempts = [
            f"document.querySelector('[data-view=\"{view_code}\"]')?.click()",
            f"document.querySelector('[data-league=\"{view_code}\"]')?.click()",
            f"document.querySelector('[href*=\"{view_code}\"]')?.click()",
        ]
        for js in js_attempts:
            result = driver.execute_script(f"return {js}")
            if result is not None:
                print(f"  [vsin] Triggered view switch via JS: {view_code}")
                time.sleep(3)
                return True
    except Exception:
        pass

    print(f"  [vsin] WARNING: Could not switch to '{display}' (view={view_code}). "
          f"Tab switch failed â€” parser will validate before saving.")
    return False


def fetch_dk_network(url: str, driver, sport: str, wait_seconds: int = 5) -> str:
    """
    Fetch DK Network splits page with sport selection via dropdown click.
    The DK Network page uses a single URL for all sports, with a JS-powered
    dropdown to switch between them. We must interact with the dropdown
    to load the correct sport's data.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException

    info = SPORT_INFO.get(sport, {})
    target_label = info.get("dk_dropdown_label", "")

    try:
        driver.get(url)
    except Exception as e:
        if "timeout" in str(e).lower():
            print(f"  [dk_network] Page load timed out, using partial content...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        else:
            raise

    time.sleep(3)  # Let the page and its embedded widget load

    # The DK Network page uses an embedded Tablebuilder widget.
    # The sport dropdown is typically a <select> or custom dropdown.
    # Strategy: try multiple approaches to select the sport.

    if target_label and target_label != "NFL":  # NFL is the default
        selected = False

        # --- Approach 1: Look for a <select> element with sport options ---
        try:
            from selenium.webdriver.support.ui import Select
            selects = driver.find_elements(By.TAG_NAME, "select")
            for sel_el in selects:
                options_text = [o.text.strip() for o in sel_el.find_elements(By.TAG_NAME, "option")]
                if any(target_label.lower() in o.lower() for o in options_text):
                    select = Select(sel_el)
                    for opt in sel_el.find_elements(By.TAG_NAME, "option"):
                        if target_label.lower() in opt.text.strip().lower():
                            select.select_by_visible_text(opt.text.strip())
                            print(f"  [dk_network] Selected sport via <select>: {opt.text.strip()}")
                            selected = True
                            break
                if selected:
                    break
        except Exception as e:
            print(f"  [dk_network] <select> approach failed: {e}")

        # --- Approach 2: Click-based custom dropdown (common in React/Vue widgets) ---
        if not selected:
            try:
                # Look for dropdown triggers containing "Sports" or the current sport name
                dropdowns = driver.find_elements(
                    By.CSS_SELECTOR,
                    "[class*='dropdown'], [class*='select'], [class*='filter'], "
                    "[role='listbox'], [role='combobox'], button[class*='sport']"
                )
                # Also try any element that currently shows "NFL" (the default)
                all_clickable = driver.find_elements(
                    By.XPATH,
                    "//*[contains(text(), 'NFL') and (self::button or self::div or self::span or self::a)]"
                )
                dropdowns.extend(all_clickable)

                for dd in dropdowns:
                    try:
                        dd.click()
                        time.sleep(1)
                        # Now look for the target sport option
                        options = driver.find_elements(
                            By.XPATH,
                            f"//*[contains(text(), '{target_label}')]"
                        )
                        for opt in options:
                            if opt.is_displayed() and opt.text.strip():
                                opt.click()
                                print(f"  [dk_network] Selected sport via dropdown click: {target_label}")
                                selected = True
                                break
                        if selected:
                            break
                    except Exception:
                        continue
            except Exception as e:
                print(f"  [dk_network] Dropdown click approach failed: {e}")

        # --- Approach 3: Try iframes (DK embeds content in iframes sometimes) ---
        if not selected:
            try:
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for iframe in iframes:
                    try:
                        driver.switch_to.frame(iframe)
                        # Look for select elements inside iframe
                        from selenium.webdriver.support.ui import Select
                        selects = driver.find_elements(By.TAG_NAME, "select")
                        for sel_el in selects:
                            options_text = [o.text.strip() for o in sel_el.find_elements(By.TAG_NAME, "option")]
                            if any(target_label.lower() in o.lower() for o in options_text):
                                select = Select(sel_el)
                                for opt in sel_el.find_elements(By.TAG_NAME, "option"):
                                    if target_label.lower() in opt.text.strip().lower():
                                        select.select_by_visible_text(opt.text.strip())
                                        print(f"  [dk_network] Selected sport in iframe via <select>: {opt.text.strip()}")
                                        selected = True
                                        break
                            if selected:
                                break
                        driver.switch_to.default_content()
                        if selected:
                            break
                    except Exception:
                        driver.switch_to.default_content()
                        continue
            except Exception as e:
                print(f"  [dk_network] iframe approach failed: {e}")

        if not selected:
            print(f"  [dk_network] WARNING: Could not select sport '{target_label}' â€” data may be for wrong sport (NFL default)")

        # Wait for data to reload after sport switch
        time.sleep(wait_seconds)
    else:
        time.sleep(wait_seconds)

    return driver.page_source


def fetch_sbd(url: str, driver, wait_seconds: int = 6) -> str:
    """
    Fetch SportsBettingDime page with extended wait for JS-rendered content.
    SBD loads game data dynamically via JavaScript framework â€” we need to wait
    longer and look for SBD-specific container elements.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    try:
        driver.get(url)
    except Exception as e:
        if "timeout" in str(e).lower():
            print(f"  [sbd] Page load timed out, using partial content...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        else:
            raise

    # SBD uses various selectors for their betting trends data
    sbd_selectors = [
        "[class*='trend']",
        "[class*='betting']",
        "[class*='matchup']",
        "[class*='game']",
        "[class*='event']",
        "[class*='consensus']",
        "table",
        "[class*='card']",
        "[data-sport]",
        "[data-game]",
    ]

    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, ", ".join(sbd_selectors))
        ))
    except Exception:
        pass

    # Wait for SBD's JS framework to populate the DOM
    time.sleep(wait_seconds)

    # Scroll to trigger lazy-loaded content
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
    except Exception:
        pass

    return driver.page_source


# -------------------------------------------------------
# VSiN-SPECIFIC PARSER (data.vsin.com)
# -------------------------------------------------------
# Two layouts are supported:
#
# (1) NEW (May 2026+): <table class="sp-table">
#     - One <tr class="sp-row"> per TEAM (so 2 rows per game)
#     - Rows paired by data-gamecode attribute (e.g. "20260515NBA00067")
#     - Date in <thead> as "<League> - Friday, May 15"
#     - Each row has 11 <td>s. Column layout:
#         td[0]: action button (also carries data-gamecode)
#         td[1]: team logo + <a class="sp-team-link"> name
#         td[2]: spread line  (<span class="sp-badge sp-badge-line">)
#         td[3]: spread handle %  (<span class="sp-badge">)
#         td[4]: spread bets %
#         td[5]: total line
#         td[6]: total handle %  (row 1 = over, row 2 = under)
#         td[7]: total bets %
#         td[8]: moneyline price
#         td[9]: ML handle %
#         td[10]: ML bets %
#
# (2) LEGACY: <table class="freezetable"> with one row per GAME, 10 tds,
#     percentages paired within single cells. Kept as a fallback.

def parse_vsin(html: str, source_key: str, sport: str) -> list[dict]:
    """Parse VSiN betting splits. Tries new sp-table layout first, then legacy."""
    soup = BeautifulSoup(html, "html.parser")

    # Try the new sp-table layout first
    if soup.find("table", class_="sp-table") or soup.find("tr", class_="sp-row"):
        return _parse_vsin_sp_table(soup, source_key, sport)

    # Fall back to the legacy freezetable parser
    return _parse_vsin_legacy(html, source_key, sport)


def _gamecode_cross_league_check(sport: str):
    """Build a cross-league check for the given target sport.

    Returns a tuple (is_foreign_gamecode, all_markers, own_marker):
      - is_foreign_gamecode(gc): True if `gc` carries a league marker that
        belongs to a DIFFERENT tracked sport than `sport`. False if the
        gamecode matches our own marker, is unrecognized, or is empty.
      - all_markers: set of every gamecode_league string in SPORT_INFO.
      - own_marker: the marker for `sport` (empty if not declared).

    Used by both the new sp-table parser and the legacy freezetable parser
    to reject rows whose data-gamecode shows they came from the wrong
    league (most commonly WNBA rows leaking onto /nba/betting-splits/).
    Conservative: only rejects on POSITIVE evidence (the row's marker is
    a known foreign league). Rows without a recognizable marker pass through.
    """
    info = SPORT_INFO.get(sport, {})
    own_marker = info.get("gamecode_league", "")
    all_markers = {
        s_info["gamecode_league"]
        for s_info in SPORT_INFO.values()
        if s_info.get("gamecode_league")
    }
    # Longest-first so "WNBA" wins over "NBA" when scanning a gamecode prefix.
    sorted_markers = sorted(all_markers, key=len, reverse=True)

    def _extract_marker(gc: str) -> str:
        if not gc:
            return ""
        m = re.match(r"^\d+([A-Z]+)", gc)
        if not m:
            return ""
        alpha = m.group(1)
        for marker in sorted_markers:
            if alpha.startswith(marker):
                return marker
        return ""  # Unrecognized â€” treat as non-foreign

    def is_foreign(gc: str) -> bool:
        marker = _extract_marker(gc)
        if not marker:
            return False                  # No evidence â†’ keep the row
        if own_marker:
            return marker != own_marker   # We know our marker; reject mismatches
        # No own marker declared (e.g. an "other" sport). Reject only if
        # the row is positively identified as belonging to a different sport.
        return marker in all_markers and marker != own_marker

    return is_foreign, all_markers, own_marker


def _parse_vsin_sp_table(soup, source_key: str, sport: str) -> list[dict]:
    """Parse VSiN's new sp-table layout (one row per team, paired by data-gamecode)."""
    now = now_eastern().isoformat()
    src = SOURCES[source_key]
    games = []

    # Cross-league contamination filter (data-gamecode based). See
    # _gamecode_cross_league_check for the full rationale.
    is_foreign_gamecode, _all_markers, _own_marker = _gamecode_cross_league_check(sport)
    rejected_other_league = 0

    MONTHS_RE = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"

    def _num_from(text):
        if not text:
            return ""
        m = re.search(r"[+-]?\d+(?:\.\d+)?", text)
        return float(m.group(0)) if m else ""

    def _pct_from(text):
        if not text:
            return ""
        m = re.search(r"(\d{1,3}(?:\.\d)?)\s*%", text)
        return float(m.group(1)) if m else ""

    def _badge_text(td):
        if td is None:
            return ""
        badge = td.find(class_="sp-badge")
        return (badge or td).get_text(" ", strip=True)

    # Collect all sp-tables; if none, fall back to scanning the whole soup
    tables = soup.find_all("table", class_="sp-table") or [soup]

    for table in tables:
        # Date for this table comes from its <thead>, e.g. "NBA - Friday, May 15"
        current_date = ""
        thead = table.find("thead") if hasattr(table, "find") else None
        if thead is not None:
            m = re.search(rf"({MONTHS_RE})\s+(\d{{1,2}})", thead.get_text(" "))
            if m:
                current_date = f"{m.group(1)} {m.group(2)}"

        rows = table.find_all("tr", class_="sp-row") if hasattr(table, "find_all") else []

        # Group rows by data-gamecode, preserving order
        by_game: dict = {}
        order: list = []
        for row in rows:
            gc_el = row.find(attrs={"data-gamecode": True})
            gamecode = gc_el.get("data-gamecode") if gc_el else None
            if not gamecode:
                continue

            # Cross-league filter: skip rows whose gamecode identifies a
            # different tracked league.
            if is_foreign_gamecode(gamecode):
                rejected_other_league += 1
                continue

            if gamecode not in by_game:
                by_game[gamecode] = []
                order.append(gamecode)
            by_game[gamecode].append(row)

        for gamecode in order:
            pair = by_game[gamecode]
            if len(pair) < 2:
                continue
            away_row, home_row = pair[0], pair[1]

            def _extract(row):
                tds = row.find_all("td")
                if len(tds) < 11:
                    return None
                team_link = row.find(class_="sp-team-link")
                team = team_link.get_text(strip=True) if team_link else ""
                # Strip sport suffixes like [W], [M]
                team = re.sub(r"\s*[\[\(][WMwm][\]\)]\s*$", "", team).strip()
                if not team:
                    return None
                return {
                    "team":        team,
                    "spread_line": _num_from(_badge_text(tds[2])),
                    "spread_hnd":  _pct_from(_badge_text(tds[3])),
                    "spread_bet":  _pct_from(_badge_text(tds[4])),
                    "total_line":  _num_from(_badge_text(tds[5])),
                    "total_hnd":   _pct_from(_badge_text(tds[6])),
                    "total_bet":   _pct_from(_badge_text(tds[7])),
                    "ml_hnd":      _pct_from(_badge_text(tds[9])),
                    "ml_bet":      _pct_from(_badge_text(tds[10])),
                }

            a = _extract(away_row)
            h = _extract(home_row)
            if not a or not h:
                continue

            game = {col: "" for col in COLUMNS}
            game["timestamp"]  = now
            game["source"]     = source_key
            game["book"]       = src["book"]
            game["sport"]      = sport
            game["game_date"]  = current_date
            game["away_team"]  = a["team"]
            game["home_team"]  = h["team"]

            # Spread: store the away team's line (negative = away favored).
            # Matches the legacy parser's convention of one line per game.
            game["spread_line"]             = a["spread_line"]
            game["spread_away_handle_pct"]  = a["spread_hnd"]
            game["spread_home_handle_pct"]  = h["spread_hnd"]
            game["spread_away_bets_pct"]    = a["spread_bet"]
            game["spread_home_bets_pct"]    = h["spread_bet"]

            # Total: both teams' rows show the same total line; over/under split
            # is row1 (away) = over %, row2 (home) = under %.
            game["total_line"]              = a["total_line"]
            game["total_over_handle_pct"]   = a["total_hnd"]
            game["total_under_handle_pct"]  = h["total_hnd"]
            game["total_over_bets_pct"]     = a["total_bet"]
            game["total_under_bets_pct"]    = h["total_bet"]

            # Moneyline
            game["ml_away_handle_pct"] = a["ml_hnd"]
            game["ml_home_handle_pct"] = h["ml_hnd"]
            game["ml_away_bets_pct"]   = a["ml_bet"]
            game["ml_home_bets_pct"]   = h["ml_bet"]

            compute_signals(game)
            games.append(game)

    if rejected_other_league:
        print(f"  [{source_key}] Cross-league filter: rejected "
              f"{rejected_other_league} row(s) whose data-gamecode "
              f"belonged to a different league than {sport.upper()}.")

    return games


def _parse_vsin_legacy(html: str, source_key: str, sport: str) -> list[dict]:
    """Legacy parser for VSiN's old freezetable layout (pre-May 2026)."""
    soup = BeautifulSoup(html, "html.parser")
    now = now_eastern().isoformat()
    src = SOURCES[source_key]
    games = []

    # Cross-league contamination filter. Mirrors the new sp-table parser.
    # The legacy freezetable layout MAY include a data-gamecode somewhere on
    # the row (action button, inner span, etc.) â€” when present we use it to
    # reject foreign-league rows. When absent we keep the row (no evidence).
    is_foreign_gamecode, _all_markers, _own_marker = _gamecode_cross_league_check(sport)
    rejected_other_league = 0

    def _row_gamecode(row) -> str:
        """Search a row for any data-gamecode attribute. Returns '' if none."""
        # Check the row itself first, then any descendant element.
        for el in [row, *row.find_all(attrs={"data-gamecode": True})]:
            gc = el.get("data-gamecode") if hasattr(el, "get") else None
            if gc:
                return gc
        return ""

    table = soup.select_one("table.freezetable")
    if not table:
        # Fallback: try any table
        tables = soup.find_all("table")
        for t in tables:
            if len(t.find_all("tr")) > 3:
                table = t
                break
    if not table:
        return []

    current_date = ""
    rows = table.find_all("tr")

    # Debug: log what the parser sees
    data_rows = [r for r in rows if len(r.find_all("td")) >= 8]
    if not data_rows and rows:
        # Log sample row structure to diagnose column count issues
        sample_rows = rows[:5]
        for i, r in enumerate(sample_rows):
            tds = r.find_all("td")
            ths = r.find_all("th")
            text_preview = r.get_text(" ", strip=True)[:80]
            print(f"  [{source_key}] Debug row {i}: {len(tds)} tds, {len(ths)} ths: {text_preview}")

    for row in rows:
        # Check for date header rows (contain <th> with day names)
        ths = row.find_all("th")
        if ths:
            header_text = row.get_text(strip=True)
            # Extract date like "Friday,Feb 13" or "Saturday,Feb 14"
            date_match = re.search(
                r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
                r'[,\s]*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{1,2})',
                header_text
            )
            if date_match:
                current_date = f"{date_match.group(2)} {date_match.group(3)}"
            continue

        tds = row.find_all("td")
        if len(tds) < 8:
            continue

        # Cross-league filter: if this row carries a data-gamecode and it
        # belongs to a different tracked sport, skip it. Rows without a
        # gamecode pass through to the existing extraction logic.
        gc = _row_gamecode(row)
        if gc and is_foreign_gamecode(gc):
            rejected_other_league += 1
            continue

        # --- Extract team names from td[0] ---
        # Primary: look for links with VSiN's team name class
        team_links = tds[0].select("a.txt-color-vsinred")
        if not team_links:
            # Fallback: any <a> tag in the cell (some tabs use different classes)
            team_links = tds[0].select("a[href]")
        if not team_links:
            # Fallback: any <a> tag at all
            team_links = tds[0].find_all("a")

        teams = []
        for a in team_links:
            name = a.get_text(strip=True)
            # Strip sport suffixes like [W], [M], (W), etc.
            name = re.sub(r'\s*[\[\(][WMwm][\]\)]\s*$', '', name).strip()
            # Filter out non-team text (numbers, "VSiN Pick", short junk)
            if (name and len(name) > 1
                    and not re.match(r'^[+-]?\d', name)
                    and "vsn pick" not in name.lower()
                    and "vsn" not in name.lower()
                    and "pick" not in name.lower()
                    and "history" not in name.lower()
                    and "betting splits" not in name.lower()):
                teams.append(name)

        # If links didn't work, try extracting from raw cell text
        if len(teams) < 2:
            cell_text = tds[0].get_text("\n", strip=True)
            lines = [l.strip() for l in cell_text.split("\n") if l.strip()]
            teams = []
            for line in lines:
                line = re.sub(r'\s*[\[\(][WMwm][\]\)]\s*$', '', line).strip()
                if (line and len(line) > 1
                        and not re.match(r'^[+-]?\d', line)
                        and "betting splits" not in line.lower()
                        and "pick" not in line.lower()):
                    teams.append(line)

        if len(teams) < 2:
            continue

        away_team = teams[0]
        home_team = teams[1]

        # --- Helper to extract two percentages from a cell ---
        def get_pcts(td):
            pcts = re.findall(r'(\d{1,3})%', td.get_text())
            if len(pcts) >= 2:
                return float(pcts[0]), float(pcts[1])
            return None, None

        # --- Helper to extract a line from a cell ---
        def get_line(td):
            nums = re.findall(r'([+-]?\d+(?:\.\d+)?)', td.get_text(" "))
            # Return only the first number (VSiN repeats the line for away/home or over/under)
            if nums:
                return float(nums[0])
            return ""

        # --- Build game record ---
        game = {col: "" for col in COLUMNS}
        game["timestamp"] = now
        game["source"] = source_key
        game["book"] = src["book"]
        game["sport"] = sport
        game["game_date"] = current_date
        game["away_team"] = away_team
        game["home_team"] = home_team

        n = len(tds)

        # Spread line from td[1]
        if n > 1:
            game["spread_line"] = get_line(tds[1])

        # Spread HANDLE % from td[2] (away%, home%)
        if n > 2:
            sh_away, sh_home = get_pcts(tds[2])
            game["spread_away_handle_pct"] = sh_away or ""
            game["spread_home_handle_pct"] = sh_home or ""

        # Spread BETS % from td[3] (away%, home%)
        if n > 3:
            sb_away, sb_home = get_pcts(tds[3])
            game["spread_away_bets_pct"] = sb_away or ""
            game["spread_home_bets_pct"] = sb_home or ""

        # Total line from td[4]
        if n > 4:
            game["total_line"] = get_line(tds[4])

        # Total HANDLE % from td[5] (over%, under%)
        if n > 5:
            th_over, th_under = get_pcts(tds[5])
            game["total_over_handle_pct"] = th_over or ""
            game["total_under_handle_pct"] = th_under or ""

        # Total BETS % from td[6] (over%, under%)
        if n > 6:
            tb_over, tb_under = get_pcts(tds[6])
            game["total_over_bets_pct"] = tb_over or ""
            game["total_under_bets_pct"] = tb_under or ""

        # ML line from td[7] (not stored but could be useful)
        # ML HANDLE % from td[8]
        if n > 8:
            mh_away, mh_home = get_pcts(tds[8])
            game["ml_away_handle_pct"] = mh_away or ""
            game["ml_home_handle_pct"] = mh_home or ""

        # ML BETS % from td[9]
        if n > 9:
            mb_away, mb_home = get_pcts(tds[9])
            game["ml_away_bets_pct"] = mb_away or ""
            game["ml_home_bets_pct"] = mb_home or ""

        # Compute derived signals
        compute_signals(game)
        games.append(game)

    if rejected_other_league:
        print(f"  [{source_key}] Cross-league filter (legacy): rejected "
              f"{rejected_other_league} row(s) whose data-gamecode "
              f"belonged to a different league than {sport.upper()}.")

    return games


# -------------------------------------------------------
# GENERIC PARSER (DK Network, SportsBettingDime, etc.)
# -------------------------------------------------------

def parse_generic(html: str, source_key: str, sport: str) -> list[dict]:
    """
    Generic parser for non-VSiN sources. Uses heuristics to find
    game containers and extract teams + percentages.
    """
    soup = BeautifulSoup(html, "html.parser")
    now = now_eastern().isoformat()
    src = SOURCES[source_key]
    games = []

    # Gather all containers that might hold game data
    containers = soup.select(
        ".game-card, .splits-game, .event-card, [class*='matchup'], "
        "[class*='game-row'], [class*='contest'], [class*='event-row'], "
        "[class*='betting-split'], [class*='public-betting'], "
        "tr[data-game], div[data-event]"
    )

    # Also try table rows
    if not containers:
        for table in soup.find_all("table"):
            containers.extend(table.find_all("tr"))

    # Also try any div with enough numeric content
    if not containers:
        for div in soup.find_all("div"):
            text = div.get_text()
            pcts = re.findall(r'\d{1,3}(?:\.\d)?%', text)
            if len(pcts) >= 4 and len(text) < 1000:
                containers.append(div)

    seen = set()
    for container in containers:
        game = extract_game_generic(container, source_key, sport, now)
        if game:
            key = (game["away_team"], game["home_team"])
            if key not in seen and game["away_team"]:
                seen.add(key)
                games.append(game)

    return games


def extract_game_generic(el, source_key: str, sport: str, timestamp: str) -> dict | None:
    """Extract a single game's splits from an HTML element (generic)."""
    text = el.get_text(" ", strip=True)
    if len(text) < 8:
        return None

    # Find team names
    team_els = el.select(
        ".team-name, .team, [class*='team'], span[class*='name'], "
        "td:first-child, [class*='participant']"
    )
    teams = []
    for t in team_els:
        name = t.get_text(strip=True)
        if name and 1 < len(name) < 50 and not re.match(r'^[\d.%+\-/]+$', name):
            if name.lower() not in {"spread", "total", "moneyline", "ml", "over", "under",
                                     "bets", "handle", "%", "vs", "at", "@", "run line",
                                     "puck line", "game total", "1st half", "2nd half"}:
                teams.append(name)

    # Deduplicate while preserving order
    seen_teams = []
    for t in teams:
        if t not in seen_teams:
            seen_teams.append(t)
    teams = seen_teams[:2]

    if len(teams) < 2:
        return None

    # Extract all percentages
    pcts = [float(p) for p in re.findall(r'(\d{1,3}(?:\.\d)?)%', text)]

    # Extract numeric lines
    numbers = re.findall(r'([+-]?\d+(?:\.\d+)?)', text)
    numbers = [float(n) for n in numbers if 1 < abs(float(n)) < 300]

    src = SOURCES[source_key]
    game = {col: "" for col in COLUMNS}
    game["timestamp"] = timestamp
    game["source"] = source_key
    game["book"] = src["book"]
    game["sport"] = sport
    game["away_team"] = teams[0]
    game["home_team"] = teams[1]

    pct_map = [
        "spread_away_bets_pct", "spread_home_bets_pct",
        "spread_away_handle_pct", "spread_home_handle_pct",
        "total_over_bets_pct", "total_under_bets_pct",
        "total_over_handle_pct", "total_under_handle_pct",
        "ml_away_bets_pct", "ml_home_bets_pct",
        "ml_away_handle_pct", "ml_home_handle_pct",
    ]
    for i, field in enumerate(pct_map):
        if i < len(pcts):
            game[field] = pcts[i]

    for n in numbers:
        if -30 < n < 30 and n != 0 and not game["spread_line"]:
            game["spread_line"] = n
        elif 30 < n < 300 and not game["total_line"]:
            game["total_line"] = n

    compute_signals(game)
    return game


# -------------------------------------------------------
# DK NETWORK-SPECIFIC PARSER
# -------------------------------------------------------
# DK Network renders splits in a specific card-based layout:
#   <h5> Game title (e.g. "Team A @ Team B")
#   Then sections for Moneyline, Spread, Total
#   Each section has rows with team/outcome, odds, % Handle, % Bets

def parse_dk_network(html: str, source_key: str, sport: str) -> list[dict]:
    """Parse DK Network betting splits from their card-based layout."""
    soup = BeautifulSoup(html, "html.parser")
    now = now_eastern().isoformat()
    src = SOURCES[source_key]
    games = []

    # DK Network uses <h5> tags for game titles, or game card containers
    # The structure after each h5 has Moneyline/Spread/Total sections
    # Each section has a header row (Odds / % Handle / % Bets) and data rows

    # Strategy 1: Parse the structured text looking for game patterns
    # The rendered HTML has patterns like:
    #   "Team A\n[odds]\nXX%\nYY%\nTeam B\n[odds]\nXX%\nYY%"

    # Find all h5 elements that look like game titles
    game_headers = soup.find_all("h5")
    game_sections = []

    for h5 in game_headers:
        text = h5.get_text(strip=True)
        # Game titles look like "Team @ Team" or contain date patterns
        if "@" in text or " vs " in text.lower():
            # Collect all sibling content until next h5
            section_html = str(h5)
            for sib in h5.find_next_siblings():
                if sib.name == "h5":
                    break
                section_html += str(sib)
            game_sections.append((text, BeautifulSoup(section_html, "html.parser")))

    # If no h5-based sections found, try a broader approach
    if not game_sections:
        # Look for any containers with team names and percentages
        # DK Network may use divs/sections with specific patterns
        pass

    seen = set()
    for title, section_soup in game_sections:
        game = _parse_dk_network_game(title, section_soup, source_key, sport, now)
        if game:
            key = (game["away_team"], game["home_team"])
            if key not in seen:
                seen.add(key)
                games.append(game)

    # If structured parsing failed, fall through to enhanced generic
    if not games:
        games = _parse_dk_network_generic(soup, source_key, sport, now)

    return games


def _parse_dk_network_game(title: str, section_soup, source_key: str, sport: str, timestamp: str) -> dict | None:
    """Parse a single game from DK Network section."""
    src = SOURCES[source_key]

    # Extract teams from title ("Away @ Home" or "Away vs Home")
    teams = re.split(r'\s+[@]\s+|\s+vs\.?\s+', title, flags=re.IGNORECASE)
    if len(teams) < 2:
        return None
    away_team = teams[0].strip()
    home_team = teams[1].strip()
    # Remove date/time if embedded in team name
    for i, t in enumerate([away_team, home_team]):
        # Remove trailing date patterns like "2/13, 07:00PM"
        t = re.sub(r'\d{1,2}/\d{1,2}.*$', '', t).strip()
        if i == 0:
            away_team = t
        else:
            home_team = t

    if not away_team or not home_team:
        return None

    game = {col: "" for col in COLUMNS}
    game["timestamp"] = timestamp
    game["source"] = source_key
    game["book"] = src["book"]
    game["sport"] = sport
    game["away_team"] = away_team
    game["home_team"] = home_team

    # Extract all percentages from the section
    text = section_soup.get_text(" ", strip=True)
    pcts = [float(p) for p in re.findall(r'(\d{1,3})%', text)]

    # DK Network layout per game: Moneyline (4 pcts), Spread (4 pcts), Total (4 pcts)
    # Each bet type: away_handle%, away_bets%, home_handle%, home_bets%
    # But the order in the rendered text may vary

    # Extract spread and total lines
    numbers = re.findall(r'([+-]?\d+(?:\.\d+)?)', text)
    numbers = [float(n) for n in numbers]
    for n in numbers:
        if -50 < n < 50 and n != 0 and not game["spread_line"]:
            game["spread_line"] = n
        elif 30 < n < 400 and not game["total_line"]:
            game["total_line"] = n

    # Map percentages to fields (best effort with DK Network's order)
    # DK shows: Moneyline section, then Spread section, then Total section
    # Each section has 4 percentages: team1_handle, team1_bets, team2_handle, team2_bets
    if len(pcts) >= 12:
        # Moneyline: pcts[0:4]
        game["ml_away_handle_pct"] = pcts[0]
        game["ml_away_bets_pct"] = pcts[1]
        game["ml_home_handle_pct"] = pcts[2]
        game["ml_home_bets_pct"] = pcts[3]
        # Spread: pcts[4:8]
        game["spread_away_handle_pct"] = pcts[4]  # Actually home-spread first in DK
        game["spread_away_bets_pct"] = pcts[5]
        game["spread_home_handle_pct"] = pcts[6]
        game["spread_home_bets_pct"] = pcts[7]
        # Total: pcts[8:12]
        game["total_over_handle_pct"] = pcts[8]
        game["total_over_bets_pct"] = pcts[9]
        game["total_under_handle_pct"] = pcts[10]
        game["total_under_bets_pct"] = pcts[11]
    elif len(pcts) >= 4:
        # Partial data â€” map what we have
        pct_map = [
            "spread_away_bets_pct", "spread_home_bets_pct",
            "spread_away_handle_pct", "spread_home_handle_pct",
            "total_over_bets_pct", "total_under_bets_pct",
            "total_over_handle_pct", "total_under_handle_pct",
            "ml_away_bets_pct", "ml_home_bets_pct",
            "ml_away_handle_pct", "ml_home_handle_pct",
        ]
        for i, field in enumerate(pct_map):
            if i < len(pcts):
                game[field] = pcts[i]

    compute_signals(game)
    return game


def _parse_dk_network_generic(soup, source_key: str, sport: str, timestamp: str) -> list[dict]:
    """Fallback: parse DK Network page by looking for percentage-rich sections."""
    src = SOURCES[source_key]
    games = []
    seen = set()

    # Look for sections with "@" in text (game matchups) plus percentages
    for el in soup.find_all(["div", "section", "article"]):
        text = el.get_text(" ", strip=True)
        if "@" not in text:
            continue
        pcts = re.findall(r'(\d{1,3})%', text)
        if len(pcts) < 4:
            continue
        if len(text) > 5000:  # Too broad a container
            continue

        # Try to extract game title
        title_match = re.search(r'([A-Z][A-Za-z\s.\']+)\s+@\s+([A-Z][A-Za-z\s.\']+)', text)
        if not title_match:
            # Try shorter abbreviation pattern like "NYK @ BOS"
            title_match = re.search(r'(\w{2,4}\s+\w+)\s+@\s+(\w{2,4}\s+\w+)', text)
        if title_match:
            away = title_match.group(1).strip()
            home = title_match.group(2).strip()
            key = (away, home)
            if key not in seen:
                seen.add(key)
                game = {col: "" for col in COLUMNS}
                game["timestamp"] = timestamp
                game["source"] = source_key
                game["book"] = src["book"]
                game["sport"] = sport
                game["away_team"] = away
                game["home_team"] = home
                pct_vals = [float(p) for p in pcts]
                pct_map = [
                    "ml_away_handle_pct", "ml_away_bets_pct",
                    "ml_home_handle_pct", "ml_home_bets_pct",
                    "spread_away_handle_pct", "spread_away_bets_pct",
                    "spread_home_handle_pct", "spread_home_bets_pct",
                    "total_over_handle_pct", "total_over_bets_pct",
                    "total_under_handle_pct", "total_under_bets_pct",
                ]
                for i, field in enumerate(pct_map):
                    if i < len(pct_vals):
                        game[field] = pct_vals[i]
                compute_signals(game)
                games.append(game)

    return games


# -------------------------------------------------------
# SBD (SportsBettingDime) SPECIFIC PARSER
# -------------------------------------------------------
# SBD renders data via JS framework. The DOM structure uses various
# class names that we need to target specifically.

def parse_sbd(html: str, source_key: str, sport: str) -> list[dict]:
    """Parse SportsBettingDime betting trends page."""
    soup = BeautifulSoup(html, "html.parser")
    now = now_eastern().isoformat()
    src = SOURCES[source_key]
    games = []
    seen = set()

    # SBD may use class names with 'matchup', 'game', 'event', 'trend',
    # 'consensus', or table-based layouts. Try multiple selectors.
    selectors = [
        # Common SBD selectors
        "[class*='matchup']",
        "[class*='game-card']",
        "[class*='event-card']",
        "[class*='betting-trend']",
        "[class*='public-betting']",
        "[class*='consensus']",
        # Table rows
        "table tbody tr",
        # Generic card patterns
        "[class*='card']",
    ]

    containers = []
    for sel in selectors:
        found = soup.select(sel)
        if found:
            containers.extend(found)

    # Deduplicate: skip containers that are parents of other containers
    if not containers:
        # Broader fallback: any element with team-like text + percentages
        for el in soup.find_all(["div", "tr", "li", "article", "section"]):
            text = el.get_text(" ", strip=True)
            pcts = re.findall(r'\d{1,3}%', text)
            if len(pcts) >= 2 and 20 < len(text) < 2000:
                # Check for team-like words (capitals, common patterns)
                if re.search(r'[A-Z][a-z]+ [A-Z][a-z]+|[A-Z]{2,}', text):
                    containers.append(el)

    for container in containers:
        game = _extract_sbd_game(container, source_key, sport, now)
        if game:
            key = (game["away_team"], game["home_team"])
            if key not in seen and game["away_team"] and game["home_team"]:
                seen.add(key)
                games.append(game)

    return games


def _extract_sbd_game(el, source_key: str, sport: str, timestamp: str) -> dict | None:
    """Extract a single game from an SBD container element."""
    text = el.get_text(" ", strip=True)
    if len(text) < 10:
        return None

    src = SOURCES[source_key]

    # Find team names â€” SBD often uses specific class names
    team_els = el.select(
        "[class*='team'], [class*='name'], [class*='participant'], "
        "[class*='matchup'], td:first-child"
    )

    teams = []
    for t in team_els:
        name = t.get_text(strip=True)
        if name and 1 < len(name) < 60 and not re.match(r'^[\d.%+\-/]+$', name):
            skip_words = {
                "spread", "total", "moneyline", "ml", "over", "under",
                "bets", "handle", "%", "vs", "at", "@", "run line",
                "puck line", "game total", "1st half", "2nd half",
                "public", "betting", "trends", "consensus", "money",
                "bet %", "money %", "line", "odds"
            }
            if name.lower() not in skip_words:
                teams.append(name)

    # Deduplicate preserving order
    seen_teams = []
    for t in teams:
        if t not in seen_teams:
            seen_teams.append(t)
    teams = seen_teams[:2]

    if len(teams) < 2:
        # Fallback: try regex for "Team vs Team" or "Team @ Team"
        match = re.search(r'([A-Z][A-Za-z\s.\']+?)\s+(?:vs\.?|@|at)\s+([A-Z][A-Za-z\s.\']+)', text)
        if match:
            teams = [match.group(1).strip(), match.group(2).strip()]

    if len(teams) < 2:
        return None

    # Extract percentages
    pcts = [float(p) for p in re.findall(r'(\d{1,3}(?:\.\d)?)%', text)]
    if len(pcts) < 2:
        return None

    # Extract lines
    numbers = re.findall(r'([+-]?\d+(?:\.\d+)?)', text)
    numbers = [float(n) for n in numbers if 1 < abs(float(n)) < 300]

    game = {col: "" for col in COLUMNS}
    game["timestamp"] = timestamp
    game["source"] = source_key
    game["book"] = src["book"]
    game["sport"] = sport
    game["away_team"] = teams[0]
    game["home_team"] = teams[1]

    # Map percentages
    pct_map = [
        "spread_away_bets_pct", "spread_home_bets_pct",
        "spread_away_handle_pct", "spread_home_handle_pct",
        "total_over_bets_pct", "total_under_bets_pct",
        "total_over_handle_pct", "total_under_handle_pct",
        "ml_away_bets_pct", "ml_home_bets_pct",
        "ml_away_handle_pct", "ml_home_handle_pct",
    ]
    for i, field in enumerate(pct_map):
        if i < len(pcts):
            game[field] = pcts[i]

    for n in numbers:
        if -50 < n < 50 and n != 0 and not game["spread_line"]:
            game["spread_line"] = n
        elif 30 < n < 300 and not game["total_line"]:
            game["total_line"] = n

    compute_signals(game)
    return game


# -------------------------------------------------------
# PARSER DISPATCHER
# -------------------------------------------------------

def parse_splits_page(html: str, source_key: str, sport: str) -> list[dict]:
    """Route to the correct parser based on source."""
    if source_key in ("vsin_dk", "vsin_circa"):
        games = parse_vsin(html, source_key, sport)
        if games:
            return games
        # If VSiN parser found nothing, fall through to generic
        print(f"  [{source_key}] VSiN parser found 0 games, trying generic...")

    if source_key == "dk_network":
        games = parse_dk_network(html, source_key, sport)
        if games:
            return games
        print(f"  [{source_key}] DK Network parser found 0 games, trying generic...")

    if source_key == "sbd":
        games = parse_sbd(html, source_key, sport)
        if games:
            return games
        print(f"  [{source_key}] SBD parser found 0 games, trying generic...")

    return parse_generic(html, source_key, sport)


def compute_signals(game: dict):
    """Compute sharp money signals from bets vs handle divergence."""
    try:
        hb = float(game.get("spread_home_bets_pct", 0) or 0)
        hh = float(game.get("spread_home_handle_pct", 0) or 0)
        if hb and hh:
            game["spread_bets_handle_divergence"] = round(abs(hb - hh), 1)
            game["sharp_signal_spread"] = abs(hb - hh) >= 15
        ob = float(game.get("total_over_bets_pct", 0) or 0)
        oh = float(game.get("total_over_handle_pct", 0) or 0)
        if ob and oh:
            game["total_bets_handle_divergence"] = round(abs(ob - oh), 1)
            game["sharp_signal_total"] = abs(ob - oh) >= 15
    except (ValueError, TypeError):
        pass


def make_game_id(sport: str, game_date: str, away_team: str, home_team: str) -> str:
    """Generate a stable, deterministic game ID for linking scrapes of the same game.

    Format: {sport}_{YYYYMMDD}_{away_normalized}_{home_normalized}
    Example: cbb_20260213_duke_north_carolina
    """
    # Normalize team names: lowercase, strip punctuation, collapse whitespace
    def norm(name):
        name = name.lower().strip()
        name = re.sub(r'[^\w\s]', '', name)      # drop punctuation
        name = re.sub(r'\s+', '_', name.strip())  # spaces â†’ underscores
        # Drop common prefixes/suffixes that vary across sources
        for prefix in ["the ", "university of ", "univ of "]:
            if name.startswith(prefix.replace(" ", "_")):
                name = name[len(prefix.replace(" ", "_")):]
        return name

    # Normalize game_date to YYYYMMDD
    date_str = ""
    today = now_eastern()
    year = str(today.year)

    if game_date:
        gd = game_date.strip()
        # Prepend current year to avoid Python 3.15 deprecation warning
        for fmt in ["%Y %b %d", "%Y %m/%d", "%Y %B %d", "%Y %b. %d"]:
            try:
                parsed = datetime.strptime(f"{year} {gd}", fmt)
                date_str = parsed.strftime("%Y%m%d")
                break
            except ValueError:
                continue

    if not date_str:
        date_str = today.strftime("%Y%m%d")

    away = norm(away_team) if away_team else "unk"
    home = norm(home_team) if home_team else "unk"
    return f"{sport}_{date_str}_{away}_{home}"


def assign_game_ids(games: list[dict], sport: str):
    """Assign game_id to each game record."""
    for game in games:
        game["game_id"] = make_game_id(
            sport,
            str(game.get("game_date", "")),
            str(game.get("away_team", "")),
            str(game.get("home_team", "")),
        )


# In-memory cache of previous scrape data, keyed by (game_id, source).
# Used to compute deltas without reading CSV files every scrape.
_prev_scrape_cache: dict[tuple[str, str], dict] = {}
_scrape_counter: dict[tuple[str, str], int] = {}


def compute_deltas(games: list[dict]):
    """Compute per-field deltas from the previous scrape of the same game+source.
    Also assigns scrape_num (incrementing per game_id+source)."""
    for game in games:
        key = (game.get("game_id", ""), game.get("source", ""))

        # Increment scrape counter
        _scrape_counter[key] = _scrape_counter.get(key, 0) + 1
        game["scrape_num"] = _scrape_counter[key]

        prev = _prev_scrape_cache.get(key)
        if prev:
            for field in DELTA_FIELDS:
                d_field = f"d_{field}"
                try:
                    curr_val = float(game.get(field, 0) or 0)
                    prev_val = float(prev.get(field, 0) or 0)
                    if curr_val and prev_val:
                        game[d_field] = round(curr_val - prev_val, 1)
                    else:
                        game[d_field] = ""
                except (ValueError, TypeError):
                    game[d_field] = ""
        else:
            # First scrape for this game â€” no deltas
            for field in DELTA_FIELDS:
                game[f"d_{field}"] = ""

        # Update cache
        _prev_scrape_cache[key] = {f: game.get(f, "") for f in DELTA_FIELDS}


# ============================================================
# CROSS-SPORT CONTAMINATION DETECTION (ESPN Schedule)
# ============================================================
# VSiN's "other" sport tabs (wcbb, cbase) often fail to switch,
# silently showing the default sport (CBB). We detect this by
# fetching the real schedule from ESPN's public API and checking
# whether the scraped team names match the correct sport.

import urllib.request
import json as _json

# ESPN scoreboard API endpoints (public, no auth needed)
_ESPN_SCHEDULE_URLS = {
    "nfl":     "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={date}&limit=200",
    "cfb":     "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates={date}&groups=80&limit=200",
    "nba":     "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}&limit=200",
    "wnba":    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates={date}&limit=200",
    "cbb":     "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates={date}&groups=50&limit=200",
    "wcbb":    "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard?dates={date}&groups=50&limit=200",
    "nhl":     "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={date}&limit=200",
    "mlb":     "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={date}&limit=200",
    "cbase":   "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard?dates={date}&groups=50&limit=200",
    "chockey": "https://site.api.espn.com/apis/site/v2/sports/hockey/mens-college-hockey/scoreboard?dates={date}&limit=200",
    # Soccer leagues use the same ESPN soccer endpoint but with league IDs
    "epl":     "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates={date}&limit=200",
    "ucl":     "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard?dates={date}&limit=200",
}

# Cache: { ("wcbb", "20260213"): {"manhattan", "niagara", "duke", ...} }
_espn_team_cache: dict[tuple[str, str], set[str]] = {}


def _normalize_team(name: str) -> str:
    """Normalize team name for fuzzy matching across sources."""
    name = name.lower().strip()
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _fetch_espn_teams(sport: str, date_str: str) -> set[str]:
    """
    Fetch today's team names for a sport from ESPN's public scoreboard API.
    Returns a set of normalized team names scheduled to play that day.
    Caches results so we only hit ESPN once per sport per day.
    """
    cache_key = (sport, date_str)
    if cache_key in _espn_team_cache:
        return _espn_team_cache[cache_key]

    url_template = _ESPN_SCHEDULE_URLS.get(sport)
    if not url_template:
        return set()

    url = url_template.format(date=date_str)
    teams = set()

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))

        for event in data.get("events", []):
            for comp in event.get("competitions", []):
                for team_entry in comp.get("competitors", []):
                    team_obj = team_entry.get("team", {})
                    # Collect multiple name forms for better matching
                    for field in ["displayName", "shortDisplayName", "name", "location"]:
                        val = team_obj.get(field, "")
                        if val:
                            teams.add(_normalize_team(val))

        _espn_team_cache[cache_key] = teams
        print(f"  [espn] Fetched {sport.upper()} schedule for {date_str}: "
              f"{len(data.get('events', []))} games, {len(teams)} team names")
    except Exception as e:
        print(f"  [espn] Warning: Could not fetch {sport} schedule: {e}")
        _espn_team_cache[cache_key] = set()

    return _espn_team_cache[cache_key]


# ============================================================
# ESPN GAME START TIMES (for closing-line capture)
# ============================================================
# Cache: { ("cbb", "20260219"): [ {"away_names": [...], "home_names": [...], "start_utc": datetime}, ...] }
_espn_schedule_cache: dict[tuple[str, str], list[dict]] = {}


def _fetch_espn_schedule(sport: str, date_str: str) -> list[dict]:
    """
    Fetch game start times for a sport/date from ESPN's public scoreboard API.
    Returns a list of dicts with team name variants and UTC start times.
    Caches results; refreshes if called again after 10 minutes.
    """
    cache_key = (sport, date_str)
    if cache_key in _espn_schedule_cache:
        cached = _espn_schedule_cache[cache_key]
        # Check staleness: re-fetch if cache is older than 10 minutes
        if cached and cached[0].get("_fetched_at"):
            age = (datetime.utcnow() - cached[0]["_fetched_at"]).total_seconds()
            if age < 600:
                return cached

    url_template = _ESPN_SCHEDULE_URLS.get(sport)
    if not url_template:
        return []

    url = url_template.format(date=date_str)
    schedule = []

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))

        for event in data.get("events", []):
            for comp in event.get("competitions", []):
                away_entry = None
                home_entry = None
                for team_entry in comp.get("competitors", []):
                    if team_entry.get("homeAway") == "away":
                        away_entry = team_entry
                    elif team_entry.get("homeAway") == "home":
                        home_entry = team_entry

                if not away_entry or not home_entry:
                    continue

                # Parse ISO 8601 start time from ESPN
                start_str = comp.get("date", "")
                start_utc = None
                if start_str:
                    try:
                        # ESPN returns ISO 8601: "2026-02-19T23:00Z"
                        start_str = start_str.replace("Z", "+00:00")
                        start_utc = datetime.fromisoformat(start_str)
                    except (ValueError, TypeError):
                        pass

                def _team_names(entry):
                    t = entry.get("team", {})
                    return [_normalize_team(t.get(f, ""))
                            for f in ("displayName", "shortDisplayName", "name", "location", "abbreviation")
                            if t.get(f)]

                game_info = {
                    "away_names": _team_names(away_entry),
                    "home_names": _team_names(home_entry),
                    "start_utc": start_utc,
                    "status_id": str(comp.get("status", {}).get("type", {}).get("id", "")),
                    "_fetched_at": datetime.utcnow(),
                }
                schedule.append(game_info)

        _espn_schedule_cache[cache_key] = schedule
        if schedule:
            print(f"  [espn] Schedule for {sport.upper()} {date_str}: {len(schedule)} games with start times")
    except Exception as e:
        print(f"  [espn] Warning: Could not fetch {sport} schedule: {e}")
        if cache_key not in _espn_schedule_cache:
            _espn_schedule_cache[cache_key] = []

    return _espn_schedule_cache.get(cache_key, [])


def _find_espn_start_utc(sport: str, away_team: str, home_team: str, date_str: str) -> datetime | None:
    """
    Look up a game's UTC start time from the ESPN schedule cache.
    Matches by fuzzy team name comparison.
    Returns a timezone-aware UTC datetime, or None if not found.
    """
    schedule = _fetch_espn_schedule(sport, date_str)
    if not schedule:
        return None

    away_norm = _normalize_team(away_team)
    home_norm = _normalize_team(home_team)

    for game in schedule:
        if not game.get("start_utc"):
            continue
        # Check if away team matches any ESPN away name variant
        away_match = any(
            (away_norm and espn_name and (
                away_norm == espn_name or
                (len(away_norm) >= 4 and (away_norm in espn_name or espn_name in away_norm)) or
                (len(away_norm.replace(" ", "")) >= 4 and (
                    away_norm.replace(" ", "") in espn_name.replace(" ", "") or
                    espn_name.replace(" ", "") in away_norm.replace(" ", "")))
            ))
            for espn_name in game["away_names"]
        )
        home_match = any(
            (home_norm and espn_name and (
                home_norm == espn_name or
                (len(home_norm) >= 4 and (home_norm in espn_name or espn_name in home_norm)) or
                (len(home_norm.replace(" ", "")) >= 4 and (
                    home_norm.replace(" ", "") in espn_name.replace(" ", "") or
                    espn_name.replace(" ", "") in home_norm.replace(" ", "")))
            ))
            for espn_name in game["home_names"]
        )
        if away_match and home_match:
            return game["start_utc"]

    return None


def _find_espn_et_date(sport: str, away_team: str, home_team: str,
                      window_days: int = 1) -> str | None:
    """
    Search ESPN's schedule for a window around today (yesterday/today/tomorrow
    by default) to find when a game is actually scheduled. Returns the game's
    Eastern Time date as 'YYYYMMDD', or None if no match.

    This is the authoritative-date helper used to correct VSiN's `game_date`
    column. VSiN occasionally shows tomorrow's games under today's table
    (or keeps yesterday's games visible), so trusting their `game_date`
    leads to fragmented game_ids for the same game.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    today = now_eastern()
    candidates = []
    for offset in range(-window_days, window_days + 1):
        d = today + timedelta(days=offset)
        candidates.append(d.strftime("%Y%m%d"))

    for date_str in candidates:
        start_utc = _find_espn_start_utc(sport, away_team, home_team, date_str)
        if start_utc:
            # Convert UTC start time to ET date (this is the "real" game date)
            start_et = start_utc.astimezone(et)
            return start_et.strftime("%Y%m%d")

    return None


def _yyyymmdd_to_display_date(yyyymmdd: str) -> str:
    """Convert '20260519' â†’ 'May 19' format (matches VSiN's game_date column)."""
    if not yyyymmdd or len(yyyymmdd) != 8:
        return ""
    try:
        d = datetime.strptime(yyyymmdd, "%Y%m%d")
        return d.strftime("%b %-d") if hasattr(d, "strftime") else f"{d.strftime('%b')} {d.day}"
    except ValueError:
        return ""


def normalize_game_dates_with_espn(games: list[dict], sport: str) -> tuple[list[dict], int, int]:
    """
    For each game, overwrite VSiN's `game_date` with ESPN's authoritative
    date if a match can be found. Also drop games whose ET date is in the
    past relative to today_ET (VSiN's stale-page problem).

    Returns: (kept_games, normalized_count, dropped_stale_count)
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    today_et_str = now_eastern().strftime("%Y%m%d")

    kept = []
    normalized = 0
    dropped = 0

    for g in games:
        away = str(g.get("away_team", "")).strip()
        home = str(g.get("home_team", "")).strip()
        if not away or not home:
            kept.append(g)
            continue

        espn_date = _find_espn_et_date(sport, away, home, window_days=2)

        if espn_date:
            # Update game_date to ESPN's authoritative ET date
            new_display = _yyyymmdd_to_display_date(espn_date)
            if new_display and new_display != g.get("game_date", ""):
                normalized += 1
                g["game_date"] = new_display

            # Drop games whose ET date is BEFORE today (VSiN serving stale page)
            if espn_date < today_et_str:
                dropped += 1
                continue
        # If ESPN didn't recognize the game, keep the row as-is â€” we can't
        # tell if it's stale or just a game ESPN doesn't have (rare team,
        # preseason, international league).
        kept.append(g)

    return kept, normalized, dropped


def _match_team_to_schedule(scraped_name: str, schedule_teams: set[str]) -> bool:
    """Check if a scraped team name matches any name in the ESPN schedule.
    Uses substring matching and spaceless matching to handle abbreviations
    and punctuation differences (e.g. 'Michigan ST' vs 'Michigan State',
    'Loyola-Chicago' vs 'Loyola Chicago')."""
    norm = _normalize_team(scraped_name)
    if not norm:
        return False

    norm_nospace = norm.replace(" ", "")

    # Exact match
    if norm in schedule_teams:
        return True

    for espn_name in schedule_teams:
        espn_nospace = espn_name.replace(" ", "")

        # Substring match (with spaces)
        if len(norm) >= 4 and norm in espn_name:
            return True
        if len(espn_name) >= 4 and espn_name in norm:
            return True

        # Spaceless match (catches "loyolachicago" vs "loyola chicago")
        if len(norm_nospace) >= 4 and norm_nospace in espn_nospace:
            return True
        if len(espn_nospace) >= 4 and espn_nospace in norm_nospace:
            return True

    return False


# Map "other" sports to their default/contamination parent sport
_CONTAMINATION_MAP = {
    "wcbb": "cbb",
    "cbase": "cbb",
    "chockey": "nhl",
}


def check_contamination(sport: str, games: list[dict], source_key: str = "") -> list[dict]:
    """
    Validate scraped games for 'other' sports against ESPN's schedule.

    For each game, check whether BOTH teams appear on the correct sport's
    ESPN schedule. If most games match the PARENT sport schedule instead,
    the VSiN tab switch failed and we reject the data.

    Returns the filtered games list (contaminated games removed).
    """
    parent_sport = _CONTAMINATION_MAP.get(sport)
    if not parent_sport:
        return games  # Not an "other" sport, no check needed

    if not games:
        return games

    today = today_str()

    # Fetch both schedules
    correct_teams = _fetch_espn_teams(sport, today)
    parent_teams = _fetch_espn_teams(parent_sport, today)

    if not correct_teams and not parent_teams:
        print(f"  [{source_key}] Could not fetch ESPN schedules â€” "
              f"cannot validate, passing data through with warning.")
        return games

    if not correct_teams:
        # No games on the correct sport schedule today â€” could be legitimate
        # (early in the day, or just no games). If parent has games, be suspicious.
        if parent_teams:
            # Check if scraped games match parent sport
            parent_matches = 0
            for g in games:
                away = str(g.get("away_team", ""))
                home = str(g.get("home_team", ""))
                if (_match_team_to_schedule(away, parent_teams) or
                    _match_team_to_schedule(home, parent_teams)):
                    parent_matches += 1
            if parent_matches > len(games) * 0.5:
                info = SPORT_INFO.get(sport, {})
                print(f"  [{source_key}] CONTAMINATION: No {sport.upper()} games on ESPN today, "
                      f"but {parent_matches}/{len(games)} games match {parent_sport.upper()} schedule.")
                print(f"  [{source_key}] Rejecting all {len(games)} games â€” tab switch likely failed.")
                return []
        return games

    # We have schedule data for the correct sport â€” validate each game
    clean_games = []
    rejected = 0

    for g in games:
        away = str(g.get("away_team", ""))
        home = str(g.get("home_team", ""))

        on_correct = (_match_team_to_schedule(away, correct_teams) or
                      _match_team_to_schedule(home, correct_teams))
        on_parent = (_match_team_to_schedule(away, parent_teams) and
                     _match_team_to_schedule(home, parent_teams))

        if on_correct:
            clean_games.append(g)
        elif on_parent and not on_correct:
            rejected += 1
        else:
            # Can't confirm either way â€” keep it (benefit of the doubt)
            clean_games.append(g)

    if rejected > 0:
        print(f"  [{source_key}] Contamination filter: kept {len(clean_games)}, "
              f"rejected {rejected} games that matched {parent_sport.upper()} schedule.")

    # Final safety check: if we rejected ALL games, the tab switch totally failed
    if len(clean_games) == 0 and len(games) > 0:
        print(f"  [{source_key}] All {len(games)} games rejected â€” "
              f"entire page was {parent_sport.upper()} data.")

    return clean_games


# ============================================================
# SCRAPE ORCHESTRATOR
# ============================================================

def scrape_source(source_key: str, sport: str, driver) -> list[dict]:
    """Scrape a single source for a sport. Returns (games, driver) â€” driver
    may be replaced if a timeout forces a restart."""
    src = SOURCES[source_key]
    url = src["url_fn"](sport)
    if url is None:
        info = SPORT_INFO.get(sport, {})
        print(f"  [{source_key}] {info.get('display', sport)} not available on {src['name']} â€” skipping")
        return []

    print(f"  [{source_key}] Fetching {url}")

    try:
        # Use source-specific fetch functions (reduced wait times)
        if source_key == "dk_network":
            html = fetch_dk_network(url, driver, sport)
        elif source_key == "sbd":
            html = fetch_sbd(url, driver)
        elif source_key in ("vsin_dk", "vsin_circa"):
            html = fetch_vsin(url, driver, sport, source_key=source_key)
        else:
            html = fetch_page(url, driver)

        games = parse_splits_page(html, source_key, sport)
        print(f"  [{source_key}] Parsed {len(games)} games")

        # --- Cross-sport contamination check (ESPN schedule) ---
        # For "other" sports (wcbb, cbase), verify games against ESPN schedule
        info = SPORT_INFO.get(sport, {})
        if games and info.get("category") == "other":
            games = check_contamination(sport, games, source_key=source_key)
            if not games:
                print(f"  [{source_key}] All games rejected by contamination filter.")

        # --- Date normalization + stale-page filter ---
        # VSiN occasionally serves stale or future-dated games on a sport's
        # page (e.g. yesterday's completed games still visible the next
        # morning, or tomorrow's games listed under today's table). Look up
        # each game's authoritative ET date from ESPN, overwrite game_date
        # when ESPN disagrees, and drop games whose actual ET date is in
        # the past (relative to today_ET).
        if games:
            try:
                games, normalized, dropped_stale = normalize_game_dates_with_espn(games, sport)
                if normalized:
                    print(f"  [{source_key}] Normalized game_date for {normalized} game(s) using ESPN.")
                if dropped_stale:
                    print(f"  [{source_key}] Dropped {dropped_stale} stale (yesterday-or-earlier) game(s).")
            except Exception as e:
                # Never let date normalization kill the whole scrape â€”
                # fall back to whatever VSiN said if ESPN lookups blow up.
                print(f"  [{source_key}] Warning: date normalization failed: {e}")

        # Save raw HTML ONLY on parse failure (0 games) for debugging
        if not games:
            ts = now_eastern().strftime("%Y%m%d_%H%M%S")
            raw_path = DATA_DIR / "raw_html" / f"{source_key}_{sport}_{ts}.html"
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(html)

            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            h5s = [h.get_text(strip=True)[:60] for h in soup.find_all("h5")]
            pct_count = len(re.findall(r'\d{1,3}%', soup.get_text()))
            print(f"  [{source_key}] Debug: {len(tables)} tables, {len(h5s)} h5 headers, ~{pct_count} percentages in page")
            if h5s[:3]:
                print(f"  [{source_key}] Debug h5 samples: {h5s[:3]}")
            print(f"  [{source_key}] Debug: raw HTML saved to {raw_path} for inspection")

        # Clear browser memory between sources
        try:
            driver.execute_script("window.gc && window.gc();")
        except Exception:
            pass

        return games
    except Exception as e:
        err_str = str(e).lower()
        if "timeout" in err_str or "timed out" in err_str or "connectionrefused" in err_str:
            print(f"  [{source_key}] TIMEOUT/CONNECTION ERROR: {e}")
            print(f"  [{source_key}] Browser may have crashed â€” skipping this source")
        else:
            print(f"  [{source_key}] Error: {e}")
            import traceback
            traceback.print_exc()
        return []


def _driver_is_alive(driver) -> bool:
    """Check if the Selenium driver is still responsive."""
    try:
        _ = driver.title
        return True
    except Exception:
        return False


def scrape_all(sport: str, sources: list[str] | None = None, driver=None) -> tuple[list[dict], any]:
    """Scrape all (or specified) sources for a sport.
    Returns (games, driver) â€” driver may have been restarted."""
    own_driver = driver is None
    if own_driver:
        driver = get_driver()

    # Default to VSiN sources only (DK Network and SBD removed for efficiency —
    # DK Network is redundant with vsin_dk, SBD often lacks handle data).
    # Re-enable with --source dk_network or --source sbd if needed.
    DEFAULT_SOURCES = ["vsin_dk", "vsin_circa"]
    target_sources = sources or DEFAULT_SOURCES
    all_games = []

    info = SPORT_INFO.get(sport, {})
    display = info.get("display", sport.upper())

    print(f"\n{'='*60}")
    print(f"  Scraping {display} from {len(target_sources)} sources")
    print(f"  {now_eastern():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}")

    for src_key in target_sources:
        # Check driver health before each source â€” restart if crashed
        if not _driver_is_alive(driver):
            print(f"  [!] Driver unresponsive â€” restarting browser...")
            try:
                driver.quit()
            except Exception:
                pass
            driver = get_driver()

        games = scrape_source(src_key, sport, driver)
        all_games.extend(games)

    if own_driver:
        try:
            driver.quit()
        except Exception:
            pass
        driver = None

    print(f"\n  Total: {len(all_games)} game records across all sources")
    return all_games, driver


# ============================================================
# MULTI-SPORT BATCH SCRAPING
# ============================================================

def scrape_batch(sports: list[str], sources: list[str] | None = None,
                 auto_close_window: int = 0):
    """Scrape multiple sports in one session (shares a single browser).

    If auto_close_window > 0, each sport's scrape is checked against ESPN
    schedules. Games starting within that many minutes get their current
    data saved as closing-line snapshots to data/closing/.
    """
    driver = get_driver()
    all_results = {}

    print(f"\n{'='*60}")
    print(f"  BATCH SCRAPE: {len(sports)} sports")
    print(f"  Sports: {', '.join(SPORT_INFO[s]['display'] for s in sports)}")
    if auto_close_window > 0:
        print(f"  Auto-close: {auto_close_window} min window")
    print(f"  {now_eastern():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}")

    for sport in sports:
        # scrape_all may restart driver if it crashes â€” track the new one
        games, driver = scrape_all(sport, sources, driver=driver)
        save_data(games, sport)
        all_results[sport] = len(games)

        # Check for imminent games and save closing lines
        if auto_close_window > 0 and games:
            auto_close_check(games, sport, window_minutes=auto_close_window)

    try:
        driver.quit()
    except Exception:
        pass

    print(f"\n{'='*60}")
    print(f"  BATCH COMPLETE")
    for sport, count in all_results.items():
        print(f"  {SPORT_INFO[sport]['display']:35s} {count:>4} records")
    print(f"{'='*60}")


# ============================================================
# STORAGE
# ============================================================

def _safe_csv_write(df: pd.DataFrame, path: Path, mode: str = "w",
                    header: bool = True, max_retries: int = 3) -> bool:
    """Write a DataFrame to CSV, handling file locks (e.g. Excel has it open).

    Strategy:
    1. Try writing directly (fast path).
    2. On PermissionError, write to a temp file next to the target,
       then retry the rename/append after a short wait.
    3. If all retries fail, save to a .pending file so no data is lost.

    Returns True if the write succeeded to the target path.
    """
    # Fast path: try direct write
    try:
        df.to_csv(path, mode=mode, header=header, index=False)
        return True
    except PermissionError:
        pass

    # File is locked â€” write to temp, then retry
    pending_path = path.with_suffix(f".pending_{now_eastern():%H%M%S}.csv")
    df.to_csv(pending_path, index=False)

    for attempt in range(1, max_retries + 1):
        time.sleep(2 * attempt)  # 2s, 4s, 6s backoff
        try:
            if mode == "a" and path.exists():
                df.to_csv(path, mode="a", header=False, index=False)
            else:
                df.to_csv(path, mode="w", header=True, index=False)
            # Success â€” clean up pending file
            try:
                pending_path.unlink()
            except Exception:
                pass
            print(f"  (file was locked, succeeded on retry {attempt})")
            return True
        except PermissionError:
            if attempt < max_retries:
                print(f"  (file locked, retry {attempt}/{max_retries}...)")
            continue

    # All retries failed â€” pending file preserves the data
    print(f"  WARNING: Could not write to {path.name} (file locked by another program).")
    print(f"  Data saved to {pending_path.name} â€” it will be merged on next successful write.")
    return False


def _merge_pending_files(path: Path):
    """Check for any .pending_*.csv files and merge them into the target file."""
    pending_pattern = path.stem + ".pending_*.csv"
    pending_files = sorted(path.parent.glob(pending_pattern))
    if not pending_files:
        return

    for pf in pending_files:
        try:
            pending_df = pd.read_csv(pf)
            if path.exists():
                pending_df.to_csv(path, mode="a", header=False, index=False)
            else:
                pending_df.to_csv(path, index=False)
            pf.unlink()
            print(f"  Merged pending file: {pf.name}")
        except Exception as e:
            print(f"  Could not merge {pf.name}: {e}")


def save_data(games: list[dict], sport: str, is_closing: bool = False):
    """Save scraped data to timeseries and (optionally) closing files.

    Phase 1 simplification: removed per-scrape snapshot files and master
    file append. Timeseries is the single source of truth for intraday data.
    Master dataset can be reconstructed from timeseries files (and will be
    replaced by Parquet archive in Phase 3).
    """
    if not games:
        print("  No data to save.")
        return

    # Assign game_ids and compute deltas from previous scrape
    assign_game_ids(games, sport)
    compute_deltas(games)

    df = pd.DataFrame(games)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[COLUMNS]

    today = today_str()

    # Closing
    if is_closing:
        close_path = DATA_DIR / "closing" / f"{sport}_{today}.csv"
        _safe_csv_write(df, close_path, mode="w", header=True)
        print(f"  Closing: {close_path}")

    # Time series (one file per sport per day, append every scrape)
    ts_path = DATA_DIR / "timeseries" / f"{sport}_{today}.csv"
    _merge_pending_files(ts_path)  # Merge any previously failed writes
    if ts_path.exists():
        _safe_csv_write(df, ts_path, mode="a", header=False)
    else:
        _safe_csv_write(df, ts_path, mode="w", header=True)
    print(f"  Timeseries: {ts_path} (+{len(df)} rows)")


def auto_close_check(games: list[dict], sport: str, window_minutes: int = 10):
    """Check if any scraped games are about to start and save closing lines.

    This brings the closing-line logic from run_gameday() into --once mode.
    For each game, we look up the ESPN start time. If the game starts within
    `window_minutes`, we save the current scrape as the closing snapshot.

    Designed to run after every --once scrape so that whichever cron run
    happens to land near tip-off automatically captures closing lines.
    """
    if not games or window_minutes <= 0:
        return

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    utc = ZoneInfo("UTC")
    now_utc = datetime.now(utc)
    today = today_str()

    closing_games = []

    # Load existing closing file to avoid duplicating games already captured
    close_path = DATA_DIR / "closing" / f"{sport}_{today}.csv"
    already_saved = set()
    if close_path.exists():
        try:
            existing = pd.read_csv(close_path)
            for _, row in existing.iterrows():
                already_saved.add((
                    str(row.get("away_team", "")).strip(),
                    str(row.get("home_team", "")).strip(),
                    str(row.get("source", "")).strip(),
                ))
        except Exception:
            pass

    for game in games:
        away = str(game.get("away_team", "")).strip()
        home = str(game.get("home_team", "")).strip()
        source = str(game.get("source", "")).strip()

        if (away, home, source) in already_saved:
            continue

        # Look up start time from ESPN
        gid = str(game.get("game_id", ""))
        m = re.search(r'_(\d{8})_', gid)
        date_str = m.group(1) if m else today

        start_utc = _find_espn_start_utc(sport, away, home, date_str)
        if not start_utc:
            continue

        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=utc)

        minutes_until = (start_utc - now_utc).total_seconds() / 60

        if 0 < minutes_until <= window_minutes:
            closing_games.append(game)
            print(f"  [auto-close] {away} @ {home} ({source}) "
                  f"starts in {minutes_until:.0f} min -- capturing closing line")

    if closing_games:
        save_data(closing_games, sport, is_closing=True)
        print(f"  [auto-close] Saved {len(closing_games)} closing line(s) for {sport}")
    else:
        # Only log if ESPN had games today (avoid noise for off-days)
        schedule = _fetch_espn_schedule(sport, today)
        if schedule:
            upcoming = sum(1 for g in schedule
                          if g.get("start_utc") and
                          g["start_utc"].tzinfo and
                          (g["start_utc"] - now_utc).total_seconds() / 60 > 0)
            if upcoming > 0:
                print(f"  [auto-close] {sport}: {upcoming} games remaining today, "
                      f"none within {window_minutes} min window")


# ============================================================
# ANALYSIS
# ============================================================

def analyze(sport: str):
    """Analyze collected data from the master file."""
    master = DATA_DIR / f"{sport}_master.csv"
    if not master.exists():
        print(f"No data for {sport}. Run the scraper first.")
        return

    info = SPORT_INFO.get(sport, {})
    display = info.get("display", sport.upper())

    df = pd.read_csv(master)
    print(f"\n{'='*70}")
    print(f"  {display} MASTER DATA ANALYSIS")
    print(f"{'='*70}")
    print(f"  Records:       {len(df):,}")
    print(f"  Date range:    {df['timestamp'].min()} â†’ {df['timestamp'].max()}")
    print(f"  Sources:       {df['source'].nunique()} ({', '.join(df['source'].unique())})")
    print(f"  Unique games:  ~{df.groupby(['away_team','home_team','game_date']).ngroups}")
    print(f"  Snapshots:     {df['timestamp'].nunique()}")

    # Source comparison
    print(f"\n  BY SOURCE:")
    for src, grp in df.groupby("source"):
        print(f"    {src}: {len(grp):,} records, {grp.groupby(['away_team','home_team']).ngroups} games")

    # Sharp signals
    for col in ["sharp_signal_spread", "sharp_signal_total"]:
        if col in df.columns:
            signals = df[df[col] == True] if df[col].dtype == bool else df[df[col] == "True"]
            pct = len(signals) / len(df) * 100 if len(df) > 0 else 0
            print(f"\n  {col}: {len(signals)} records ({pct:.1f}% of total)")

    # Cross-source agreement
    if df["source"].nunique() > 1:
        print(f"\n  CROSS-SOURCE ANALYSIS:")
        for col in ["spread_home_bets_pct", "total_over_bets_pct"]:
            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(vals) > 0:
                over60 = (vals > 60).sum()
                over70 = (vals > 70).sum()
                print(f"    {col}: mean={vals.mean():.1f}%, "
                      f">60%: {over60} ({over60/len(vals)*100:.1f}%), "
                      f">70%: {over70} ({over70/len(vals)*100:.1f}%)")


# ============================================================
# API DISCOVERY
# ============================================================

FIND_API_SCRIPT = '''#!/usr/bin/env python3
"""
Network intercept script to discover the actual JSON API endpoints
that power the betting splits pages. Run this once to find the URLs,
then you can call them directly without a browser.

Setup: pip install playwright && playwright install chromium
"""
import asyncio, json
from playwright.async_api import async_playwright

URLS_TO_MONITOR = [
    # Main sports
    ("VSiN DK - NBA",        "https://data.vsin.com/nba/betting-splits/"),
    ("VSiN DK - NFL",        "https://data.vsin.com/nfl/betting-splits/"),
    ("VSiN DK - NHL",        "https://data.vsin.com/nhl/betting-splits/"),
    ("VSiN DK - MLB",        "https://data.vsin.com/mlb/betting-splits/"),
    ("VSiN DK - CBB",        "https://data.vsin.com/college-basketball/betting-splits/"),
    ("VSiN DK - CFB",        "https://data.vsin.com/college-football/betting-splits/"),
    # Other leagues
    ("VSiN DK - WCBB",       "https://data.vsin.com/draftkings/betting-splits/?view=wcbb"),
    ("VSiN DK - ColBaseball", "https://data.vsin.com/draftkings/betting-splits/?view=cbase"),
    # Circa
    ("VSiN Circa - NBA",     "https://data.vsin.com/nba/betting-splits/?bookid=circa"),
    ("VSiN Circa - NHL",     "https://data.vsin.com/nhl/betting-splits/?bookid=circa"),
    # Third-party
    ("DK Network",           "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"),
    ("SBD - NBA",            "https://www.sportsbettingdime.com/nba/public-betting-trends/"),
    ("SBD - CBB",            "https://www.sportsbettingdime.com/college-basketball/public-betting-trends/"),
]

async def discover():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for label, url in URLS_TO_MONITOR:
            print(f"\\n{'='*60}")
            print(f"  Monitoring: {label}")
            print(f"  URL: {url}")
            print(f"{'='*60}")

            page = await browser.new_page()
            api_calls = []

            async def on_response(response):
                req_url = response.url
                content_type = response.headers.get("content-type", "")
                if "json" in content_type or any(kw in req_url.lower() for kw in [
                    "split", "handle", "consensus", "betting", "popular",
                    "public", "odds", "event", "game", "matchup", "wager"
                ]):
                    try:
                        body = await response.text()
                        if len(body) > 50:
                            api_calls.append({
                                "url": req_url,
                                "status": response.status,
                                "content_type": content_type,
                                "size": len(body),
                                "preview": body[:300]
                            })
                    except:
                        pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(5000)
            except Exception as e:
                print(f"  Page load error: {e}")

            await page.close()

            if api_calls:
                api_calls.sort(key=lambda x: x["size"], reverse=True)
                print(f"  Found {len(api_calls)} potential API endpoints:")
                for i, call in enumerate(api_calls[:5]):
                    print(f"\\n  [{i+1}] {call['url'][:120]}")
                    print(f"      Status: {call['status']} | Type: {call['content_type']} | Size: {call['size']:,} bytes")
                    print(f"      Preview: {call['preview'][:150]}...")
            else:
                print(f"  No API endpoints detected. Data may be server-rendered.")

        await browser.close()

asyncio.run(discover())
'''


# ============================================================
# SCHEDULER
# ============================================================

def run_scheduled(sport: str, sources: list[str] | None, interval: int = 5):
    try:
        import schedule
    except ImportError:
        sys.exit("Install: pip install schedule")

    driver = get_driver()
    info = SPORT_INFO.get(sport, {})
    display = info.get("display", sport.upper())
    print(f"Scheduled scraping: {display}, every {interval} min")
    print("Press Ctrl+C to stop.\n")

    def job():
        nonlocal driver
        try:
            games, driver = scrape_all(sport, sources, driver=driver)
            save_data(games, sport)
        except Exception as e:
            print(f"  Error: {e}")

    job()
    schedule.every(interval).minutes.do(job)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def run_scheduled_batch(sports: list[str], sources: list[str] | None, interval: int = 5):
    """Schedule batch scraping of multiple sports."""
    try:
        import schedule
    except ImportError:
        sys.exit("Install: pip install schedule")

    driver = get_driver()
    display_list = ", ".join(SPORT_INFO[s]["display"] for s in sports)
    print(f"Scheduled batch scraping: {display_list}")
    print(f"Every {interval} min. Press Ctrl+C to stop.\n")

    def job():
        nonlocal driver
        try:
            for sport in sports:
                games, driver = scrape_all(sport, sources, driver=driver)
                save_data(games, sport)
        except Exception as e:
            print(f"  Error: {e}")

    job()
    schedule.every(interval).minutes.do(job)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ============================================================
# GAMEDAY SCHEDULER (Two-Tier + Auto Closing Lines)
# ============================================================

def run_gameday(sports: list[str], sources: list[str] | None,
                early_start: str = "07:30", early_end: str = "11:30",
                late_end: str = "23:00",
                early_interval: int = 60, late_interval: int = 30,
                closing_window: int = 5,
                timezone_str: str = "US/Eastern"):
    """
    Two-tier gameday schedule with automatic closing-line capture.

    Schedule:
      early_start -> early_end : scrape every early_interval minutes
      early_end   -> late_end  : scrape every late_interval minutes

    Closing lines:
      Game start times are fetched from ESPN's public schedule API (not
      from scraped HTML, which lacks reliable time data). When a game is
      within `closing_window` minutes of tip, the current scrape is saved
      as the closing line snapshot to data/closing/.

    Adaptive pre-tip scrapes:
      Between scheduled scrapes, the loop checks ESPN every 60 seconds
      for imminent tipoffs. If any game is within the closing window,
      an extra scrape fires immediately so that the closing snapshot
      captures the very latest wager data (important for sharp money
      signals from late-arriving handle).

    Args:
        sports: List of sport keys to scrape
        sources: Source filter (or None for all)
        early_start: Start of early window (HH:MM, 24h)
        early_end: End of early / start of late window
        late_end: Stop scraping after this time
        early_interval: Minutes between scrapes in early window
        late_interval: Minutes between scrapes in late window
        closing_window: Minutes before game start to save closing line (default 5)
        timezone_str: Timezone for all time logic
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_str)

    def parse_hm(s):
        h, m = s.split(":")
        return int(h), int(m)

    early_h, early_m = parse_hm(early_start)
    switch_h, switch_m = parse_hm(early_end)
    end_h, end_m = parse_hm(late_end)

    display_list = ", ".join(SPORT_INFO[s]["display"] for s in sports)
    print(f"\n{'='*60}")
    print(f"  GAMEDAY SCHEDULER")
    print(f"  Sports: {display_list}")
    print(f"  Timezone: {timezone_str}")
    print(f"  Early window:  {early_start} - {early_end}  (every {early_interval} min)")
    print(f"  Late window:   {early_end} - {late_end}  (every {late_interval} min)")
    print(f"  Closing window: {closing_window} min before game time")
    print(f"  Press Ctrl+C to stop.")
    print(f"{'='*60}\n")

    driver = get_driver()

    # Track closing lines: {(sport, away, home): last_game_data}
    closing_tracker = {}

    def now_tz():
        return datetime.now(tz)

    def time_minutes(dt):
        return dt.hour * 60 + dt.minute

    def in_window(dt):
        t = time_minutes(dt)
        start = early_h * 60 + early_m
        end = end_h * 60 + end_m
        return start <= t < end

    def current_interval(dt):
        t = time_minutes(dt)
        switch = switch_h * 60 + switch_m
        if t < switch:
            return early_interval
        return late_interval

    def get_game_start_utc(game: dict, sport: str) -> datetime | None:
        """Look up a game's start time via ESPN's schedule API.

        Returns a timezone-aware UTC datetime, or None if not found.
        This replaces the old parse_game_datetime which relied on
        the never-populated game_time field from VSiN scraping.
        """
        away = str(game.get("away_team", "")).strip()
        home = str(game.get("home_team", "")).strip()
        if not away or not home:
            return None

        # Determine the YYYYMMDD date string for the ESPN lookup.
        # Use game_id if available (format: sport_YYYYMMDD_away_home),
        # otherwise fall back to today's date.
        gid = str(game.get("game_id", ""))
        m = re.search(r'_(\d{8})_', gid)
        date_str = m.group(1) if m else now_tz().strftime("%Y%m%d")

        return _find_espn_start_utc(sport, away, home, date_str)

    # Track which games have already had closing lines saved
    # so we don't save duplicates across scrape cycles.
    closing_saved: set[tuple[str, str, str]] = set()

    def process_closing_lines(games: list[dict], sport: str):
        """Check each game against closing window; save closing snapshots.

        Uses ESPN API to get authoritative game start times instead of
        parsing the (often empty) game_time field from VSiN scrapes.
        """
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        utc = ZoneInfo("UTC")
        now_utc = datetime.now(utc)
        closing_games = []

        for game in games:
            key = (sport, game.get("away_team", ""), game.get("home_team", ""))

            # Skip if we already saved closing lines for this game
            if key in closing_saved:
                continue

            start_utc = get_game_start_utc(game, sport)
            if not start_utc:
                # Can't determine start time -- update tracker with latest data
                closing_tracker[key] = game
                continue

            # Ensure start_utc is timezone-aware for comparison
            if start_utc.tzinfo is None:
                start_utc = start_utc.replace(tzinfo=utc)

            minutes_until = (start_utc - now_utc).total_seconds() / 60

            if minutes_until <= 0:
                # Game already started -- don't update closing tracker
                pass
            elif minutes_until <= closing_window:
                # Within closing window -- this IS the closing line
                closing_games.append(game)
                closing_tracker[key] = game
                closing_saved.add(key)
                print(f"  [closing] {game['away_team']} @ {game['home_team']} "
                      f"starts in {minutes_until:.0f} min -- capturing closing line")
            else:
                # Pre-game, not yet in window -- update tracker for later
                closing_tracker[key] = game

        # Save closing lines if any
        if closing_games:
            save_data(closing_games, sport, is_closing=True)
            print(f"  [closing] Saved {len(closing_games)} closing line(s) for {sport}")

    def _next_tipoff_minutes() -> float | None:
        """Scan ESPN schedules for all tracked sports and return the number
        of minutes until the soonest upcoming tipoff (that we haven't already
        captured closing lines for). Returns None if no upcoming games found."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        utc = ZoneInfo("UTC")
        now_utc = datetime.now(utc)
        today = now_tz().strftime("%Y%m%d")
        soonest = None

        for sport in sports:
            schedule = _fetch_espn_schedule(sport, today)
            for espn_game in schedule:
                start_utc = espn_game.get("start_utc")
                if not start_utc:
                    continue
                if start_utc.tzinfo is None:
                    start_utc = start_utc.replace(tzinfo=utc)
                mins = (start_utc - now_utc).total_seconds() / 60
                # Only consider games that haven't started and are within
                # the closing window + a small buffer for the scrape itself
                if 0 < mins <= closing_window + 2:
                    if soonest is None or mins < soonest:
                        soonest = mins

        return soonest

    _scrape_count = 0
    _RECYCLE_EVERY = 20  # Recycle browser every N scrapes to prevent memory leaks

    def do_scrape(reason: str = "scheduled"):
        nonlocal driver, _scrape_count
        now = now_tz()
        print(f"\n  [{now:%H:%M:%S}] Running scrape ({reason}, "
              f"interval: {current_interval(now)} min)...")

        _scrape_count += 1

        # Proactive driver recycling to prevent stale browser / memory leaks
        if _scrape_count % _RECYCLE_EVERY == 0:
            print(f"  [maintenance] Recycling browser (scrape #{_scrape_count})...")
            try:
                driver.quit()
            except Exception:
                pass
            driver = get_driver()

        for sport in sports:
            try:
                games, driver = scrape_all(sport, sources, driver=driver)
                if games:
                    save_data(games, sport)
                    process_closing_lines(games, sport)
            except Exception as e:
                print(f"  [{sport}] Error during scrape/save: {e}")
                # Restart driver on failure to prevent cascading errors
                print(f"  [{sport}] Restarting browser and continuing...")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = get_driver()

    # --- Main loop ---
    try:
        while True:
            now = now_tz()

            if not in_window(now):
                # Outside scraping window
                t = time_minutes(now)
                start = early_h * 60 + early_m
                if t < start:
                    wait = start - t
                    print(f"  [{now:%H:%M}] Before window. Sleeping {wait} min until {early_start}...")
                    time.sleep(wait * 60)
                else:
                    print(f"  [{now:%H:%M}] Past end of window ({late_end}). Done for today.")
                    break
                continue

            do_scrape("scheduled")

            # --- Adaptive sleep: wake up early for pre-tip closing scrapes ---
            #
            # Instead of blindly sleeping for the full interval, we check
            # ESPN for any games starting within the closing window. If a
            # tipoff falls inside our sleep, we wake up just before it so
            # we capture the freshest possible closing lines.
            interval = current_interval(now_tz())
            sleep_remaining = interval * 60  # seconds

            while sleep_remaining > 30:  # don't bother for < 30 sec
                # Sleep in 60-second chunks so we can re-check
                chunk = min(60, sleep_remaining)
                time.sleep(chunk)
                sleep_remaining -= chunk

                # Check if any tipoff is imminent
                tip_mins = _next_tipoff_minutes()
                if tip_mins is not None and tip_mins <= closing_window:
                    print(f"\n  [!] Game starting in {tip_mins:.0f} min -- "
                          f"running pre-tip closing scrape...")
                    do_scrape("pre-tip closing")
                    # After the pre-tip scrape, resume the remaining sleep
                    # (the closing_saved set prevents duplicate saves)
                    break

            # If we exited normally (no pre-tip), sleep any tiny remainder
            if sleep_remaining > 0:
                time.sleep(sleep_remaining)

            now = now_tz()
            print(f"  [{now:%H:%M}] Next scheduled scrape...")

    except KeyboardInterrupt:
        print("\n\nStopping gameday scheduler...")
    finally:
        # Final pass: save closing lines from tracker for any games about
        # to start that we haven't saved yet.
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        utc = ZoneInfo("UTC")
        now_utc = datetime.now(utc)
        for (sport, away, home), game in closing_tracker.items():
            key = (sport, away, home)
            if key in closing_saved:
                continue
            start_utc = get_game_start_utc(game, sport)
            if start_utc:
                if start_utc.tzinfo is None:
                    start_utc = start_utc.replace(tzinfo=utc)
                mins = (start_utc - now_utc).total_seconds() / 60
                if mins <= closing_window:
                    save_data([game], sport, is_closing=True)
                    closing_saved.add(key)

        try:
            driver.quit()
        except Exception:
            pass

    print(f"\n  Gameday complete. Check data/closing/ for closing lines.")



# ============================================================
# LIST SPORTS
# ============================================================

def list_sports():
    print(f"\n{'='*60}")
    print(f"  SUPPORTED SPORTS")
    print(f"{'='*60}")

    print(f"\n  {'Key':<10} {'Name':<35} {'Season':<12} {'Sources'}")
    print(f"  {'---':<10} {'----':<35} {'------':<12} {'-------'}")

    for key, info in SPORT_INFO.items():
        sources_avail = []
        for src_key, src in SOURCES.items():
            if src["url_fn"](key) is not None:
                sources_avail.append(src_key)
        print(f"  {key:<10} {info['display']:<35} {info['season']:<12} {', '.join(sources_avail)}")

    print(f"\n  Use --sport <key> to scrape a specific sport")
    print(f"  Use --batch <key1> <key2> ... to scrape multiple sports at once")
    print(f"  Use 'all-major' as shorthand for nfl,nba,nhl,mlb,cfb,cbb")
    print(f"  Use 'all' for every supported sport\n")


# ============================================================
# MAIN
# ============================================================

def resolve_sports(sport_arg: str) -> list[str]:
    """Resolve sport argument which may be a key, 'all', or 'all-major'."""
    if sport_arg == "all":
        return ALL_SPORTS
    elif sport_arg == "all-major":
        return [k for k, v in SPORT_INFO.items() if v["category"] == "major"]
    elif sport_arg in SPORT_INFO:
        return [sport_arg]
    else:
        print(f"Unknown sport: {sport_arg}")
        print(f"Valid options: {', '.join(ALL_SPORTS + ['all', 'all-major'])}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Source Betting Splits Scraper (v2 - Expanded Leagues)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sports:
  nfl        NFL
  nba        NBA
  nhl        NHL
  mlb        MLB
  cfb        College Football
  cbb        College Basketball (Men's)
  wcbb       College Basketball (Women's)
  cbase      College Baseball

Shortcuts:
  all-major  nfl, nba, nhl, mlb, cfb, cbb
  all        Everything including wcbb, cbase

Examples:
  %(prog)s --sport nba --once
  %(prog)s --sport wcbb --once
  %(prog)s --sport cbase --source vsin_dk --once
  %(prog)s --batch nba nhl cbb --schedule
  %(prog)s --batch all-major --once
  %(prog)s --batch cbb wcbb --gameday
  %(prog)s --batch cbb wcbb --gameday --early-start 07:30 --switch-time 11:30 --end-time 23:00
  %(prog)s --list-sports
        """)
    parser.add_argument("--sport", default=None,
                        help="Sport to scrape (see --list-sports)")
    parser.add_argument("--batch", nargs="+", default=None,
                        help="Multiple sports to scrape in one session")
    parser.add_argument("--source", default=None,
                        choices=list(SOURCES.keys()),
                        help="Single source (default: all available)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    parser.add_argument("--schedule", action="store_true",
                        help="Run on a fixed schedule")
    parser.add_argument("--gameday", action="store_true",
                        help="Two-tier gameday schedule with auto closing lines")
    parser.add_argument("--close", action="store_true",
                        help="Save closing snapshot")
    parser.add_argument("--interval", type=int, default=5,
                        help="Minutes between scheduled runs (default: 5)")
    parser.add_argument("--early-start", default="07:30",
                        help="Gameday: start of early window (HH:MM, default: 07:30)")
    parser.add_argument("--switch-time", default="11:30",
                        help="Gameday: switch from early to late interval (HH:MM, default: 11:30)")
    parser.add_argument("--end-time", default="23:00",
                        help="Gameday: stop scraping (HH:MM, default: 23:00)")
    parser.add_argument("--early-interval", type=int, default=60,
                        help="Gameday: minutes between early scrapes (default: 60)")
    parser.add_argument("--late-interval", type=int, default=30,
                        help="Gameday: minutes between late scrapes (default: 30)")
    parser.add_argument("--closing-window", type=int, default=5,
                        help="Gameday: minutes before tip to capture closing line (default: 5)")
    parser.add_argument("--timezone", default="US/Eastern",
                        help="Timezone for schedule (default: US/Eastern)")
    parser.add_argument("--analyze", metavar="SPORT", nargs="?", const="nba",
                        help="Analyze collected data for a sport")
    parser.add_argument("--auto-close", type=int, default=0, metavar="MINUTES",
                        help="Auto-save closing lines for games starting within N minutes (use with --once)")
    parser.add_argument("--find-api", action="store_true",
                        help="Generate API discovery script")
    parser.add_argument("--list-sports", action="store_true",
                        help="Show all supported sports and sources")

    args = parser.parse_args()

    # --- List sports ---
    if args.list_sports:
        list_sports()
        return

    # --- Find API ---
    if args.find_api:
        script_path = DATA_DIR / "find_api_endpoints.py"
        with open(script_path, "w") as f:
            f.write(FIND_API_SCRIPT)
        print(f"Saved API discovery script to {script_path}")
        print(f"Run: pip install playwright && playwright install chromium")
        print(f"Then: python {script_path}")
        return

    # --- Analyze ---
    if args.analyze:
        analyze(args.analyze)
        return

    # --- Determine sports list ---
    if args.batch:
        sports = []
        for item in args.batch:
            sports.extend(resolve_sports(item))
        # Deduplicate while preserving order
        seen = set()
        sports = [s for s in sports if not (s in seen or seen.add(s))]
    elif args.sport:
        sports = resolve_sports(args.sport)
    else:
        parser.print_help()
        print("\nError: --sport or --batch is required (or use --list-sports)")
        sys.exit(1)

    sources = [args.source] if args.source else None

    # --- Execute ---
    if args.gameday:
        run_gameday(
            sports, sources,
            early_start=args.early_start,
            early_end=args.switch_time,
            late_end=args.end_time,
            early_interval=args.early_interval,
            late_interval=args.late_interval,
            closing_window=args.closing_window,
            timezone_str=args.timezone,
        )
    elif args.schedule:
        if len(sports) == 1:
            run_scheduled(sports[0], sources, args.interval)
        else:
            run_scheduled_batch(sports, sources, args.interval)
    else:
        if len(sports) == 1:
            games, _ = scrape_all(sports[0], sources)
            save_data(games, sports[0], is_closing=args.close)
            if args.auto_close > 0 and games:
                auto_close_check(games, sports[0], window_minutes=args.auto_close)
        else:
            scrape_batch(sports, sources, auto_close_window=args.auto_close)


if __name__ == "__main__":
    main()
