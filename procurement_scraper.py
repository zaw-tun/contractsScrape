"""
UK Government Procurement Notice Scraper
=========================================
Uses two official government APIs — no scraping, no blocks, no ToS issues.

Sources:
  1. Find a Tender Service (FTS) — OCDS API
     High-value contracts (above ~£139k), central govt, NHS, defence, utilities
     API: https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages

  2. Contracts Finder — REST API
     Lower-value contracts (above £12k), wider public sector
     API: https://www.contractsfinder.service.gov.uk/Published/Notices/

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
# CONFIGURATION — edit these to filter what you care about
# ─────────────────────────────────────────────────────────────────────────────

KEYWORDS = [
    "data", "analytics", "artificial intelligence", "AI", "machine learning",
    "digital", "cloud", "software", "technology", "cyber", "automation"
]
# Leave empty list [] to fetch ALL notices with no keyword filter

DB_FILE = "procurement.db"          # SQLite database (auto-created)
CSV_FILE = "procurement_latest.csv" # Today's results also saved as CSV

# ─────────────────────────────────────────────────────────────────────────────


def fetch_fts_notices(published_from: str, published_to: str) -> list[dict]:
    """
    Fetch notices from Find a Tender Service (FTS) OCDS API.
    Handles pagination automatically.

    Args:
        published_from: date string e.g. "2026-05-18"
        published_to:   date string e.g. "2026-05-19"

    Returns:
        List of notice dicts
    """
    base_url = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_notices = []
    page = 1

    print(f"\n[FTS] Fetching notices published {published_from} to {published_to}...")

    while True:
        params = {
            "publishedFrom": published_from,
            "publishedTo":   published_to,
            "page":          page,
        }

        try:
            response = requests.get(base_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            print(f"  [FTS] Request error on page {page}: {e}")
            break
        except json.JSONDecodeError:
            print(f"  [FTS] Could not parse JSON on page {page}")
            break

        releases = data.get("releases", [])
        if not releases:
            break  # no more pages

        for release in releases:
            tender = release.get("tender", {})
            buyer  = release.get("buyer", {})

            notice = {
                "source":        "FTS",
                "notice_id":     release.get("ocid", ""),
                "title":         tender.get("title", ""),
                "description":   tender.get("description", "")[:500] if tender.get("description") else "",
                "organisation":  buyer.get("name", ""),
                "value_low":     tender.get("minValue", {}).get("amount") if tender.get("minValue") else None,
                "value_high":    tender.get("value", {}).get("amount")    if tender.get("value")    else None,
                "currency":      "GBP",
                "published_date": release.get("date", "")[:10],
                "deadline":      tender.get("tenderPeriod", {}).get("endDate", "")[:10]
                                 if tender.get("tenderPeriod", {}).get("endDate") else "",
                "notice_type":   release.get("tag", [""])[0] if release.get("tag") else "",
                "status":        tender.get("status", ""),
                "cpv_codes":     ", ".join(
                                    [c.get("id", "") for c in tender.get("classification", [])]
                                 ) if isinstance(tender.get("classification"), list)
                                   else tender.get("classification", {}).get("id", ""),
                "url":           f"https://www.find-tender.service.gov.uk/Notice/{release.get('ocid','').split('-')[-1]}",
                "scraped_at":    datetime.now(timezone.utc).isoformat(),
            }
            all_notices.append(notice)

        print(f"  [FTS] Page {page}: {len(releases)} notices fetched")
        page += 1
        time.sleep(0.5)  # polite delay

    print(f"  [FTS] Total: {len(all_notices)} notices")
    return all_notices


def fetch_contracts_finder_notices(published_from: str) -> list[dict]:
    """
    Fetch notices from Contracts Finder public search API (no auth required).
    Filters to notices published on or after published_from.

    Args:
        published_from: date string e.g. "2026-05-18"

    Returns:
        List of notice dicts
    """
    base_url = "https://www.contractsfinder.service.gov.uk/Published/Notices/PaginatedList"
    all_notices = []
    page = 1

    print(f"\n[Contracts Finder] Fetching notices from {published_from}...")

    while True:
        params = {
            "publishedFrom": published_from,
            "size":          100,
            "page":          page,
        }
        headers = {"Accept": "application/json"}

        try:
            response = requests.get(base_url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            print(f"  [CF] Request error on page {page}: {e}")
            break
        except json.JSONDecodeError:
            print(f"  [CF] Could not parse JSON on page {page}")
            break

        notices_raw = data.get("noticeList", [])
        if not notices_raw:
            break

        for n in notices_raw:
            notice = {
                "source":         "Contracts Finder",
                "notice_id":      n.get("id", ""),
                "title":          n.get("title", ""),
                "description":    n.get("description", "")[:500] if n.get("description") else "",
                "organisation":   n.get("organisationName", ""),
                "value_low":      n.get("valueLow"),
                "value_high":     n.get("valueHigh"),
                "currency":       "GBP",
                "published_date": n.get("publishedDate", "")[:10],
                "deadline":       n.get("deadlineDate", "")[:10] if n.get("deadlineDate") else "",
                "notice_type":    n.get("type", ""),
                "status":         n.get("status", ""),
                "cpv_codes":      ", ".join(n.get("cpvCodes", [])) if n.get("cpvCodes") else "",
                "url":            f"https://www.contractsfinder.service.gov.uk/Notice/{n.get('id','')}",
                "scraped_at":     datetime.now(timezone.utc).isoformat(),
            }
            all_notices.append(notice)

        total_pages = data.get("totalPages", 1)
        print(f"  [CF] Page {page}/{total_pages}: {len(notices_raw)} notices fetched")

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.5)

    print(f"  [CF] Total: {len(all_notices)} notices")
    return all_notices


def filter_by_keywords(notices: list[dict], keywords: list[str]) -> list[dict]:
    """Filter notices to those containing any keyword in title or description."""
    if not keywords:
        return notices  # no filter — return all

    keywords_lower = [k.lower() for k in keywords]
    filtered = []
    for n in notices:
        text = f"{n.get('title','')} {n.get('description','')}".lower()
        if any(kw in text for kw in keywords_lower):
            filtered.append(n)
    return filtered


def save_to_database(notices: list[dict], db_file: str):
    """
    Append notices to SQLite database.
    Skips duplicates based on notice_id.
    """
    if not notices:
        return

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    # Create table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            notice_id     TEXT PRIMARY KEY,
            source        TEXT,
            title         TEXT,
            description   TEXT,
            organisation  TEXT,
            value_low     REAL,
            value_high    REAL,
            currency      TEXT,
            published_date TEXT,
            deadline      TEXT,
            notice_type   TEXT,
            status        TEXT,
            cpv_codes     TEXT,
            url           TEXT,
            scraped_at    TEXT
        )
    """)

    inserted = 0
    skipped  = 0
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


