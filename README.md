# contractsScrape

Automated daily scraper for UK government procurement notices, with LLM-powered sector classification and multi-dimensional analysis.

---

## What it does

- Fetches **all published procurement notices** every day from two official UK government sources
- Stores results in a growing **SQLite database** and dated **CSV files**
- Classifies contracts into **15 industrial sectors** using local NLP (no API cost)
- Produces a **multi-sheet Excel workbook** and **text summary report**
- Runs automatically every morning via **GitHub Actions** — no server needed

---

## Data Sources

| Source | What it covers | Method |
|---|---|---|
| [Find a Tender (FTS)](https://www.find-tender.service.gov.uk) | High-value contracts (>£138k) — central govt, NHS, defence, utilities | OCDS API, cursor pagination |
| [Contracts Finder](https://www.contractsfinder.service.gov.uk) | Lower-value contracts (>£12k) — wider public sector | Official daily bulk CSV via data.gov.uk |

---

## Repository Structure

```
contractsScrape/
├── procurement_scraper.py       # Daily scraper — fetches all notices
├── procurement_analysis.py      # Analysis + sector classification
├── requirements.txt             # Python dependencies
├── procurement.db               # SQLite database (grows daily)
├── procurement_latest_YYYY-MM-DD.csv   # Daily CSV output
├── procurement_analysis_YYYY-MM-DD.xlsx  # Analysis workbook
└── .github/
    └── workflows/
        └── daily_scrape.yml     # GitHub Actions — runs 7am UTC daily
```

---

## Files

### `procurement_scraper.py`
Fetches all new procurement notices and saves them locally.

**Configure at the top of the file:**
```python
KEYWORDS      = []   # Filter by keywords, e.g. ["AI", "data", "digital"]
                     # Leave empty [] to fetch ALL notices
LOOKBACK_DAYS = 1    # How many days back to fetch (increase to backfill history)
```

**Run manually:**
```bash
python procurement_scraper.py
```

---

### `procurement_analysis.py`
Classifies contracts into sectors using local NLP and produces analysis reports.

**Run options:**
```bash
# Full analysis with NLP sector classification
python procurement_analysis.py

# Specify a CSV file
python procurement_analysis.py procurement_latest_2026-05-20.csv

# Keyword-only mode (instant, no model download)
python procurement_analysis.py --keyword-only

# Use lightweight model (~120 MB vs default 1.6 GB)
python procurement_analysis.py --model fast
```

**Output — Excel workbook with 8 sheets:**
- All Contracts
- Sector Analysis
- Contract Types
- Value Bands
- Top Buyers
- Sector × Buyer
- Sector × Type
- Open Opportunities
- Classification Quality

---

## Sector Classification

Contracts are classified into 15 sectors using a two-layer approach:

| Layer | Method | Coverage |
|---|---|---|
| 1 | Keyword rules (instant) | ~80% of contracts |
| 2 | Zero-shot BART/BERT model (local, offline) | Remaining ~20% |

**Sectors:**
Construction & Infrastructure · Health & Social Care · Education & Training · Digital & Technology · Transport & Logistics · Professional Services · Facilities Management · Defence & Security · Environment & Utilities · Finance & Legal · Marketing & Communications · Food & Catering · Housing & Real Estate · Research & Innovation · Other

---

## Setup

### 1. Install dependencies
```bash
pip install requests pandas transformers torch openpyxl
```

### 2. Clone the repo
```bash
git clone https://github.com/zaw-tun/contractsScrape.git
cd contractsScrape
```

### 3. Run the scraper
```bash
python procurement_scraper.py
```

### 4. Run the analysis
```bash
python procurement_analysis.py --keyword-only   # instant, no model download
python procurement_analysis.py                  # full NLP (downloads ~1.6 GB model first time)
```

---

## Automated Daily Runs (GitHub Actions)

The scraper runs automatically every day at **7am UTC** via GitHub Actions.

Results are committed back to the repository automatically — new CSV and database entries appear in the repo each morning.

**To trigger a manual run:**
1. Go to the **Actions** tab in GitHub
2. Click **Daily Procurement Scraper**
3. Click **Run workflow**

**To enable write permissions** (required for auto-commit):
Settings → Actions → General → Workflow permissions → Read and write

---

## Querying the Database

After a few days of running, query the growing database locally:

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("procurement.db")

# All AI/data contracts over £500k
df = pd.read_sql("""
    SELECT title, organisation, value_high, published_date, url
    FROM notices
    WHERE (title LIKE '%data%' OR title LIKE '%AI%')
    AND value_high >= 500000
    ORDER BY value_high DESC
""", conn)

print(df)
```

---

## Notice Types

| Type | Meaning |
|---|---|
| `tender` | Open competition — can bid now |
| `planning` | Early market engagement — upcoming opportunity |
| `award` | Contract already awarded |
| `tenderUpdate` | Amendment to an open tender |

**SME opportunities** are flagged automatically: open notices with value under £2m.

---

## License

Data sourced from [Find a Tender](https://www.find-tender.service.gov.uk) and [Contracts Finder](https://www.contractsfinder.service.gov.uk) under the [Open Government Licence v3.0](http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
