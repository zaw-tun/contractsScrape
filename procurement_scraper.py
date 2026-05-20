"""
UK Government Procurement Notice Scraper
=========================================
Two proven data sources — no pagination limits, no API caps:

  1. Find a Tender Service (FTS)
     Source : FTS OCDS API with cursor pagination
     Volume : 50–300 notices/day

  2. Contracts Finder (CF)
     Source : Official daily bulk CSV from data.gov.uk
     URL    : https://ckan.publishing.service.gov.uk/dataset/contracts-finder-notices-MM-YYYY
     Volume : 300–800 notices/day (COMPLETE — no API caps)

The CF OCDS API was returning only 100 results because it serves a
cached/demo response regardless of date parameters. The bulk CSV on
data.gov.uk is the authoritative complete daily dataset.

Requirements:
    pip install requests pandas
"""

import io
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

KEYWORDS      = []       # [] = fetch ALL; ["data","AI","digital"] to filter
LOOKBACK_DAYS = 1        # days back to fetch; increase to 7 or 30 to backfill
DB_FILE       = "procurement.db"
CSV_PREFIX    = "procurement_latest"
API_DELAY     = 1.0      # seconds between FTS API requests

# ─────────────────────────────────────────────────────────────────────────────


# ═════════════════════════════════════════════════════════════════════════════
# SOURCE 1: FTS — cursor-based API pagination
# ═════════════════════════════════════════════════════════════════════════════

def fetch_fts_notices(updated_from: str, updated_to: str) -> list:
    """
    Fetch all FTS notices via cursor-based pagination.
    updated_from / updated_to must be full ISO datetime strings:
        e.g. "2026-05-19T00:00:00"
    """
    base_url    = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_notices = []
    cursor      = None
    page        = 1

    print(f"\n[FTS] Fetching notices {updated_from[:10]} → {updated_to[:10]}")

    while True:
        params = {"updatedFrom": updated_from, "updatedTo": updated_to, "limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            r = requests.get(base_url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [FTS] HTTP error page {page}: {e}")
            break
        except Exception as e:
            print(f"  [FTS] Error page {page}: {e}")
            break

        releases = data.get("releases", [])
        if not releases:
            print(f"  [FTS] Page {page}: empty — done")
            break

        for rel in releases:
            n = _parse_fts(rel)
            if n:
                all_notices.append(n)

        cursor_next = data.get("cursor")
        print(f"  [FTS] Page {page}: +{len(releases)} | total={len(all_notices)} | "
              f"next={'yes' if cursor_next else 'none (last page)'}")

        if not cursor_next:
            break
        cursor = cursor_next
        page  += 1
        time.sleep(API_DELAY)

    print(f"  [FTS] Complete: {len(all_notices)} notices")
    return all_notices