def save_to_csv(notices: list[dict], csv_file: str):
    """Save today's notices to a dated CSV file."""
    if not notices:
        print("[CSV] No notices to save.")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    filename = csv_file.replace(".csv", f"_{today}.csv")
    df = pd.DataFrame(notices)
    df.to_csv(filename, index=False)
    print(f"[CSV] Saved {len(notices)} notices → {filename}")


def print_summary(notices: list[dict]):
    """Print a readable summary to the console."""
    if not notices:
        print("\nNo notices found for today.")
        return

    print(f"\n{'='*70}")
    print(f" PROCUREMENT NOTICES — {datetime.now().strftime('%d %B %Y')}")
    print(f" Total found: {len(notices)}")
    print(f"{'='*70}\n")

    for n in notices[:10]:  # show first 10 in console
        value = ""
        if n.get("value_high"):
            value = f"  £{n['value_high']:,.0f}"
        elif n.get("value_low"):
            value = f"  £{n['value_low']:,.0f}+"

        print(f"  [{n['source']}] {n['title'][:70]}")
        print(f"  Buyer: {n['organisation']}{value}")
        print(f"  Published: {n['published_date']}  |  Deadline: {n.get('deadline','N/A')}")
        print(f"  {n['url']}")
        print()

    if len(notices) > 10:
        print(f"  ... and {len(notices) - 10} more. See CSV or database for full list.\n")


def run_daily_scrape():
    """Main function — call this daily."""
    today     = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Fetch from both sources
    fts_notices = fetch_fts_notices(
        published_from=yesterday,
        published_to=today
    )
    cf_notices = fetch_contracts_finder_notices(
        published_from=yesterday
    )

    all_notices = fts_notices + cf_notices
    print(f"\n[Total] {len(all_notices)} notices fetched from both sources")

    # 2. Filter by keywords (optional)
    if KEYWORDS:
        filtered = filter_by_keywords(all_notices, KEYWORDS)
        print(f"[Filter] {len(filtered)} notices match keywords: {', '.join(KEYWORDS[:5])}...")
    else:
        filtered = all_notices
        print("[Filter] No keyword filter applied — keeping all notices")

    # 3. Save
    save_to_database(filtered, DB_FILE)
    save_to_csv(filtered, CSV_FILE)

    # 4. Print summary
    print_summary(filtered)

    return filtered


# ─────────────────────────────────────────────────────────────────────────────
# BONUS: Query your database after a few days of running
# ─────────────────────────────────────────────────────────────────────────────

def query_database(keyword: str = None, min_value: float = None, limit: int = 20):
    """
    Example: query your growing database.

    Usage:
        query_database(keyword="AI", min_value=500000)
    """
    conn = sqlite3.connect(DB_FILE)
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
