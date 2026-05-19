"""
UK Government Procurement Notice Scraper
=========================================
Uses two official government APIs — no scraping, no blocks, no ToS issues.

Sources:
  1. Find a Tender Service (FTS) — OCDS API
     High-value contracts, central govt, NHS, defence, utilities
     API: https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages

  2. Contracts Finder — OCDS Search API
     Lower-value contracts, wider public sector
     API: https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search

Run daily (manually, cron, or GitHub Actions) to build a growing database.

Requirements:
    pip install requests pandas
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
# Set keywords to filter notices, e.g:
# KEYWORDS = ["data", "analytics", "AI", "digital", "cloud", "software"]
# Leave as [] to fetch ALL notices

LOOKBACK_DAYS = 1           # how many days back to fetch (1 = yesterday to today)
DB_FILE       = "procurement.db"
CSV_PREFIX    = "procurement_latest"

# ─────────────────────────────────────────────────────────────────────────────


def fetch_fts_notices(updated_from: str, updated_to: str) -> list:
    """
    Fetch notices from Find a Tender Service (FTS) OCDS API.
    Uses cursor-based pagination.

    Correct parameters (as of 2025/2026):
        updatedFrom, updatedTo, stages, limit, cursor
    """
    base_url = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_notices = []
    cursor = None
    page = 1

    print(f"\n[FTS] Fetching notices updated {updated_from} to {updated_to}...")

    while True:
        params = {
            "updatedFrom": updated_from,
            "updatedTo":   updated_to,
            "limit":       100,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            response = requests.get(base_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [FTS] HTTP error on page {page}: {e}")
            print(f"  [FTS] Response: {response.text[:300]}")
            break
        except requests.RequestException as e:
            print(f"  [FTS] Request error on page {page}: {e}")
            break
        except json.JSONDecodeError:
            print(f"  [FTS] Could not parse JSON on page {page}")
            break

        releases = data.get("releases", [])
        if not releases:
            print(f"  [FTS] No releases on page {page} — done")
            break

        for release in releases:
            tender = release.get("tender", {}) or {}
            buyer  = release.get("buyer",  {}) or {}

            classification = tender.get("classification", {})
            if isinstance(classification, list):
                cpv = ", ".join([c.get("id", "") for c in classification])
            elif isinstance(classification, dict):
                cpv = classification.get("id", "")
            else:
                cpv = ""

            value_obj   = tender.get("value",    {}) or {}
            min_val_obj = tender.get("minValue", {}) or {}

            notice = {
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
            all_notices.append(notice)

        print(f"  [FTS] Page {page}: {len(releases)} notices")

        cursor = data.get("cursor")
        if not cursor:
            break

        page += 1
        time.sleep(0.5)

    print(f"  [FTS] Total: {len(all_notices)} notices")
    return all_notices


def fetch_contracts_finder_notices(updated_from: str) -> list:
    """
    Fetch notices from Contracts Finder OCDS Search API.
    Correct endpoint as of 2025/2026.
    """
    base_url = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    all_notices = []
    page = 1

    print(f"\n[Contracts Finder] Fetching notices from {updated_from}...")

    while True:
        params = {
            "publishedFrom": updated_from,
            "size":          100,
            "page":          page,
        }
        headers = {
            "Accept":     "application/json",
            "User-Agent": "ProcurementResearchBot/1.0",
        }

        try:
            response = requests.get(base_url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [CF] HTTP error on page {page}: {e}")
            print(f"  [CF] Response: {response.text[:300]}")
            break
        except requests.RequestException as e:
            print(f"  [CF] Request error on page {page}: {e}")
            break
        except json.JSONDecodeError:
            print(f"  [CF] Could not parse JSON on page {page}")
            break

        releases = data.get("releases", [])
        if not releases:
            print(f"  [CF] No releases on page {page} — done")
            break

        for release in releases:
            tender = release.get("tender", {}) or {}
            buyer  = release.get("buyer",  {}) or {}

            value_obj   = tender.get("value",    {}) or {}
            min_val_obj = tender.get("minValue", {}) or {}

            classification = tender.get("classification", {})
            if isinstance(classification, list):
                cpv = ", ".join([c.get("id", "") for c in classification])
            elif isinstance(classification, dict):
                cpv = classification.get("id", "")
            else:
                cpv = ""

            notice = {
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
            all_notices.append(notice)

        total_pages = data.get("totalPages", 1)
        print(f"  [CF] Page {page}/{total_pages}: {len(releases)} notices")

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.5)

    print(f"  [CF] Total: {len(all_notices)} notices")
    return all_notices


def filter_by_keywords(notices: list, keywords: list) -> list:
    """Filter notices to those containing any keyword in title or description."""
    if not keywords:
        return notices
    keywords_lower = [k.lower() for k in keywords]
    return [
        n for n in notices
        if any(kw in (n.get("title", "") + " " + n.get("description", "")).lower()
               for kw in keywords_lower)
    ]


def save_to_database(notices: list, db_file: str):
    """Append notices to SQLite, skipping duplicates."""
    if not notices:
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
            print(f"  DB error for {n.get('notice_id')}: {e}")

    conn.commit()
    conn.close()
    print(f"\n[DB] Inserted: {inserted} new | Skipped (duplicates): {skipped}")


def save_to_csv(notices: list, prefix: str):
    """Save notices to a dated CSV file."""
    if not notices:
        print("[CSV] No notices to save.")
        return
    today    = datetime.now().strftime("%Y-%m-%d")
    filename = f"{prefix}_{today}.csv"
    pd.DataFrame(notices).to_csv(filename, index=False)
    print(f"[CSV] Saved {len(notices)} notices to {filename}")


def print_summary(notices: list):
    """Print a readable console summary."""
    if not notices:
        print("\nNo notices found for this period.")
        return

    print(f"\n{'='*70}")
    print(f" PROCUREMENT NOTICES — {datetime.now().strftime('%d %B %Y')}")
    print(f" Total found: {len(notices)}")
    print(f"{'='*70}\n")

    for n in notices[:10]:
        value = ""
        if n.get("value_high"):
            value = f"  £{n['value_high']:,.0f}"
        elif n.get("value_low"):
            value = f"  £{n['value_low']:,.0f}+"
        print(f"  [{n['source']}] {n['title'][:65]}")
        print(f"  Buyer: {n['organisation']}{value}")
        print(f"  Published: {n['published_date']}  |  Deadline: {n.get('deadline', 'N/A')}")
        print(f"  {n['url']}")
        print()

    if len(notices) > 10:
        print(f"  ... and {len(notices) - 10} more. See CSV or database.\n")


def run_daily_scrape():
    """Main entry point — call this daily."""
    today     = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    fts_notices = fetch_fts_notices(updated_from=from_date, updated_to=today)
    cf_notices  = fetch_contracts_finder_notices(updated_from=from_date)

    all_notices = fts_notices + cf_notices
    print(f"\n[Total] {len(all_notices)} notices fetched from both sources")

    if KEYWORDS:
        filtered = filter_by_keywords(all_notices, KEYWORDS)
        print(f"[Filter] {len(filtered)} notices match keywords")
    else:
        filtered = all_notices
        print("[Filter] No keyword filter — keeping all notices")

    save_to_database(filtered, DB_FILE)
    save_to_csv(filtered, CSV_PREFIX)
    print_summary(filtered)

    return filtered


# ─────────────────────────────────────────────────────────────────────────────
# BONUS: Query your database
# ─────────────────────────────────────────────────────────────────────────────

def query_database(keyword=None, min_value=None, limit=20):
    """
    Query your local database after a few days of running.

    Example:
        df = query_database(keyword="AI", min_value=500000)
        print(df)
    """
    conn  = sqlite3.connect(DB_FILE)
    query = "SELECT title, organisation, value_high, published_date, url FROM notices WHERE 1=1"
    params = []

    if keyword:
        query += " AND (title LIKE ? OR description LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    if min_value:
        query += " AND value_high >= ?"
        params.append(min_value)

    query += " ORDER BY published_date DESC LIMIT ?"
    params.append(limit)

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_daily_scrape()