def _parse_fts(release: dict):
    try:
        tender = release.get("tender", {}) or {}
        buyer  = release.get("buyer",  {}) or {}
        val    = tender.get("value",    {}) or {}
        minval = tender.get("minValue", {}) or {}
        clf    = tender.get("classification", {})
        cpv    = (", ".join(c.get("id","") for c in clf)
                  if isinstance(clf, list) else
                  clf.get("id","") if isinstance(clf, dict) else "")
        return {
            "source":         "FTS",
            "notice_id":      release.get("ocid","") + "_" + release.get("id",""),
            "title":          tender.get("title", ""),
            "description":    (tender.get("description") or "")[:500],
            "organisation":   buyer.get("name", ""),
            "value_low":      minval.get("amount"),
            "value_high":     val.get("amount"),
            "currency":       val.get("currency", "GBP"),
            "published_date": (release.get("date") or "")[:10],
            "deadline":       ((tender.get("tenderPeriod") or {}).get("endDate") or "")[:10],
            "notice_type":    (release.get("tag") or [""])[0],
            "status":         tender.get("status", ""),
            "cpv_codes":      cpv,
            "url":            "https://www.find-tender.service.gov.uk/Notice/" + release.get("id",""),
            "scraped_at":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"  [FTS] Parse error: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# SOURCE 2: Contracts Finder — official daily bulk CSV from data.gov.uk
# ═════════════════════════════════════════════════════════════════════════════

def _cf_dataset_url(year: int, month: int) -> str:
    """Build the data.gov.uk dataset page URL for a given year/month."""
    return f"https://ckan.publishing.service.gov.uk/dataset/contracts-finder-notices-{month:02d}-{year}"


def _get_cf_daily_csv_url(target_date: datetime) -> str:
    """
    Scrape the data.gov.uk dataset page to find the direct download URL
    for a specific date's CSV file.
    """
    dataset_url = _cf_dataset_url(target_date.year, target_date.month)
    date_str    = target_date.strftime("%Y-%m-%d")

    try:
        r = requests.get(dataset_url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [CF] Could not fetch dataset page: {e}")
        return None

    # Find the CSV resource link for this specific date
    # Pattern: href="...resource/UUID" with link text containing the date
    pattern = rf'href="(/dataset/[^"]+/resource/[a-f0-9\-]+)"[^>]*>\s*Contracts Finder OCDS {re.escape(date_str)}'
    match   = re.search(pattern, r.text)

    if not match:
        # Try a broader search for any link containing the date
        pattern2 = rf'href="(/dataset/[^"]+/resource/[a-f0-9\-]+)"'
        # Find all resource links and check page text for date reference nearby
        all_links = re.findall(r'href="(/dataset/contracts-finder[^"]+/resource/[a-f0-9\-]+)"', r.text)
        # Find position of date string in page
        date_pos = r.text.find(date_str)
        if date_pos > 0 and all_links:
            # Find the closest resource link to the date mention
            for link in all_links:
                link_pos = r.text.find(link)
                if abs(link_pos - date_pos) < 500:
                    return "https://ckan.publishing.service.gov.uk" + link
        print(f"  [CF] Could not find CSV link for {date_str} on dataset page")
        print(f"  [CF] Dataset page: {dataset_url}")
        return None

    resource_page_url = "https://ckan.publishing.service.gov.uk" + match.group(1)

    # Fetch the resource page to get the actual download URL
    try:
        r2 = requests.get(resource_page_url, timeout=30)
        r2.raise_for_status()
        # Extract the download link
        dl_match = re.search(r'href="(https://[^"]+\.csv[^"]*)"[^>]*>.*?(?:Download|Go to resource)', r2.text, re.DOTALL)
        if dl_match:
            return dl_match.group(1)
        # Try alternate pattern
        dl_match2 = re.search(r'"(https://[^"]+contracts.finder[^"]+\.csv[^"]*)"', r2.text)
        if dl_match2:
            return dl_match2.group(1)
    except Exception as e:
        print(f"  [CF] Could not fetch resource page: {e}")

    return resource_page_url  # return resource page as fallback


def _build_cf_direct_url(target_date: datetime) -> str:
    """
    Build the direct CSV download URL using the known pattern.
    Format observed: contractsfinder.service.gov.uk/Published/OCDS/YYYY-MM-DD.csv
    """
    date_str = target_date.strftime("%Y-%m-%d")
    # Direct bulk download endpoint (official CF bulk data)
    return f"https://www.contractsfinder.service.gov.uk/Published/OCDS/{date_str}.csv"


def fetch_cf_from_bulk_csv(target_date: datetime) -> list:
    """
    Download the complete daily bulk CSV for Contracts Finder from data.gov.uk.
    This contains ALL notices for the day — no pagination, no limits.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    print(f"\n[CF] Fetching bulk CSV for {date_str}")

    # Try direct CF bulk download URL first
    direct_url = _build_cf_direct_url(target_date)
    print(f"  [CF] Trying direct URL: {direct_url}")

    try:
        r = requests.get(direct_url, timeout=60, stream=True)
        if r.status_code == 200:
            content = r.content
            print(f"  [CF] Downloaded {len(content)/1024:.0f} KB")
            return _parse_cf_bulk_csv(content, date_str)
    except Exception as e:
        print(f"  [CF] Direct URL failed: {e}")

    # Fallback: scrape data.gov.uk for the resource link
    print(f"  [CF] Trying data.gov.uk dataset page...")
    csv_url = _get_cf_daily_csv_url(target_date)

    if not csv_url:
        print(f"  [CF] Could not find CSV for {date_str}")
        return []

    print(f"  [CF] Downloading from: {csv_url}")
    try:
        r = requests.get(csv_url, timeout=60, stream=True)
        r.raise_for_status()
        content = r.content
        print(f"  [CF] Downloaded {len(content)/1024:.0f} KB")
        return _parse_cf_bulk_csv(content, date_str)
    except Exception as e:
        print(f"  [CF] Download failed: {e}")
        return []


def _parse_cf_bulk_csv(content: bytes, date_str: str) -> list:
    """
    Parse the bulk OCDS CSV from Contracts Finder.
    The CSV uses a flattened OCDS format with dot-notation columns.
    """
    try:
        df = pd.read_csv(io.BytesIO(content), dtype=str, low_memory=False)
        print(f"  [CF] Parsed {len(df)} rows, {len(df.columns)} columns")

        notices = []
        for _, row in df.iterrows():
            # Map flattened OCDS CSV columns to our standard schema
            # Column names vary — try multiple fallback names
            def get(row, *keys, default=""):
                for k in keys:
                    if k in row.index and pd.notna(row[k]) and str(row[k]).strip():
                        return str(row[k]).strip()
                return default

            def get_num(row, *keys):
                for k in keys:
                    if k in row.index and pd.notna(row[k]):
                        try:
                            return float(str(row[k]).replace(",","").strip())
                        except:
                            pass
                return None

            notice_id = get(row,
                "id", "ocid", "release/id",
                "releases/0/id", "release.id"
            )
            ocid = get(row, "ocid", "releases/0/ocid", "release/ocid")
            if not notice_id:
                continue

            notices.append({
                "source":         "Contracts Finder",
                "notice_id":      f"{ocid}_{notice_id}" if ocid else notice_id,
                "title":          get(row, "tender/title", "releases/0/tender/title", "title"),
                "description":    get(row, "tender/description", "releases/0/tender/description", "description")[:500],
                "organisation":   get(row, "buyer/name", "releases/0/buyer/name", "parties/0/name"),
                "value_low":      get_num(row, "tender/minValue/amount", "releases/0/tender/minValue/amount"),
                "value_high":     get_num(row, "tender/value/amount", "releases/0/tender/value/amount", "value/amount"),
                "currency":       get(row, "tender/value/currency", "releases/0/tender/value/currency", default="GBP"),
                "published_date": get(row, "date", "releases/0/date", "publishedDate", default=date_str)[:10],
                "deadline":       get(row, "tender/tenderPeriod/endDate", "releases/0/tender/tenderPeriod/endDate")[:10] if get(row, "tender/tenderPeriod/endDate", "releases/0/tender/tenderPeriod/endDate") else "",
                "notice_type":    get(row, "tag", "releases/0/tag", "releases/0/tag/0"),
                "status":         get(row, "tender/status", "releases/0/tender/status"),
                "cpv_codes":      get(row, "tender/classification/id", "releases/0/tender/classification/id"),
                "url":            get(row, "tender/documents/0/url", "releases/0/tender/documents/0/url",
                                       default=f"https://www.contractsfinder.service.gov.uk/Notice/{notice_id}"),
                "scraped_at":     datetime.now(timezone.utc).isoformat(),
            })

        print(f"  [CF] Complete: {len(notices)} notices from bulk CSV")
        return notices

    except Exception as e:
        print(f"  [CF] CSV parse error: {e}")
        import traceback
        traceback.print_exc()
        return []


# ═════════════════════════════════════════════════════════════════════════════
# STORAGE & OUTPUT
# ═════════════════════════════════════════════════════════════════════════════

def filter_by_keywords(notices: list, keywords: list) -> list:
    if not keywords:
        return notices
    kws = [k.lower() for k in keywords]
    return [n for n in notices
            if any(kw in (n.get("title","") + " " + n.get("description","")).lower()
                   for kw in kws)]


def save_to_database(notices: list, db_file: str):
    if not notices:
        print("[DB] Nothing to save.")
        return
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            notice_id TEXT PRIMARY KEY, source TEXT, title TEXT,
            description TEXT, organisation TEXT, value_low REAL,
            value_high REAL, currency TEXT, published_date TEXT,
            deadline TEXT, notice_type TEXT, status TEXT,
            cpv_codes TEXT, url TEXT, scraped_at TEXT
        )
    """)
    inserted = skipped = 0
    for n in notices:
        try:
            cur = conn.execute("""
                INSERT OR IGNORE INTO notices VALUES (
                    :notice_id,:source,:title,:description,:organisation,
                    :value_low,:value_high,:currency,:published_date,:deadline,
                    :notice_type,:status,:cpv_codes,:url,:scraped_at
                )""", n)
            inserted += cur.rowcount
            skipped  += (1 - cur.rowcount)
        except Exception as e:
            print(f"  [DB] {e}")
    conn.commit()
    conn.close()
    print(f"[DB] Inserted: {inserted} | Duplicates skipped: {skipped}")


def save_to_csv(notices: list, prefix: str):
    if not notices:
        print("[CSV] Nothing to save.")
        return
    fname = f"{prefix}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    pd.DataFrame(notices).to_csv(fname, index=False)
    print(f"[CSV] {len(notices):,} notices → {fname}")


def print_summary(notices: list):
    if not notices:
        print("No notices found.")
        return
    df  = pd.DataFrame(notices)
    val = pd.to_numeric(df["value_high"], errors="coerce").dropna()
    print(f"\n{'='*60}")
    print(f"  SUMMARY — {datetime.now().strftime('%d %B %Y')}")
    print(f"{'='*60}")
    print(f"  Total notices        : {len(notices):,}")
    for src in df["source"].unique():
        print(f"  {src:<25}: {(df['source']==src).sum():,}")
    if not val.empty:
        print(f"  With declared value  : {len(val):,}")
        print(f"  Total spend declared : £{val.sum()/1e6:.1f}m")
        print(f"  Largest contract     : £{val.max()/1e6:.2f}m")
    print(f"\n  By notice type:")
    for t, c in df["notice_type"].value_counts().items():
        if t:
            print(f"    {t:<30} {c:>5}")
    print(f"{'='*60}\n")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def run_daily_scrape():
    today     = datetime.now()
    from_date = today - timedelta(days=LOOKBACK_DAYS)

    print(f"\n{'='*60}")
    print(f"  UK Procurement Scraper")
    print(f"  Range    : {from_date.strftime('%d %b %Y')} → {today.strftime('%d %b %Y')}")
    print(f"  Keywords : {'ALL' if not KEYWORDS else ', '.join(KEYWORDS)}")
    print(f"  CF source: data.gov.uk daily bulk CSV (complete, no API cap)")
    print(f"{'='*60}")

    # FTS: API with cursor pagination
    fts_from = from_date.strftime("%Y-%m-%dT00:00:00")
    fts_to   = today.strftime("%Y-%m-%dT23:59:59")
    fts      = fetch_fts_notices(fts_from, fts_to)

    # CF: daily bulk CSV — fetch for each day in range
    cf = []
    current = from_date
    while current.date() <= today.date():
        daily = fetch_cf_from_bulk_csv(current)
        cf.extend(daily)
        current += timedelta(days=1)
        if current.date() <= today.date():
            time.sleep(API_DELAY)

    all_notices = fts + cf
    print(f"\n[Total] {len(all_notices):,} notices ({len(fts):,} FTS + {len(cf):,} CF)")

    filtered = filter_by_keywords(all_notices, KEYWORDS)
    if KEYWORDS:
        print(f"[Filter] {len(filtered):,} match keywords")
    else:
        filtered = all_notices

    save_to_database(filtered, DB_FILE)
    save_to_csv(filtered, CSV_PREFIX)
    print_summary(filtered)
    return filtered


if __name__ == "__main__":
    run_daily_scrape()
