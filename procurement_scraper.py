"""
UK Government Procurement Notice Scraper
=========================================
Fetches ALL notices from both APIs — no arbitrary caps.

Sources:
  1. Find a Tender Service (FTS) — cursor-based pagination, exhausts all pages
  2. Contracts Finder — page-number pagination, exhausts all pages

On a typical day expect:
  FTS             :  50–300 notices
  Contracts Finder: 200–800 notices
  Total           : 300–1,100+ notices per day

Requirements:
    pip install requests pandas

Usage:
    python procurement_scraper.py        # run and save
"""

import requests
import pandas as pd
import sqlite3
import json
import time
from datetime import datetime, timedelta, timezone


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

KEYWORDS = []
# Filter to matching notices only. Examples:
# KEYWORDS = ["data", "analytics", "AI", "digital", "cloud", "software"]
# Leave as [] to fetch ALL notices (recommended — analyse with procurement_analysis.py)

LOOKBACK_DAYS = 30       # days back to fetch (1 = yesterday to today)
                        # Increase temporarily to backfill, e.g. 7 or 30

DB_FILE    = "procurement.db"
CSV_PREFIX = "procurement_latest"

# API page sizes — maximum allowed per request
FTS_PAGE_SIZE = 100     # FTS max per cursor page
CF_PAGE_SIZE  = 100     # Contracts Finder max per page

# Polite delay between API calls (seconds)
API_DELAY = 0.5

# ─────────────────────────────────────────────────────────────────────────────


