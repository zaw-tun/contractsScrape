"""
UK Government Procurement Notice Scraper
=========================================
Fetches ALL notices from both APIs using cursor-based pagination.

IMPORTANT FIX: Both APIs use cursor tokens — NOT page numbers.
Each response returns a 'cursor' value to fetch the next batch.
Loop continues until no cursor is returned = all pages exhausted.

API limits (hard caps — cannot be changed):
  FTS              : max 100 per request, cursor pagination
  Contracts Finder : max 100 per request, cursor pagination

On a typical day expect:
  FTS              :  50–300 notices
  Contracts Finder : 300–800 notices
  Total            : 400–1,100+ notices per day

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

KEYWORDS      = []       # [] = fetch ALL; or e.g. ["data", "AI", "digital"]
LOOKBACK_DAYS = 1        # days back to fetch (increase to 7 or 30 to backfill)
DB_FILE       = "procurement.db"
CSV_PREFIX    = "procurement_latest"
API_DELAY     = 1.0      # seconds between requests (CF rate-limits at 403 if too fast)

# ─────────────────────────────────────────────────────────────────────────────


def fetch_fts_notices(updated_from: str, updated_to: str) -> list:
    """
    Fetch ALL FTS notices using cursor-based pagination.
    Keeps fetching until API returns no cursor (= last page reached).
    """
    base_url    = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_notices = []
    cursor      = None
    page        = 1

    print(f"\n[FTS] Fetching notices {updated_from[:10]} → {updated_to[:10]}")

    while True:
        params = {
            "updatedFrom": updated_from,
            "updatedTo":   updated_to,
            "limit":       100,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            r = requests.get(base_url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [FTS] HTTP error page {page}: {e} — {r.text[:200]}")
            break
        except Exception as e:
            print(f"  [FTS] Error page {page}: {e}")
            break

        releases = data.get("releases", [])
        if not releases:
            print(f"  [FTS] Page {page}: no releases returned — done")
            break

        for rel in releases:
            n = _parse_fts(rel)
            if n:
                all_notices.append(n)

        cursor_next = data.get("cursor")
        print(f"  [FTS] Page {page}: +{len(releases)} notices | total={len(all_notices)} | cursor={'yes' if cursor_next else 'none (last page)'}")

        if not cursor_next:
            break

        cursor = cursor_next
        page  += 1
        time.sleep(API_DELAY)

    print(f"  [FTS] Done: {len(all_notices)} total")
    return all_notices


def fetch_cf_notices(published_from: str) -> list:
    """
    Fetch ALL Contracts Finder notices using cursor-based pagination.

    FIXED: CF also uses cursor tokens, NOT page numbers.
    The cursor is returned in the response and passed back on the next call.
    """
    base_url    = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    all_notices = []
    cursor      = None
    page        = 1

    # CF needs full datetime format
    published_from_dt = published_from + "T00:00:00"
    published_to_dt   = datetime.now().strftime("%Y-%m-%dT23:59:59")

    print(f"\n[CF] Fetching notices {published_from} → today")

    while True:
        params = {
            "publishedFrom": published_from_dt,
            "publishedTo":   published_to_dt,
            "limit":         100,
        }
        if cursor:
            params["cursor"] = cursor

        headers = {
            "Accept":     "application/json",
            "User-Agent": "UKProcurementResearch/1.0",
        }

        try:
            r = requests.get(base_url, params=params, headers=headers, timeout=30)

            # Rate limit hit — wait and retry
            if r.status_code == 403:
                print(f"  [CF] Rate limited (403) — waiting 60 seconds...")
                time.sleep(60)
                continue

            r.raise_for_status()
            data = r.json()

        except requests.exceptions.HTTPError as e:
            print(f"  [CF] HTTP error page {page}: {e} — {r.text[:200]}")
            break
        except Exception as e:
            print(f"  [CF] Error page {page}: {e}")
            break

        releases = data.get("releases", [])
        if not releases:
            print(f"  [CF] Page {page}: no releases returned — done")
            break

        for rel in releases:
            n = _parse_cf(rel)
            if n:
                all_notices.append(n)

        cursor_next = data.get("cursor")
        print(f"  [CF] Page {page}: +{len(releases)} notices | total={len(all_notices)} | cursor={'yes' if cursor_next else 'none (last page)'}")

        if not cursor_next:
            break

        cursor = cursor_next
        page  += 1
        time.sleep(API_DELAY)

    print(f"  [CF] Done: {len(all_notices)} total")
    return all_notices


def _parse_fts(release: dict):
    try:
        tender = release.get("tender", {}) or {}
        buyer  = release.get("buyer",  {}) or {}
        val    = tender.get("value",    {}) or {}
        minval = tender.get("minValue", {}) or {}
        clf    = tender.get("classification", {})
        cpv    = ", ".join(c.get("id","") for c in clf) if isinstance(clf, list) else (clf.get("id","") if isinstance(clf, dict) else "")
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


def _parse_cf(release: dict):
    try:
        tender = release.get("tender", {}) or {}
        buyer  = release.get("buyer",  {}) or {}
        val    = tender.get("value",    {}) or {}
        minval = tender.get("minValue", {}) or {}
        clf    = tender.get("classification", {})
        cpv    = ", ".join(c.get("id","") for c in clf) if isinstance(clf, list) else (clf.get("id","") if isinstance(clf, dict) else "")
        return {
            "source":         "Contracts Finder",
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
            "url":            "https://www.contractsfinder.service.gov.uk/Notice/" + release.get("id",""),
            "scraped_at":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"  [CF] Parse error: {e}")
        return None


def filter_by_keywords(notices: list, keywords: list) -> list:
    if not keywords:
        return notices
    kws = [k.lower() for k in keywords]
    return [n for n in notices if any(kw in (n.get("title","") + " " + n.get("description","")).lower() for kw in kws)]


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
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
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
    print(f"[CSV] {len(notices)} notices → {fname}")


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
    print(f"  FTS                  : {(df['source']=='FTS').sum():,}")
    print(f"  Contracts Finder     : {(df['source']=='Contracts Finder').sum():,}")
    print(f"  With declared value  : {len(val):,}")
    if not val.empty:
        print(f"  Total spend declared : £{val.sum()/1e6:.1f}m")
        print(f"  Largest contract     : £{val.max()/1e6:.2f}m")
    print(f"\n  By notice type:")
    for t, c in df["notice_type"].value_counts().items():
        print(f"    {t:<28} {c:>5}")
    print(f"{'='*60}\n")


def run_daily_scrape():
    today     = datetime.now()
    from_date = today - timedelta(days=LOOKBACK_DAYS)
    fts_from  = from_date.strftime("%Y-%m-%dT00:00:00")
    fts_to    = today.strftime("%Y-%m-%dT23:59:59")
    cf_from   = from_date.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  UK Procurement Scraper")
    print(f"  Range: {from_date.strftime('%d %b %Y')} → {today.strftime('%d %b %Y')}")
    print(f"  Keywords: {'ALL' if not KEYWORDS else ', '.join(KEYWORDS)}")
    print(f"{'='*60}")

    fts = fetch_fts_notices(fts_from, fts_to)
    cf  = fetch_cf_notices(cf_from)
    all_notices = fts + cf

    print(f"\n[Total] {len(all_notices):,} notices ({len(fts):,} FTS + {len(cf):,} CF)")

    filtered = filter_by_keywords(all_notices, KEYWORDS)
    if KEYWORDS:
        print(f"[Filter] {len(filtered):,} match keywords")

    save_to_database(filtered, DB_FILE)
    save_to_csv(filtered, CSV_PREFIX)
    print_summary(filtered)
    return filtered


if __name__ == "__main__":
    run_daily_scrape()