def fetch_fts_notices(updated_from: str, updated_to: str) -> list:
    """
    Fetch ALL notices from Find a Tender Service (FTS).

    Uses cursor-based pagination — each response returns a 'cursor' token
    for the next page. Loops until no cursor is returned (= last page).

    Parameters use full datetime format required by FTS API:
        updatedFrom = "YYYY-MM-DDTHH:MM:SS"
        updatedTo   = "YYYY-MM-DDTHH:MM:SS"
    """
    base_url    = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_notices = []
    cursor      = None
    page        = 1

    print(f"\n[FTS] Fetching all notices updated {updated_from[:10]} to {updated_to[:10]}...")

    while True:
        params = {
            "updatedFrom": updated_from,
            "updatedTo":   updated_to,
            "limit":       FTS_PAGE_SIZE,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            response = requests.get(base_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [FTS] HTTP {response.status_code} on page {page}: {e}")
            print(f"  [FTS] Response: {response.text[:300]}")
            break
        except requests.RequestException as e:
            print(f"  [FTS] Connection error on page {page}: {e}")
            break
        except json.JSONDecodeError:
            print(f"  [FTS] Could not parse JSON on page {page}")
            break

        releases = data.get("releases", [])
        if not releases:
            print(f"  [FTS] Page {page}: empty — pagination complete")
            break

        for release in releases:
            notice = _parse_fts_release(release)
            if notice:
                all_notices.append(notice)

        # Check for total count on first page
        if page == 1:
            total = data.get("totals", {}).get("total") or data.get("total")
            if total:
                est_pages = (total + FTS_PAGE_SIZE - 1) // FTS_PAGE_SIZE
                print(f"  [FTS] Total available: {total} notices (~{est_pages} pages)")

        print(f"  [FTS] Page {page}: fetched {len(releases)} | Running total: {len(all_notices)}")

        # Move to next page via cursor
        cursor = data.get("cursor")
        if not cursor:
            print(f"  [FTS] No cursor returned — all pages fetched")
            break

        page += 1
        time.sleep(API_DELAY)

    print(f"  [FTS] Complete: {len(all_notices)} notices fetched")
    return all_notices


def _parse_fts_release(release: dict) -> dict:
    """Extract fields from a single FTS OCDS release."""
    try:
        tender = release.get("tender", {}) or {}
        buyer  = release.get("buyer",  {}) or {}

        classification = tender.get("classification", {})
        if isinstance(classification, list):
            cpv = ", ".join(c.get("id", "") for c in classification)
        elif isinstance(classification, dict):
            cpv = classification.get("id", "")
        else:
            cpv = ""

        value_obj   = tender.get("value",    {}) or {}
        min_val_obj = tender.get("minValue", {}) or {}

        return {
            "source":         "FTS",
            "notice_id":      (release.get("ocid", "") + "_" + release.get("id", "")),
            "title":          tender.get("title", ""),
            "description":    (tender.get("description") or "")[:500],
            "organisation":   buyer.get("name", ""),
            "value_low":      min_val_obj.get("amount"),
            "value_high":     value_obj.get("amount"),
            "currency":       value_obj.get("currency", "GBP"),
            "published_date": (release.get("date") or "")[:10],
            "deadline":       ((tender.get("tenderPeriod") or {}).get("endDate") or "")[:10],
            "notice_type":    ((release.get("tag") or [""])[0]),
            "status":         tender.get("status", ""),
            "cpv_codes":      cpv,
            "url":            "https://www.find-tender.service.gov.uk/Notice/" + (release.get("id") or ""),
            "scraped_at":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"  [FTS] Parse error on release {release.get('id', '?')}: {e}")
        return None


def fetch_contracts_finder_notices(updated_from: str) -> list:
    """
    Fetch ALL notices from Contracts Finder OCDS Search API.

    Uses standard page-number pagination. Reads totalPages from the first
    response and loops through every page until done.
    """
    base_url    = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    all_notices = []
    page        = 1
    total_pages = None

    print(f"\n[CF] Fetching all notices from {updated_from}...")

    while True:
        params = {
            "publishedFrom": updated_from,
            "size":          CF_PAGE_SIZE,
            "page":          page,
        }
        headers = {
            "Accept":     "application/json",
            "User-Agent": "UKProcurementResearcher/1.0",
        }

        try:
            response = requests.get(base_url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [CF] HTTP {response.status_code} on page {page}: {e}")
            print(f"  [CF] Response: {response.text[:300]}")
            break
        except requests.RequestException as e:
            print(f"  [CF] Connection error on page {page}: {e}")
            break
        except json.JSONDecodeError:
            print(f"  [CF] Could not parse JSON on page {page}")
            break

        releases = data.get("releases", [])

        # Capture total pages from first response
        if page == 1:
            total_pages = data.get("totalPages", 1)
            total_count = data.get("totalElements") or data.get("total")
            if total_count:
                print(f"  [CF] Total available: {total_count} notices ({total_pages} pages)")
            else:
                print(f"  [CF] Total pages: {total_pages}")

        if not releases:
            print(f"  [CF] Page {page}: empty — pagination complete")
            break

        for release in releases:
            notice = _parse_cf_release(release)
            if notice:
                all_notices.append(notice)

        print(f"  [CF] Page {page}/{total_pages}: fetched {len(releases)} | Running total: {len(all_notices)}")

        if page >= (total_pages or 1):
            print(f"  [CF] All pages fetched")
            break

        page += 1
        time.sleep(API_DELAY)

    print(f"  [CF] Complete: {len(all_notices)} notices fetched")
    return all_notices


def _parse_cf_release(release: dict) -> dict:
    """Extract fields from a single Contracts Finder OCDS release."""
    try:
        tender = release.get("tender", {}) or {}
        buyer  = release.get("buyer",  {}) or {}

        classification = tender.get("classification", {})
        if isinstance(classification, list):
            cpv = ", ".join(c.get("id", "") for c in classification)
        elif isinstance(classification, dict):
            cpv = classification.get("id", "")
        else:
            cpv = ""

        value_obj   = tender.get("value",    {}) or {}
        min_val_obj = tender.get("minValue", {}) or {}

        return {
            "source":         "Contracts Finder",
            "notice_id":      (release.get("ocid", "") + "_" + release.get("id", "")),
            "title":          tender.get("title", ""),
            "description":    (tender.get("description") or "")[:500],
            "organisation":   buyer.get("name", ""),
            "value_low":      min_val_obj.get("amount"),
            "value_high":     value_obj.get("amount"),
            "currency":       value_obj.get("currency", "GBP"),
            "published_date": (release.get("date") or "")[:10],
            "deadline":       ((tender.get("tenderPeriod") or {}).get("endDate") or "")[:10],
            "notice_type":    ((release.get("tag") or [""])[0]),
            "status":         tender.get("status", ""),
            "cpv_codes":      cpv,
            "url":            "https://www.contractsfinder.service.gov.uk/Notice/" + (release.get("id") or ""),
            "scraped_at":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"  [CF] Parse error on release {release.get('id', '?')}: {e}")
        return None


def filter_by_keywords(notices: list, keywords: list) -> list:
    """Filter notices to those containing any keyword in title or description."""
    if not keywords:
        return notices
    keywords_lower = [k.lower() for k in keywords]
    return [
        n for n in notices
        if any(
            kw in (n.get("title", "") + " " + n.get("description", "")).lower()
            for kw in keywords_lower
        )
    ]


def save_to_database(notices: list, db_file: str):
    """Append notices to SQLite, skipping duplicates based on notice_id."""
    if not notices:
        print("[DB] No notices to save.")
        return

    conn   = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            notice_id      TEXT PRIMARY KEY,
            source         TEXT,
            title          TEXT,
            description    TEXT,
            organisation   TEXT,
            value_low      REAL,
            value_high     REAL,
            currency       TEXT,
            published_date TEXT,
            deadline       TEXT,
            notice_type    TEXT,
            status         TEXT,
            cpv_codes      TEXT,
            url            TEXT,
            scraped_at     TEXT
        )
    """)

    inserted = skipped = 0
    for n in notices:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notices VALUES (
                    :notice_id, :source, :title, :description, :organisation,
                    :value_low, :value_high, :currency, :published_date, :deadline,
                    :notice_type, :status, :cpv_codes, :url, :scraped_at
                )
            """, n)
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.Error as e:
            print(f"  [DB] Error for {n.get('notice_id', '?')}: {e}")

    conn.commit()
    conn.close()
    print(f"[DB] Inserted: {inserted} new | Skipped (duplicates): {skipped}")


def save_to_csv(notices: list, prefix: str):
    """Save notices to a dated CSV file."""
    if not notices:
        print("[CSV] No notices to save.")
        return
    today    = datetime.now().strftime("%Y-%m-%d")
    filename = f"{prefix}_{today}.csv"
    pd.DataFrame(notices).to_csv(filename, index=False)
    print(f"[CSV] Saved {len(notices)} notices → {filename}")


def print_summary(notices: list):
    """Print a readable console summary."""
    if not notices:
        print("\nNo notices found for this period.")
        return

    df = pd.DataFrame(notices)

    print(f"\n{'='*70}")
    print(f"  PROCUREMENT NOTICES — {datetime.now().strftime('%d %B %Y')}")
    print(f"{'='*70}")
    print(f"  Total notices      : {len(notices):,}")
    print(f"  FTS                : {(df['source']=='FTS').sum():,}")
    print(f"  Contracts Finder   : {(df['source']=='Contracts Finder').sum():,}")

    # Notice type breakdown
    print(f"\n  By type:")
    for t, c in df["notice_type"].value_counts().items():
        print(f"    {t:<28} {c:>5}")

    # Value summary
    vals = pd.to_numeric(df["value_high"], errors="coerce").dropna()
    if not vals.empty:
        print(f"\n  Value (where declared):")
        print(f"    Contracts with value : {len(vals):,}")
        print(f"    Total declared spend : £{vals.sum()/1_000_000:.1f}m")
        print(f"    Largest contract     : £{vals.max()/1_000_000:.2f}m")

    # Sample notices
    print(f"\n  Sample notices:")
    for n in notices[:5]:
        value = f"  £{n['value_high']:,.0f}" if n.get("value_high") else ""
        print(f"    [{n['source'][:2]}] {n['title'][:60]}")
        print(f"         {n['organisation']}{value}")
        print(f"         {n['url']}")
        print()

    if len(notices) > 5:
        print(f"  ... and {len(notices) - 5:,} more — see CSV or database.\n")
    print(f"{'='*70}\n")


def run_daily_scrape():
    """Main entry point — fetches all notices for the configured date range."""
    today     = datetime.now()
    from_date = today - timedelta(days=LOOKBACK_DAYS)

    # FTS requires full datetime format
    fts_from = from_date.strftime("%Y-%m-%dT00:00:00")
    fts_to   = today.strftime("%Y-%m-%dT23:59:59")

    # Contracts Finder uses date only
    cf_from  = from_date.strftime("%Y-%m-%d")

    print(f"\n{'='*70}")
    print(f"  UK Procurement Scraper — {today.strftime('%d %B %Y')}")
    print(f"  Date range: {from_date.strftime('%d %b %Y')} to {today.strftime('%d %b %Y')}")
    print(f"  Lookback  : {LOOKBACK_DAYS} day(s)")
    print(f"  Keywords  : {'ALL notices' if not KEYWORDS else ', '.join(KEYWORDS)}")
    print(f"{'='*70}")

    # Fetch from both sources — full pagination
    fts_notices = fetch_fts_notices(fts_from, fts_to)
    cf_notices  = fetch_contracts_finder_notices(cf_from)

    all_notices = fts_notices + cf_notices
    print(f"\n[Total] {len(all_notices):,} notices fetched from both sources")

    # Optional keyword filter
    if KEYWORDS:
        filtered = filter_by_keywords(all_notices, KEYWORDS)
        print(f"[Filter] {len(filtered):,} notices match keywords")
    else:
        filtered = all_notices
        print(f"[Filter] No keyword filter — keeping all {len(filtered):,} notices")

    # Save
    save_to_database(filtered, DB_FILE)
    save_to_csv(filtered, CSV_PREFIX)
    print_summary(filtered)

    return filtered


# ─────────────────────────────────────────────────────────────────────────────
# BONUS: Query the database
# ─────────────────────────────────────────────────────────────────────────────

def query_database(keyword=None, min_value=None, notice_type=None, limit=50):
    """
    Query your growing local database.

    Examples:
        df = query_database(keyword="AI", min_value=500000)
        df = query_database(notice_type="tender")
        df = query_database(keyword="digital", limit=100)
    """
    conn   = sqlite3.connect(DB_FILE)
    query  = "SELECT * FROM notices WHERE 1=1"
    params = []

    if keyword:
        query += " AND (title LIKE ? OR description LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    if min_value:
        query += " AND value_high >= ?"
        params.append(min_value)
    if notice_type:
        query += " AND notice_type = ?"
        params.append(notice_type)

    query += " ORDER BY published_date DESC LIMIT ?"
    params.append(limit)

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_daily_scrape()
