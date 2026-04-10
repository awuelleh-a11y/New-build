#!/usr/bin/env python3
"""
Contractor Scraper — with auto nearby-city discovery and merge

Sources:
  - BBB (free, all cities)
  - San Antonio City BILS portal (sanantonio.gov, SA only — includes license data)
  - Google Places API (optional — pass --api-key KEY, all cities)

Nearby city discovery:
  Downloads simplemaps US cities CSV (free, cached locally) to find all cities
  within --nearby-miles (default 50) of the target city, then scrapes and merges
  them all into the target city's output CSV.

Output: one CSV per target city (includes contractors from all nearby cities)
  contractors_Seaford_DE.csv
  contractors_Mansfield_TX.csv
  ...

Usage:
  python3 scrape_contractors.py --city "Seaford,DE" --no-defaults
  python3 scrape_contractors.py --city "Baltimore,MD" --no-defaults --nearby-miles 30
  python3 scrape_contractors.py --city "Mansfield,TX" --no-defaults --no-nearby
"""

import argparse
import csv
import io
import math
import os
import re
import sys
import time
import random
import zipfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

CITY_SOURCES_PATH = Path(__file__).parent / "city_sources.yaml"

# ─────────────────────────────────────────────────────────────
# Nearby-city discovery (simplemaps US cities database)
# ─────────────────────────────────────────────────────────────

# GeoNames US cities dataset (free, no account needed, ~30k cities with lat/lon)
CITIES_DB_URL = "http://download.geonames.org/export/dump/US.zip"
CITIES_DB_PATH = Path(__file__).parent / ".cache" / "uscities.csv"

DEFAULT_NEARBY_MILES = 50


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _ensure_cities_db() -> bool:
    if CITIES_DB_PATH.exists():
        return True
    CITIES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"  [cities-db] Downloading GeoNames US cities database …")
    try:
        resp = requests.get(CITIES_DB_URL, timeout=60)
        resp.raise_for_status()
        # GeoNames US.zip contains US.txt (tab-delimited) + readme
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            txt_name = next(n for n in z.namelist() if n == "US.txt")
            raw = z.read(txt_name).decode("utf-8")

        # GeoNames columns (tab-delimited, 19 cols):
        #   0:geonameid  1:name  4:lat  5:lng  6:feature_class  10:admin1(state abbr)  14:population
        # Filter to populated places (feature_class=P) only
        # admin1 is already the 2-letter state abbreviation (AK, TX, etc.)
        VALID_STATES = {
            "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL",
            "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE",
            "NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD",
            "TN","TX","UT","VT","VA","WA","WV","WI","WY","PR",
        }

        rows = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 15:
                continue
            if parts[6] != "P":  # populated places only
                continue
            name = parts[1]
            lat_s, lng_s = parts[4], parts[5]
            state_abbr = parts[10].upper()
            population = parts[14] or "0"
            if state_abbr not in VALID_STATES or not lat_s or not lng_s:
                continue
            # Escape commas in city names
            name_escaped = name.replace(",", " ")
            rows.append(f"{name_escaped},{state_abbr},{lat_s},{lng_s},{population}")

        CITIES_DB_PATH.write_text("city,state_id,lat,lng,population\n" + "\n".join(rows), encoding="utf-8")
        print(f"  [cities-db] Saved {len(rows):,} populated places → {CITIES_DB_PATH}")
        return True
    except Exception as exc:
        print(f"  [cities-db] Download failed: {exc}")
        return False


DEFAULT_MIN_POPULATION = 5000
DEFAULT_MAX_NEARBY = 30


def _get_latlon(city_name: str, state: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for a city from the cities DB, or None if not found."""
    if not _ensure_cities_db():
        return None
    try:
        with open(CITIES_DB_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["city"].lower() == city_name.lower() and row["state_id"].upper() == state.upper():
                    return (float(row["lat"]), float(row["lng"]))
    except Exception:
        pass
    return None


def get_nearby_cities(
    city_name: str,
    state: str,
    max_miles: float = DEFAULT_NEARBY_MILES,
    min_population: int = DEFAULT_MIN_POPULATION,
    max_nearby: int = DEFAULT_MAX_NEARBY,
) -> list[tuple[str, str]]:
    """
    Returns list of (city_name, state) tuples within max_miles of city_name, state.
    Filters to places with population >= min_population and caps at max_nearby cities.
    Excludes the target city itself.
    """
    if not _ensure_cities_db():
        return []

    target_lat = target_lon = None
    nearby: list[tuple[str, str, float]] = []

    try:
        with open(CITIES_DB_PATH, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        for row in rows:
            if row["city"].lower() == city_name.lower() and row["state_id"].upper() == state.upper():
                try:
                    target_lat = float(row["lat"])
                    target_lon = float(row["lng"])
                    break
                except (ValueError, KeyError):
                    continue

        if target_lat is None:
            print(f"  [cities-db] Could not find coordinates for {city_name}, {state}")
            return []

        for row in rows:
            try:
                lat = float(row["lat"])
                lon = float(row["lng"])
                name = row["city"]
                st = row["state_id"].upper()
                pop = int(row.get("population", 0) or 0)
            except (ValueError, KeyError):
                continue

            if name.lower() == city_name.lower() and st == state.upper():
                continue

            if pop < min_population:
                continue

            dist = _haversine_miles(target_lat, target_lon, lat, lon)
            if dist <= max_miles:
                nearby.append((name, st, dist))

        nearby.sort(key=lambda x: x[2])
        nearby = nearby[:max_nearby]
        result = [(name, st) for name, st, _ in nearby]
        print(f"  [cities-db] Found {len(result)} cities within {max_miles} mi of {city_name}, {state} (pop≥{min_population:,}, cap={max_nearby})")
        for name, st, dist in nearby[:10]:
            print(f"    {name}, {st}  ({dist:.1f} mi)")
        if len(nearby) > 10:
            print(f"    … and {len(nearby) - 10} more")
        return result

    except Exception as exc:
        print(f"  [cities-db] Error finding nearby cities: {exc}")
        return []

# Load .env from same directory as this script
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

CITIES = [
    {"name": "Odessa",      "state": "TX", "output": "contractors_Odessa_TX.csv"},
    {"name": "San Antonio", "state": "TX", "output": "contractors_SanAntonio_TX.csv"},
    {"name": "Edinburg",    "state": "TX", "output": "contractors_Edinburg_TX.csv"},
]

BBB_TERMS = [
    "hvac contractors",
    "general contractors",
    "air conditioning contractors",
    "heating contractors",
    "hvac repair",
    "heating and air conditioning",
    "mechanical contractors",
    "plumbing heating cooling",
]

GOOGLE_QUERIES = [
    "HVAC contractor",
    "air conditioning contractor",
    "heating contractor",
    "general contractor",
    "AC repair",
    "HVAC repair",
]

SA_BILS_URL = "https://webapp9.sanantonio.gov/BILS/IndexContractorConnect.aspx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

MIN_DELAY = 1.2
MAX_DELAY = 2.8
MAX_BBB_PAGES = 12


# ─────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────

@dataclass
class Contractor:
    name: str
    phone: str = ""
    address: str = ""
    city: str = ""
    state: str = "TX"
    zip_code: str = ""
    email: str = ""
    website: str = ""
    category: str = ""
    license_number: str = ""
    license_type: str = ""
    license_expiry: str = ""
    types_of_work: str = ""
    bbb_rating: str = ""
    google_rating: str = ""
    review_count: str = ""
    bbb_profile_url: str = ""
    source: str = ""

    @property
    def dedup_key(self) -> str:
        norm_name = re.sub(r"[^a-z0-9]", "", self.name.lower())
        norm_phone = re.sub(r"\D", "", self.phone)
        if norm_phone:
            return f"{norm_name}|{norm_phone}"
        norm_addr = re.sub(r"[^a-z0-9]", "", (self.address + self.city).lower())
        return f"{norm_name}|{norm_addr}"


# ─────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_soup(url: str, retries: int = 3) -> Optional[BeautifulSoup]:
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=20)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "lxml")
            if resp.status_code == 429:
                wait = 15 + attempt * 10
                print(f"    [rate-limited] sleeping {wait}s …")
                time.sleep(wait)
            else:
                print(f"    [HTTP {resp.status_code}] {url}")
                return None
        except requests.RequestException as exc:
            print(f"    [network error] {exc} (attempt {attempt + 1}/{retries})")
            time.sleep(3)
    return None


def polite_sleep():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


# ─────────────────────────────────────────────────────────────
# BBB scraper
# ─────────────────────────────────────────────────────────────

def parse_bbb_card(card, city_name: str, state: str, term: str) -> Optional[Contractor]:
    try:
        name_el = card.select_one("h3.result-business-name a span[translate='no']")
        if not name_el:
            name_el = card.select_one("h3.result-business-name a")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            return None

        profile_link_el = card.select_one("h3.result-business-name a")
        profile_url = ""
        if profile_link_el and profile_link_el.get("href"):
            profile_url = "https://www.bbb.org" + profile_link_el["href"]

        cat_el = card.select_one("p.bds-body.text-size-4.text-gray-70")
        category = cat_el.get_text(strip=True).rstrip(" .") if cat_el else term.title()

        rating = ""
        rating_el = card.select_one("span.result-rating") or card.select_one("summary.result-rating")
        if rating_el:
            m = re.search(r"BBB Rating:\s*([A-F][+-]?|NR)", rating_el.get_text())
            if m:
                rating = m.group(1)

        info_div = card.select_one("div.result-business-info")
        phone = address = ""
        addr_city = city_name
        addr_state = state
        addr_zip = ""

        if info_div:
            phone_el = info_div.select_one("a[href^='tel:']")
            if phone_el:
                phone = phone_el.get_text(strip=True)

            addr_el = info_div.select_one("p.bds-body.text-size-5.text-gray-70")
            if addr_el:
                raw = addr_el.get_text(" ", strip=True)
                m = re.match(r"^(.+?),\s*(.+?),\s*([A-Z]{2})\s+([\d-]+)", raw)
                if m:
                    address = m.group(1).strip()
                    addr_city = m.group(2).strip()
                    addr_state = m.group(3)
                    addr_zip = m.group(4).split("-")[0]
                else:
                    address = raw.split(",")[0].strip()

        return Contractor(
            name=name,
            phone=phone,
            address=address,
            city=addr_city,
            state=addr_state,
            zip_code=addr_zip,
            category=category,
            bbb_rating=rating,
            bbb_profile_url=profile_url,
            source="BBB",
        )
    except Exception as exc:
        print(f"    [BBB parse error] {exc}")
        return None


def scrape_bbb(city_name: str, state: str, term: str, pbar: "tqdm | None" = None,
               target_latlon: Optional[tuple[float, float]] = None,
               max_miles: float = DEFAULT_NEARBY_MILES) -> list[Contractor]:
    results: list[Contractor] = []
    skipped = 0
    total_pages = None

    for page in range(1, MAX_BBB_PAGES + 1):
        url = (
            f"https://www.bbb.org/search"
            f"?find_text={requests.utils.quote(term)}"
            f"&find_loc={requests.utils.quote(city_name + ', ' + state)}"
            f"&page={page}"
        )
        if pbar:
            pbar.set_postfix_str(f"{city_name} / {term[:20]} / p{page}", refresh=True)
        soup = get_soup(url)
        if soup is None:
            break

        cards = soup.select("div.card.result-card")
        if not cards:
            break

        for card in cards:
            c = parse_bbb_card(card, city_name, state, term)
            if not c:
                continue
            # Distance filter: drop results whose city is known and too far from scrape city
            if target_latlon:
                biz_latlon = _get_latlon(c.city, c.state)
                if biz_latlon:
                    dist = _haversine_miles(*target_latlon, *biz_latlon)
                    if dist > max_miles:
                        skipped += 1
                        continue
            results.append(c)

        if total_pages is None:
            page_nums = [
                int(m.group(1))
                for link in soup.select("a[href*='page=']")
                if (m := re.search(r"page=(\d+)", link.get("href", "")))
            ]
            if page_nums:
                total_pages = max(page_nums)

        if (total_pages and page >= total_pages) or len(cards) < 5:
            break

        polite_sleep()

    if skipped:
        tqdm.write(f"    [distance filter] dropped {skipped} out-of-area results (>{max_miles:.0f} mi) for {city_name}, {state}")
    return results


# ─────────────────────────────────────────────────────────────
# San Antonio BILS scraper (government portal — SA only)
# ─────────────────────────────────────────────────────────────

def scrape_sa_bils() -> list[Contractor]:
    """
    Download all contractors from the City of San Antonio ContractorConnect portal.
    Returns the full list regardless of trade — includes license numbers, email, types of work.
    """
    print("    [SA BILS] Fetching session tokens …")
    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        r = sess.get(SA_BILS_URL, timeout=20)
        soup = BeautifulSoup(r.text, "lxml")

        viewstate = soup.find("input", {"id": "__VIEWSTATE"})["value"]
        vsg = soup.find("input", {"id": "__VIEWSTATEGENERATOR"})["value"]
        ev = soup.find("input", {"id": "__EVENTVALIDATION"})["value"]

        all_cbs = [inp["name"] for inp in soup.find_all("input", {"type": "checkbox"})]

        # Search with all checkboxes ticked
        post_data: dict[str, str] = {
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "Button1": "Find Record",
        }
        for cb in all_cbs:
            post_data[cb] = "on"

        print("    [SA BILS] Running search …")
        r2 = sess.post(SA_BILS_URL, data=post_data, timeout=30)
        soup2 = BeautifulSoup(r2.text, "lxml")

        vs2 = soup2.find("input", {"id": "__VIEWSTATE"})["value"]
        ev2 = soup2.find("input", {"id": "__EVENTVALIDATION"})["value"]

        export_data: dict[str, str] = {
            "__VIEWSTATE": vs2,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev2,
            "Button3": "Excel Export",
        }
        for cb in all_cbs:
            export_data[cb] = "on"

        print("    [SA BILS] Downloading Excel export …")
        r3 = sess.post(SA_BILS_URL, data=export_data, timeout=60)
        html_content = r3.content.decode("utf-16-le", errors="replace")

    except Exception as exc:
        print(f"    [SA BILS] Error: {exc}")
        return []

    soup3 = BeautifulSoup(html_content, "lxml")
    rows = soup3.find_all("tr")
    if len(rows) < 2:
        print("    [SA BILS] No data rows found")
        return []

    # Parse header
    header = [td.get_text(strip=True).lower().replace(" ", "_") for td in rows[0].find_all(["td", "th"])]
    print(f"    [SA BILS] Columns: {header}")

    def col(cells: list, *names: str) -> str:
        for name in names:
            if name in header:
                idx = header.index(name)
                if idx < len(cells):
                    return cells[idx].strip()
        return ""

    results: list[Contractor] = []
    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if not any(cells):
            continue

        # Company name preferred; fall back to individual name
        company = col(cells, "company_name")
        individual = col(cells, "name")
        name = company if company else individual

        if not name:
            continue

        # Format phone: "2105338003" → "(210) 533-8003"
        raw_phone = re.sub(r"\D", "", col(cells, "phone_number"))
        phone = (
            f"({raw_phone[:3]}) {raw_phone[3:6]}-{raw_phone[6:]}"
            if len(raw_phone) == 10
            else raw_phone
        )

        # Parse expiry date: "3/10/2026 12:00:00 AM" → "03/10/2026"
        expiry_raw = col(cells, "expiration_date")
        expiry = ""
        m = re.match(r"(\d+)/(\d+)/(\d+)", expiry_raw)
        if m:
            expiry = f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"

        results.append(Contractor(
            name=name,
            phone=phone,
            address=col(cells, "street_address"),
            city=col(cells, "city") or "San Antonio",
            state=col(cells, "state") or "TX",
            zip_code=col(cells, "zip"),
            email=col(cells, "email"),
            license_number=col(cells, "license", "ap_no"),
            license_type=col(cells, "license_type"),
            license_expiry=expiry,
            types_of_work=col(cells, "types_of_work"),
            category=col(cells, "registered_type") or col(cells, "license_type"),
            source="SA City Portal",
        ))

    print(f"    [SA BILS] Parsed {len(results)} contractors")
    return results


# ─────────────────────────────────────────────────────────────
# Thornton CO Licensed Contractors PDF
# ─────────────────────────────────────────────────────────────

THORNTON_PDF_URL = "https://www.thorntonco.gov/media/file/all-licensed-contractors-2"


def scrape_thornton_pdf() -> list[Contractor]:
    """
    Download the City of Thornton licensed contractors PDF and parse it.
    Column layout (fixed-width):
      Name | Street Address | City, ST Zip | Phone | License No. | License Type
    Covers the broader Denver metro / Adams County area — relevant for Brighton, CO.
    """
    import subprocess
    import tempfile

    print("    [Thornton PDF] Downloading …")
    try:
        resp = requests.get(THORNTON_PDF_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"    [Thornton PDF] Download failed: {exc}")
        return []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(resp.content)
        pdf_path = tmp.name

    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"    [Thornton PDF] pdftotext error: {result.stderr[:200]}")
        return []

    # Use fixed-width column parsing based on license number position (LCC...).
    # LCC token anchors the parse; we split left of it by working backwards.
    # Continuation lines have the license type column only (indent >= 108 chars).
    LCC_RE = re.compile(r"\bLCC\S+")         # license number token
    PHONE_RE = re.compile(r"(\d{3}[-.\s]\d{3}[-.\s]\d{4}|\(\d{3}\)\s*\d{3}[-.\s]\d{4})")
    # Match state+zip anchored to end of the "left-of-phone" slice
    # Allow malformed zips (extra digits, e.g. 802413243)
    STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\s+(\d{5,10}(?:-\d{4})?)\s*$")
    CONT_INDENT = 100  # continuation lines indent >= this many spaces

    results: list[Contractor] = []
    current: dict | None = None

    for raw_line in result.stdout.split("\n"):
        stripped = raw_line.strip()
        if not stripped or "Page " in raw_line or "CITY OF THORNTON" in raw_line or "Current as of" in raw_line:
            continue

        lcc_m = LCC_RE.search(raw_line)
        if lcc_m:
            # Save previous record
            if current:
                results.append(_build_thornton_contractor(current))

            license_no = lcc_m.group(0)
            license_type = raw_line[lcc_m.end():].strip()

            # Everything left of LCC: name | address | CSZ | phone
            left = raw_line[:lcc_m.start()]

            # Find phone (right-most match in left portion)
            phone = ""
            phone_m = None
            for pm in PHONE_RE.finditer(left):
                phone_m = pm
            if phone_m:
                phone = re.sub(r"\s+", "", phone_m.group(0))
                left = left[:phone_m.start()]

            # Find state+zip anchored to the right end of remaining left
            left_rstripped = left.rstrip()
            sz_m = STATE_ZIP_RE.search(left_rstripped)
            addr_city = addr_state = addr_zip = address = ""
            if sz_m:
                addr_state = sz_m.group(1)
                addr_zip = sz_m.group(2).split("-")[0]
                # Before state: "name  address  CITY," — city is before the last comma
                before_state = left_rstripped[:sz_m.start()].rstrip()
                comma_pos = before_state.rfind(",")
                before_comma = (before_state[:comma_pos] if comma_pos >= 0 else before_state).strip()
                # Split by 2+ spaces: [name, address, city] — city is last chunk
                chunks = re.split(r"\s{2,}", before_comma)
                chunks = [c.strip() for c in chunks if c.strip()]
                if len(chunks) >= 3:
                    name = chunks[0].title()
                    address = chunks[1].title()
                    addr_city = " ".join(chunks[2:]).title()
                elif len(chunks) == 2:
                    name = chunks[0].title()
                    address = chunks[1].title()
                elif len(chunks) == 1:
                    name = chunks[0]
                else:
                    name = before_comma
                # Skip the leftover assignment below
                left = ""

            if not addr_state:
                # Fallback: no state/zip found — split remaining left into name+address
                left = left.strip()
                parts = re.split(r"\s{2,}", left, maxsplit=1)
                name = parts[0].strip()
                address = parts[1].strip() if len(parts) > 1 else ""

            current = {
                "name": name,
                "address": address,
                "city": addr_city,
                "state": addr_state,
                "zip_code": addr_zip,
                "phone": phone,
                "license_no": license_no,
                "license_type": license_type,
            }
        elif stripped and current:
            indent = len(raw_line) - len(raw_line.lstrip())
            if indent >= CONT_INDENT:
                # License type continuation
                current["license_type"] += ", " + stripped
            elif indent < 5:
                # Name continuation (very rare, long names that wrap)
                current["name"] += " " + stripped

    if current:
        results.append(_build_thornton_contractor(current))

    Path(pdf_path).unlink(missing_ok=True)
    print(f"    [Thornton PDF] Parsed {len(results)} contractors")
    return results


def _build_thornton_contractor(d: dict) -> Contractor:
    return Contractor(
        name=d["name"],
        address=d["address"],
        city=d["city"],
        state=d["state"],
        zip_code=d["zip_code"],
        phone=d["phone"],
        license_number=d["license_no"],
        license_type=d["license_type"],
        category=d["license_type"].split(",")[0].strip(),
        source="Thornton City PDF",
    )


# ─────────────────────────────────────────────────────────────
# City-specific scraper hooks (plugin registry)
#
# To add a new city portal:
#   1. Write a function:  def scrape_myportal() -> list[Contractor]
#   2. Register it:       register_city_hook("CityName", "ST", scrape_myportal)
#
# The main loop calls all registered hooks automatically — no other changes needed.
# ─────────────────────────────────────────────────────────────

# Registry: (city_lower, state_upper) → list of hook functions
_CITY_HOOKS: dict[tuple[str, str], list] = {}


def register_city_hook(city: str, state: str, fn) -> None:
    key = (city.lower(), state.upper())
    _CITY_HOOKS.setdefault(key, []).append(fn)


def run_city_hooks(city: str, state: str) -> list[Contractor]:
    """Run all registered hooks for the given city/state and return combined results."""
    key = (city.lower(), state.upper())
    hooks = _CITY_HOOKS.get(key, [])
    results: list[Contractor] = []
    for hook in hooks:
        try:
            results.extend(hook())
        except Exception as exc:
            print(f"    [hook:{hook.__name__}] Error: {exc}")
    return results


# ─────────────────────────────────────────────────────────────
# Philadelphia ArcGIS Contractor Lookup (city portal)
# ─────────────────────────────────────────────────────────────

PHILLY_ARCGIS_URL = (
    "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services"
    "/AGO_Lyr_Contractor_Lookup_Application_Eclipse/FeatureServer/0/query"
)
PHILLY_ARCGIS_FIELDS = "CONTRACTORNAME,ADDRESS,PERMITNUMBER,PERMITTYPE,TYPEOFWORK,WORKDESCRIPTION,STATUS,ISSUEDATE"
PHILLY_ARCGIS_PAGE = 2000


def scrape_philly_arcgis() -> list[Contractor]:
    """
    Pull all records from the City of Philadelphia contractor/permit ArcGIS feature service.
    Paginates through all records using resultOffset (max 2000 per page).
    """
    results: list[Contractor] = []
    offset = 0

    while True:
        params = {
            "where": "1=1",
            "outFields": PHILLY_ARCGIS_FIELDS,
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": PHILLY_ARCGIS_PAGE,
            "orderByFields": "CONTRACTORNAME",
        }
        try:
            resp = requests.get(PHILLY_ARCGIS_URL, params=params, timeout=30)
            data = resp.json()
        except Exception as exc:
            print(f"    [Philly ArcGIS] Error at offset {offset}: {exc}")
            break

        if "error" in data:
            print(f"    [Philly ArcGIS] API error: {data['error']}")
            break

        features = data.get("features", [])
        print(f"    [Philly ArcGIS] offset={offset} → {len(features)} records")

        if not features:
            break

        for feat in features:
            attrs = feat.get("attributes", {})
            name = (attrs.get("CONTRACTORNAME") or "").strip()
            if not name:
                continue

            address_raw = (attrs.get("ADDRESS") or "").strip()
            # ADDRESS is typically "123 MAIN ST PHILADELPHIA PA 19103"
            street = addr_city = addr_state = addr_zip = ""
            m = re.match(
                r"^(.+?)\s+PHILADELPHIA\s+(PA)\s+(\d{5})",
                address_raw,
                re.IGNORECASE,
            )
            if m:
                street = m.group(1).strip().title()
                addr_city = "Philadelphia"
                addr_state = "PA"
                addr_zip = m.group(3)
            else:
                street = address_raw

            permit_type = (attrs.get("PERMITTYPE") or "").strip()
            work_type = (attrs.get("TYPEOFWORK") or "").strip()
            work_desc = (attrs.get("WORKDESCRIPTION") or "").strip()
            status = (attrs.get("STATUS") or "").strip()
            permit_no = (attrs.get("PERMITNUMBER") or "").strip()

            issue_date = ""
            ts = attrs.get("ISSUEDATE")
            if ts:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    issue_date = dt.strftime("%m/%d/%Y")
                except Exception:
                    pass

            types_combined = " | ".join(filter(None, [permit_type, work_type, work_desc]))

            results.append(Contractor(
                name=name,
                address=street,
                city=addr_city or "Philadelphia",
                state=addr_state or "PA",
                zip_code=addr_zip,
                license_number=permit_no,
                license_type=permit_type,
                license_expiry=issue_date,
                types_of_work=types_combined,
                category=permit_type or work_type,
                source="Philadelphia City Portal",
            ))

        offset += len(features)
        if len(features) < PHILLY_ARCGIS_PAGE:
            break  # last page

        time.sleep(0.5)  # polite pause between pages

    print(f"    [Philly ArcGIS] Total records fetched: {len(results)}")

    # Deduplicate by contractor name + address within this source
    seen: dict[str, Contractor] = {}
    for c in results:
        k = re.sub(r"[^a-z0-9]", "", c.name.lower())
        if k not in seen:
            seen[k] = c
        else:
            # Accumulate permit types / work descriptions
            existing = seen[k]
            if c.types_of_work and c.types_of_work not in existing.types_of_work:
                existing.types_of_work += " | " + c.types_of_work
    deduped = list(seen.values())
    print(f"    [Philly ArcGIS] After dedup by contractor name: {len(deduped)} unique contractors")
    return deduped


# ─────────────────────────────────────────────────────────────
# Baltimore City DPW Contractor Directory (ASP.NET postback grid)
# https://dpwpublic.baltimorecity.gov/contractormobile/ShowDetail.aspx
# ─────────────────────────────────────────────────────────────

BALTIMORE_DPW_URL = "https://dpwpublic.baltimorecity.gov/contractormobile/ShowDetail.aspx"


def scrape_baltimore_dpw() -> list[Contractor]:
    """
    Paginate through the Baltimore City DPW prequalified contractor directory.
    Uses ASP.NET postback to navigate pages; extracts contractor names and
    then fetches detail pages for phone/address/category.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)
    results: list[Contractor] = []

    def _get_form_state(soup: BeautifulSoup) -> dict:
        return {
            "__VIEWSTATE": (soup.find("input", {"id": "__VIEWSTATE"}) or {}).get("value", ""),
            "__VIEWSTATEGENERATOR": (soup.find("input", {"id": "__VIEWSTATEGENERATOR"}) or {}).get("value", ""),
            "__EVENTVALIDATION": (soup.find("input", {"id": "__EVENTVALIDATION"}) or {}).get("value", ""),
        }

    def _parse_names(soup: BeautifulSoup) -> list[str]:
        names = []
        for row in soup.select("tr"):
            cells = row.find_all("td")
            if len(cells) >= 3:
                name = cells[2].get_text(strip=True)
                if name and name != "Contractor Name":
                    names.append(name)
        return names

    def _get_detail_link(soup: BeautifulSoup, name: str) -> Optional[str]:
        """Find the 'Details' link for a contractor by name."""
        for row in soup.select("tr"):
            cells = row.find_all("td")
            if len(cells) >= 3 and cells[2].get_text(strip=True) == name:
                link = cells[1].find("a")
                if link and link.get("href"):
                    return link["href"]
        return None

    try:
        print("    [Baltimore DPW] Fetching first page …")
        resp = sess.get(BALTIMORE_DPW_URL, timeout=20)
        soup = BeautifulSoup(resp.text, "lxml")

        # Find total pages from pager
        pager_links = soup.select("a[id*=PBN]") or soup.select(".dxpager a")
        page_numbers = []
        for a in soup.find_all("a"):
            txt = a.get_text(strip=True)
            if txt.isdigit():
                page_numbers.append(int(txt))
        total_pages = max(page_numbers) if page_numbers else 1
        print(f"    [Baltimore DPW] Total pages: {total_pages}")

        all_names: list[str] = _parse_names(soup)
        form_state = _get_form_state(soup)

        # Navigate remaining pages via postback
        for page in range(2, total_pages + 1):
            time.sleep(random.uniform(0.8, 1.5))
            post_data = {
                **form_state,
                "__EVENTTARGET": "ASPxGridView5$DXPagerBottom$PBN",
                "__EVENTARGUMENT": str(page - 1),  # 0-indexed
            }
            resp = sess.post(BALTIMORE_DPW_URL, data=post_data, timeout=20)
            page_soup = BeautifulSoup(resp.text, "lxml")
            names = _parse_names(page_soup)
            all_names.extend(names)
            form_state = _get_form_state(page_soup)
            print(f"    [Baltimore DPW] page {page}/{total_pages} → {len(names)} names (total so far: {len(all_names)})")

        print(f"    [Baltimore DPW] Total contractors found: {len(all_names)}")

        # Convert names to Contractor objects (name only — no phone/address on list view)
        seen_names: set[str] = set()
        for name in all_names:
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if key in seen_names or not name:
                continue
            seen_names.add(key)
            results.append(Contractor(
                name=name,
                city="Baltimore",
                state="MD",
                category="General Contractor",
                source="Baltimore City DPW",
            ))

    except Exception as exc:
        print(f"    [Baltimore DPW] Error: {exc}")

    print(f"    [Baltimore DPW] Parsed {len(results)} unique contractors")
    return results


# ─────────────────────────────────────────────────────────────
# Google Places API (optional)
# ─────────────────────────────────────────────────────────────

# Uses Places API (New) — single POST returns all fields including phone + website
PLACES_NEW_SEARCH = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEW_FIELDS = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.nationalPhoneNumber,places.websiteUri,"
    "places.rating,places.userRatingCount,places.primaryTypeDisplayName"
)


def google_places_search_new(query: str, location: str, api_key: str) -> list[dict]:
    """Places API (New) text search — returns up to 20 results per call."""
    results = []
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACES_NEW_FIELDS,
        "Content-Type": "application/json",
    }
    body: dict = {"textQuery": f"{query} in {location}", "maxResultCount": 20}

    # New API doesn't support pagination tokens for text search; make one call per query
    try:
        resp = requests.post(PLACES_NEW_SEARCH, json=body, headers=headers, timeout=15)
        data = resp.json()
        if "error" in data:
            print(f"    [Places API] {data['error'].get('status')} — {data['error'].get('message','')}")
            return []
        results.extend(data.get("places", []))
    except Exception as exc:
        print(f"    [Places API error] {exc}")
    return results


def scrape_google_places(city_name: str, state: str, api_key: str) -> list[Contractor]:
    results: list[Contractor] = []
    location = f"{city_name}, {state}"
    seen_ids: set[str] = set()

    for query in GOOGLE_QUERIES:
        print(f"    [Google Places] {city_name} / '{query}'")
        places = google_places_search_new(query, location, api_key)
        print(f"      → {len(places)} results")

        for place in places:
            place_id = place.get("id", "")
            if place_id in seen_ids:
                continue
            seen_ids.add(place_id)

            try:
                name = place.get("displayName", {}).get("text", "")
                if not name:
                    continue

                address_full = place.get("formattedAddress", "")
                street = addr_city = addr_state = addr_zip = ""
                m = re.match(r"^(.+?),\s*(.+?),\s*([A-Z]{2})\s+(\d{5})", address_full)
                if m:
                    street, addr_city, addr_state, addr_zip = (
                        m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4)
                    )
                else:
                    street = address_full

                phone = place.get("nationalPhoneNumber", "")
                website = place.get("websiteUri", "")
                category = place.get("primaryTypeDisplayName", {}).get("text", "")

                results.append(Contractor(
                    name=name,
                    phone=phone,
                    address=street,
                    city=addr_city or city_name,
                    state=addr_state or state,
                    zip_code=addr_zip,
                    website=website,
                    category=category,
                    google_rating=str(place.get("rating", "")),
                    review_count=str(place.get("user_ratings_total", "")),
                    source="Google Places",
                ))
            except Exception as exc:
                print(f"    [Google Places parse error] {exc}")

        polite_sleep()

    return results


# ─────────────────────────────────────────────────────────────
# Deduplication / merge
# ─────────────────────────────────────────────────────────────

def merge_into(store: dict[str, Contractor], contractor: Contractor) -> None:
    key = contractor.dedup_key
    if key not in store:
        store[key] = contractor
        return
    e = store[key]
    if not e.phone and contractor.phone:
        e.phone = contractor.phone
    if not e.website and contractor.website:
        e.website = contractor.website
    if not e.address and contractor.address:
        e.address = contractor.address
    if not e.email and contractor.email:
        e.email = contractor.email
    if not e.license_number and contractor.license_number:
        e.license_number = contractor.license_number
    if not e.license_type and contractor.license_type:
        e.license_type = contractor.license_type
    if not e.license_expiry and contractor.license_expiry:
        e.license_expiry = contractor.license_expiry
    if not e.types_of_work and contractor.types_of_work:
        e.types_of_work = contractor.types_of_work
    if not e.bbb_rating and contractor.bbb_rating:
        e.bbb_rating = contractor.bbb_rating
    if not e.google_rating and contractor.google_rating:
        e.google_rating = contractor.google_rating
    if not e.bbb_profile_url and contractor.bbb_profile_url:
        e.bbb_profile_url = contractor.bbb_profile_url
    sources = set(e.source.split(", "))
    sources.add(contractor.source)
    e.source = ", ".join(sorted(sources))


# ─────────────────────────────────────────────────────────────
# Contractor filtering
# ─────────────────────────────────────────────────────────────

# Terms that disqualify a contractor if found alone (without redeeming HVAC terms)
_EXCLUDE_TERMS = ["plumb", "apprentice", "pipefitter", "sprinkler fitter"]
_HVAC_TERMS    = ["hvac", "air condition", "heating", "cooling", "ac ", "heat pump",
                  "furnace", "refrigerat", "ventilat", "mechanical", "duct"]


def should_exclude(c: Contractor) -> bool:
    """
    Return True if the contractor should be filtered out.
    Excludes:
      - Plumbing-only (category/name/types contain plumbing terms but NO hvac terms)
      - Apprentice-only (name or category contains 'apprentice')
    """
    text = " ".join([c.name, c.category, c.types_of_work, c.license_type]).lower()
    has_exclude = any(t in text for t in _EXCLUDE_TERMS)
    has_hvac    = any(t in text for t in _HVAC_TERMS)
    return has_exclude and not has_hvac


# ─────────────────────────────────────────────────────────────
# Write CSV
# ─────────────────────────────────────────────────────────────

COL_NAMES = [f.name for f in fields(Contractor)]


def _get_existing_dedup_keys(city: str, state: str) -> set[str]:
    """
    Query the local PostgreSQL DB for dedup_keys already stored for this city/state.
    Returns empty set if DB is unavailable.
    """
    try:
        from contractor_db import ContractorDB, DEFAULT_DSN
        import psycopg2
        con = psycopg2.connect(DEFAULT_DSN)
        with con.cursor() as cur:
            cur.execute(
                "SELECT dedup_key FROM contractors "
                "WHERE LOWER(city)=LOWER(%s) AND UPPER(state)=UPPER(%s)",
                (city, state)
            )
            keys = {row[0] for row in cur.fetchall()}
        con.close()
        return keys
    except Exception:
        return set()


def write_csv(contractors: list[Contractor], path: str, check_db: bool = True) -> None:
    before = len(contractors)
    contractors = [c for c in contractors if not should_exclude(c)]
    filtered = before - len(contractors)
    contractors.sort(key=lambda c: c.name.lower())

    # DB dedup check — flag records already in DB vs net-new
    existing_keys: set[str] = set()
    if check_db and contractors:
        # Use the city/state of the first record as representative
        sample = contractors[0]
        existing_keys = _get_existing_dedup_keys(sample.city, sample.state)

    col_names = COL_NAMES + (["is_new_to_db"] if existing_keys else [])
    new_count = 0

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=col_names, extrasaction="ignore")
        writer.writeheader()
        for c in contractors:
            row = {f.name: getattr(c, f.name) for f in fields(c)}
            if existing_keys:
                is_new = c.dedup_key not in existing_keys
                row["is_new_to_db"] = "yes" if is_new else "no"
                if is_new:
                    new_count += 1
            writer.writerow(row)

    if existing_keys:
        print(f"  Saved {len(contractors)} rows → {path}  "
              f"({new_count} new to DB, {len(contractors)-new_count} already exist, "
              f"filtered {filtered} plumbing/apprentice-only)")
    else:
        print(f"  Saved {len(contractors)} rows → {path}  (filtered {filtered} plumbing/apprentice-only)")


# ─────────────────────────────────────────────────────────────
# UA Local 367 Anchorage — union contractor directory
# https://ualocal367.org/contractors/
# ─────────────────────────────────────────────────────────────

UALOCAL367_URL = "https://ualocal367.org/contractors/"


def scrape_ualocal367() -> list[Contractor]:
    """
    Scrape the UA Local 367 (Anchorage plumbers & pipefitters union) contractor directory.
    The page lists signatory contractors with name, phone, address and trade category.
    """
    print("    [UA Local 367] Fetching contractor directory …")
    results: list[Contractor] = []

    soup = get_soup(UALOCAL367_URL)
    if soup is None:
        print("    [UA Local 367] Could not fetch page")
        return results

    # The page renders contractor entries — try common patterns: tables, divs, list items
    # Pattern 1: <table> rows
    for row in soup.select("table tr"):
        cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 2 or not cells[0]:
            continue
        name = cells[0].strip()
        if name.lower() in ("company", "contractor", "name", ""):
            continue  # header row
        phone = next((c for c in cells[1:] if re.search(r"\d{3}[\s.-]\d{3}[\s.-]\d{4}", c)), "")
        address = next((c for c in cells[1:] if c != phone and len(c) > 5), "")
        results.append(Contractor(
            name=name,
            phone=re.sub(r"[^\d\-\(\)\s]", "", phone).strip(),
            address=address,
            city="Anchorage",
            state="AK",
            category="Plumbing / Pipefitting",
            source="UA Local 367",
        ))

    if results:
        print(f"    [UA Local 367] Parsed {len(results)} contractors (table)")
        return results

    # Pattern 2: div/article cards (common for WordPress contractor directories)
    for card in soup.select("div.contractor, div.entry, article, div.wpbdp-listing, li.contractor"):
        name_el = card.select_one("h2, h3, h4, .contractor-name, .listing-title, strong")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue

        text = card.get_text(" ", strip=True)
        phone_m = re.search(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})", text)
        phone = phone_m.group(1) if phone_m else ""

        addr_el = card.select_one(".address, .location, address")
        address = addr_el.get_text(" ", strip=True) if addr_el else ""

        website_el = card.select_one("a[href^='http']")
        website = website_el["href"] if website_el else ""

        results.append(Contractor(
            name=name,
            phone=phone,
            address=address,
            city="Anchorage",
            state="AK",
            website=website,
            category="Plumbing / Pipefitting / HVAC",
            source="UA Local 367",
        ))

    # Pattern 3: plain paragraphs / text blocks (fallback)
    if not results:
        for p in soup.select("p, li"):
            text = p.get_text(" ", strip=True)
            if not text or len(text) < 5:
                continue
            phone_m = re.search(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})", text)
            if not phone_m:
                continue
            # Treat everything before the phone as the name
            name = text[:phone_m.start()].strip().rstrip(",;:-")
            if not name or len(name) > 80:
                continue
            results.append(Contractor(
                name=name,
                phone=phone_m.group(1),
                city="Anchorage",
                state="AK",
                category="Plumbing / Pipefitting / HVAC",
                source="UA Local 367",
            ))

    print(f"    [UA Local 367] Parsed {len(results)} contractors")
    return results


# ─────────────────────────────────────────────────────────────
# Yelp scraper (HTML — no API key needed)
# ─────────────────────────────────────────────────────────────

YELP_CATEGORIES = [
    "hvacair_conditioning_heating",
    "contractors",
    "electricians",
    "plumbing",
    "roofing",
    "generalcontractors",
]
MAX_YELP_PAGES = 5  # 10 results per page → up to 50 per category


def scrape_yelp(city_name: str, state: str) -> list[Contractor]:
    """
    Scrape Yelp search results for contractor categories in a given city.
    Uses public HTML search (no API key). Extracts name, phone, address, rating.
    """
    print(f"    [Yelp] Scraping contractors in {city_name}, {state} …")
    location = f"{city_name}, {state}"
    results: list[Contractor] = []
    seen: set[str] = set()

    yelp_session = requests.Session()
    yelp_session.headers.update({
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    for cat in YELP_CATEGORIES:
        for page_num in range(MAX_YELP_PAGES):
            start = page_num * 10
            url = (
                f"https://www.yelp.com/search"
                f"?find_desc={requests.utils.quote(cat.replace('_', '+'))}"
                f"&find_loc={requests.utils.quote(location)}"
                f"&start={start}"
            )
            print(f"      [Yelp] {cat} / page {page_num + 1}")
            try:
                resp = yelp_session.get(url, timeout=20)
                if resp.status_code == 403 or resp.status_code == 429:
                    print(f"      [Yelp] rate-limited ({resp.status_code}), backing off 30s …")
                    time.sleep(30)
                    break
                if resp.status_code != 200:
                    print(f"      [Yelp] HTTP {resp.status_code}, skipping")
                    break
            except requests.RequestException as exc:
                print(f"      [Yelp] network error: {exc}")
                break

            soup = BeautifulSoup(resp.text, "lxml")

            # Yelp renders business cards as <div> with data-testid or aria-label
            cards = soup.select('li[data-testid], div[data-testid="serp-ia-card"]')
            if not cards:
                # fallback selector for older Yelp HTML
                cards = soup.select("li.lemon--li__373c0__1r9wz, ul.lemon--ul__373c0__1_cOA > li")

            found = 0
            for card in cards:
                name_el = card.select_one("a.css-19v1rkv, span.css-1egxyab, h4 a, h3 a, a[name]")
                if not name_el:
                    name_el = card.select_one("a[href*='/biz/']")
                if not name_el:
                    continue
                name = name_el.get_text(strip=True)
                if not name:
                    continue

                dedup_key = re.sub(r"[^a-z0-9]", "", name.lower())
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                text = card.get_text(" ", strip=True)
                phone_m = re.search(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})", text)
                phone = phone_m.group(1) if phone_m else ""

                addr_el = card.select_one("address, span[class*='address'], p[class*='address']")
                address = addr_el.get_text(" ", strip=True) if addr_el else ""

                rating_m = re.search(r"(\d+\.?\d*)\s*star", text, re.IGNORECASE)
                rating = rating_m.group(1) if rating_m else ""

                profile_el = card.select_one("a[href*='/biz/']")
                profile_url = ""
                if profile_el and profile_el.get("href"):
                    href = profile_el["href"]
                    profile_url = href if href.startswith("http") else "https://www.yelp.com" + href

                results.append(Contractor(
                    name=name,
                    phone=phone,
                    address=address,
                    city=city_name,
                    state=state,
                    google_rating=rating,
                    bbb_profile_url=profile_url,
                    category=cat.replace("_", " ").title(),
                    source="Yelp",
                ))
                found += 1

            print(f"        → {found} new results (total: {len(results)})")
            if found == 0:
                break  # no more results for this category

            time.sleep(random.uniform(3.0, 5.0))  # Yelp is strict about rate limiting

    print(f"    [Yelp] Total: {len(results)} contractors")
    return results


# ─────────────────────────────────────────────────────────────
# Angi (HomeAdvisor) scraper — public directory HTML
# ─────────────────────────────────────────────────────────────

ANGI_CATEGORIES = [
    "hvac-repair",
    "general-contractors",
    "electricians",
    "roofing-contractors",
    "plumbers",
]
MAX_ANGI_PAGES = 4


def scrape_angi(city_name: str, state: str) -> list[Contractor]:
    """
    Scrape Angi (formerly HomeAdvisor) public pro directory for a city.
    No API key needed — uses public HTML listing pages.
    """
    print(f"    [Angi] Scraping contractors in {city_name}, {state} …")
    city_slug = city_name.lower().replace(" ", "-")
    state_lower = state.lower()
    results: list[Contractor] = []
    seen: set[str] = set()

    angi_session = requests.Session()
    angi_session.headers.update({
        **HEADERS,
        "Referer": "https://www.angi.com/",
    })

    for cat in ANGI_CATEGORIES:
        for page_num in range(1, MAX_ANGI_PAGES + 1):
            url = (
                f"https://www.angi.com/companylist/{state_lower}/{city_slug}/{cat}.htm"
                if page_num == 1
                else f"https://www.angi.com/companylist/{state_lower}/{city_slug}/{cat}-{page_num}.htm"
            )
            print(f"      [Angi] {cat} / page {page_num}")
            try:
                resp = angi_session.get(url, timeout=20)
                if resp.status_code == 404:
                    break
                if resp.status_code in (403, 429):
                    print(f"      [Angi] rate-limited ({resp.status_code}), backing off 20s …")
                    time.sleep(20)
                    break
                if resp.status_code != 200:
                    print(f"      [Angi] HTTP {resp.status_code}, skipping")
                    break
            except requests.RequestException as exc:
                print(f"      [Angi] network error: {exc}")
                break

            soup = BeautifulSoup(resp.text, "lxml")

            # Angi pro cards
            cards = soup.select(
                "div.provider-listing, div[class*='ProCard'], div[class*='provider'], "
                "li[class*='provider'], div.profile-card"
            )
            if not cards:
                cards = soup.select("article, div.company-card, div.pro-card")

            found = 0
            for card in cards:
                name_el = card.select_one("h2, h3, h4, .company-name, .provider-name, a.provider-link")
                if not name_el:
                    continue
                name = name_el.get_text(strip=True)
                if not name:
                    continue

                dedup_key = re.sub(r"[^a-z0-9]", "", name.lower())
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                text = card.get_text(" ", strip=True)
                phone_m = re.search(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})", text)
                phone = phone_m.group(1) if phone_m else ""

                addr_el = card.select_one("address, .address, span[class*='city']")
                address = addr_el.get_text(" ", strip=True) if addr_el else ""

                rating_m = re.search(r"(\d+\.?\d*)\s*(?:star|out of)", text, re.IGNORECASE)
                rating = rating_m.group(1) if rating_m else ""

                profile_el = card.select_one("a[href*='angi.com']") or card.select_one("a[href]")
                profile_url = ""
                if profile_el and profile_el.get("href"):
                    href = profile_el["href"]
                    profile_url = href if href.startswith("http") else "https://www.angi.com" + href

                results.append(Contractor(
                    name=name,
                    phone=phone,
                    address=address,
                    city=city_name,
                    state=state,
                    google_rating=rating,
                    bbb_profile_url=profile_url,
                    category=cat.replace("-", " ").title(),
                    source="Angi",
                ))
                found += 1

            print(f"        → {found} new results (total: {len(results)})")
            if found == 0:
                break

            time.sleep(random.uniform(2.0, 4.0))

    print(f"    [Angi] Total: {len(results)} contractors")
    return results


# ─────────────────────────────────────────────────────────────
# Alaska NECA Contractor Brochure PDF parser
# https://www.alaskaneca.org/wp-content/uploads/12.20.24-Contractor-Brochure.pdf
# ─────────────────────────────────────────────────────────────

ALASKA_NECA_PDF_URL = "https://www.alaskaneca.org/wp-content/uploads/12.20.24-Contractor-Brochure.pdf"

# Lines that are section headers or boilerplate — skip them
_NECA_SKIP_RE = re.compile(
    r"^\s*("
    r"FAIRBANKS MEMBERS|SOUTHEASTERN MEMBERS|ANCHORAGE|SOUTHCENTRAL MEMBERS"
    r"|QUALITY BEGINS|ALASKA CHAPTER|NECA Contractors|printed in house"
    r"|Numbers To Remember|AETF|AJEATT|IBEW|Statewide Toll Free"
    r"|UCW\s*-|UL\s*-|OC\s*-|IC\s*-|RW\s*-|NEC®"
    r"|December 2024|LARRY BELL|www\.alaskaneca\.org"
    r"|712 W 36th|Anchorage, Alaska 99503|T \(907\)|F \(907\)"
    r"|It is accomplished|able to provide|reputation|are a given"
    r"|properly trained|highly technical|NECA Contractors use|trained electrical"
    r"|state-of-the-art|utilized daily|safer work|under$|budget"
    r")\b",
    re.IGNORECASE,
)
_PHONE_RE  = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")
_EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_LIC_RE    = re.compile(r"\(Lic#[^)]+\)", re.IGNORECASE)
_CITY_ST_ZIP_RE = re.compile(r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


def scrape_alaska_neca_pdf(url: str = ALASKA_NECA_PDF_URL) -> list[Contractor]:
    """
    Download and parse the Alaska NECA contractor brochure PDF.
    Uses pdftotext (no -layout) for clean line-by-line text flow.
    Parses contractor blocks: name → address → city/state/zip → contact → phone → email.
    """
    import subprocess, tempfile

    print(f"    [Alaska NECA PDF] Downloading {url} …")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"    [Alaska NECA PDF] Download failed: {exc}")
        return []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(resp.content)
        pdf_path = tmp.name

    result = subprocess.run(["pdftotext", pdf_path, "-"], capture_output=True, text=True)
    Path(pdf_path).unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"    [Alaska NECA PDF] pdftotext error: {result.stderr[:200]}")
        return []

    lines = [l.strip() for l in result.stdout.splitlines()]

    results: list[Contractor] = []

    # State machine: accumulate lines into a contractor block, flush when we see
    # the next company name (non-blank line that is not phone/email/address/license)
    def _is_company_name(line: str) -> bool:
        """Heuristic: a company name line has no phone, no @, doesn't start with digits."""
        if not line or len(line) < 3:
            return False
        if _PHONE_RE.search(line) and not re.search(r"[A-Za-z]{3,}", line):
            return False
        if _EMAIL_RE.search(line):
            return False
        if _NECA_SKIP_RE.match(line):
            return False
        if re.match(r"^\d", line):  # starts with digit → address
            return False
        if re.match(r"^\(Lic", line, re.IGNORECASE):
            return False
        if _CITY_ST_ZIP_RE.match(line):
            return False
        # Looks like a company name if it has at least one capital word
        if re.search(r"[A-Z][a-z]", line):
            return True
        return False

    def _flush(block: list[str]) -> Optional[Contractor]:
        if not block:
            return None
        name = block[0].strip()
        # Strip inline license from name line
        name = _LIC_RE.sub("", name).strip().strip("()")
        if not name:
            return None

        phone = email = address = addr_city = addr_state = addr_zip = lic = ""

        for line in block[1:]:
            line = line.strip()
            if not line:
                continue
            m_email = _EMAIL_RE.search(line)
            m_phone = _PHONE_RE.search(line)
            m_csz   = _CITY_ST_ZIP_RE.match(line)
            m_lic   = _LIC_RE.search(line)

            if m_email and not email:
                email = m_email.group(0)
            if m_phone and not phone:
                phone = m_phone.group(0)
            if m_csz:
                addr_city  = m_csz.group(1).strip()
                addr_state = m_csz.group(2)
                addr_zip   = m_csz.group(3).split("-")[0]
            if m_lic and not lic:
                lic = m_lic.group(0).strip("()")
            # Address: line that starts with digit and isn't city/state/zip
            if re.match(r"^\d", line) and not m_csz and not address:
                address = _LIC_RE.sub("", line).strip()

        if not addr_city:
            return None  # can't determine location — skip

        return Contractor(
            name=name,
            phone=phone,
            email=email,
            address=address,
            city=addr_city,
            state=addr_state,
            zip_code=addr_zip,
            license_number=lic,
            license_type="Electrical",
            category="Electrical Contractor",
            source="Alaska NECA",
        )

    current_block: list[str] = []

    for line in lines:
        if not line:
            # Blank line — could be end of block
            if current_block:
                c = _flush(current_block)
                if c:
                    results.append(c)
                current_block = []
            continue

        if _NECA_SKIP_RE.match(line):
            continue

        if _is_company_name(line) and current_block:
            c = _flush(current_block)
            if c:
                results.append(c)
            current_block = [line]
        elif _is_company_name(line):
            current_block = [line]
        else:
            current_block.append(line)

    if current_block:
        c = _flush(current_block)
        if c:
            results.append(c)

    print(f"    [Alaska NECA PDF] Parsed {len(results)} contractors")
    return results


# ─────────────────────────────────────────────────────────────
# Generic Chamber of Commerce directory scraper
# Works for any city chamber using standard member-list HTML pagination
# ─────────────────────────────────────────────────────────────

MAX_CHAMBER_PAGES = 20


def scrape_chamber(city_name: str, state: str, url: str, source_name: str) -> list[Contractor]:
    """
    Scrape a chamber of commerce member directory.
    Handles paginated lists (appends ?page=N or follows next-page links).
    """
    print(f"    [{source_name}] Fetching {url} …")
    results: list[Contractor] = []
    seen: set[str] = set()

    def _parse_page(soup: BeautifulSoup) -> int:
        """Parse cards from one page, return count added."""
        added = 0
        # Common chamber directory card selectors (GrowthZone, MemberClicks, etc.)
        cards = soup.select(
            "div.gz-business-card, div.member-listing, div.directory-item, "
            "li.member-item, div.business-listing, article.member, "
            "div[class*='BusinessCard'], div[class*='member-card']"
        )
        if not cards:
            # Fallback: any <article> or list item with a heading
            cards = soup.select("article, li")

        for card in cards:
            name_el = card.select_one("h2, h3, h4, .gz-card-title, .member-name, strong, a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name or len(name) < 3:
                continue
            dedup = re.sub(r"[^a-z0-9]", "", name.lower())
            if dedup in seen:
                continue
            seen.add(dedup)

            text = card.get_text(" ", strip=True)
            phone_m = re.search(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})", text)
            phone = phone_m.group(1) if phone_m else ""

            addr_el = card.select_one("address, .gz-card-address, .address, .location")
            address = addr_el.get_text(" ", strip=True) if addr_el else ""

            web_el = card.select_one("a[href^='http']:not([href*='chamber']):not([href*='facebook'])")
            website = web_el["href"] if web_el else ""

            profile_el = card.select_one("a[href]")
            profile_url = ""
            if profile_el:
                href = profile_el["href"]
                profile_url = href if href.startswith("http") else f"https://{url.split('/')[2]}{href}"

            results.append(Contractor(
                name=name,
                phone=phone,
                address=address,
                city=city_name,
                state=state,
                website=website,
                bbb_profile_url=profile_url,
                category="General Contractor",
                source=source_name,
            ))
            added += 1
        return added

    # Fetch first page
    soup = get_soup(url)
    if soup is None:
        print(f"    [{source_name}] Could not fetch page")
        return results

    _parse_page(soup)

    # Follow pagination
    for page_num in range(2, MAX_CHAMBER_PAGES + 1):
        next_el = soup.select_one("a[rel='next'], a.next, li.next a, a[aria-label='Next']")
        if next_el and next_el.get("href"):
            next_url = next_el["href"]
            if not next_url.startswith("http"):
                base = "/".join(url.split("/")[:3])
                next_url = base + next_url
        else:
            # Try appending ?page=N
            sep = "&" if "?" in url else "?"
            next_url = f"{url}{sep}page={page_num}"

        print(f"      [{source_name}] page {page_num}: {next_url}")
        soup = get_soup(next_url)
        if soup is None:
            break

        added = _parse_page(soup)
        print(f"        → {added} new (total: {len(results)})")
        if added == 0:
            break
        time.sleep(random.uniform(1.5, 3.0))

    print(f"    [{source_name}] Total: {len(results)} contractors")
    return results


# ─────────────────────────────────────────────────────────────
# city_sources.yaml — config-driven hook loader
# Reads city_sources.yaml and registers hooks for each source type.
# Supported types: union_directory, chamber, yelp, angi, bbb (default)
# ─────────────────────────────────────────────────────────────

def _load_city_sources_hooks() -> None:
    """Read city_sources.yaml and register scraper hooks for supported source types."""
    if not _YAML_AVAILABLE:
        print("  [city_sources] PyYAML not installed — skipping config-driven hooks (pip install pyyaml)")
        return
    if not CITY_SOURCES_PATH.exists():
        return

    try:
        config = yaml.safe_load(CITY_SOURCES_PATH.read_text())
    except Exception as exc:
        print(f"  [city_sources] Failed to load {CITY_SOURCES_PATH}: {exc}")
        return

    for entry in config.get("cities", []):
        city = entry.get("city", "").strip()
        state = entry.get("state", "").strip().upper()
        if not city or not state:
            continue

        for src in entry.get("sources", []):
            src_type = src.get("type", "bbb")
            src_url = src.get("url", "")
            src_name = src.get("name", f"{city} {src_type.title()}")

            if src_type == "union_directory":
                # Route to specific union scrapers by URL
                if "ualocal367" in src_url:
                    register_city_hook(city, state, scrape_ualocal367)
                else:
                    # Generic: treat like a chamber directory
                    _url, _name = src_url, src_name
                    register_city_hook(city, state,
                        lambda c=city, s=state, u=_url, n=_name: scrape_chamber(c, s, u, n))

            elif src_type == "chamber":
                _url, _name = src_url, src_name
                register_city_hook(city, state,
                    lambda c=city, s=state, u=_url, n=_name: scrape_chamber(c, s, u, n))

            elif src_type == "yelp":
                _c, _s = city, state
                register_city_hook(city, state,
                    lambda c=_c, s=_s: scrape_yelp(c, s))

            elif src_type == "angi":
                _c, _s = city, state
                register_city_hook(city, state,
                    lambda c=_c, s=_s: scrape_angi(c, s))

            elif src_type == "pdf":
                _url = src_url
                if "alaskaneca.org" in _url:
                    register_city_hook(city, state,
                        lambda u=_url: scrape_alaska_neca_pdf(u))
                elif _url:
                    # Generic: reuse Thornton-style PDF scraper (pdftotext + fixed-width parse)
                    # For now log it — city-specific parsers must be added manually
                    print(f"  [city_sources] PDF source for {city},{state} ({_url}) — "
                          f"no generic PDF parser; add a custom scraper function")

            # bbb / aspnet / arcgis / aspnet_sa / csv_url are handled
            # elsewhere (main loop or existing hooks) — no additional hook needed


# ─────────────────────────────────────────────────────────────
# Register all city-specific portal hooks
# ─────────────────────────────────────────────────────────────

# Hardcoded hooks for cities with custom portal scrapers
register_city_hook("San Antonio", "TX", scrape_sa_bils)
register_city_hook("Philadelphia", "PA", scrape_philly_arcgis)
register_city_hook("Baltimore", "MD", scrape_baltimore_dpw)
for _co_city in ("Brighton", "Thornton", "Commerce City", "Westminster", "Northglenn"):
    register_city_hook(_co_city, "CO", scrape_thornton_pdf)

# Config-driven hooks — reads city_sources.yaml and registers yelp/angi/chamber/union sources
_load_city_sources_hooks()


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def build_city_list(extra: list[str]) -> list[dict]:
    """
    Merge the default CITIES with any extra cities passed via --city.
    Format: "City Name,ST"  e.g. "Brighton,CO" or "Philadelphia,PA"
    """
    cities = list(CITIES)
    for entry in extra:
        parts = entry.split(",")
        if len(parts) != 2:
            print(f"WARN: Skipping invalid --city '{entry}' — expected 'City,ST'")
            continue
        city_name = parts[0].strip()
        state = parts[1].strip().upper()
        slug = city_name.replace(" ", "") + "_" + state
        cities.append({
            "name": city_name,
            "state": state,
            "output": f"contractors_{slug}.csv",
        })
    return cities


def main():
    parser = argparse.ArgumentParser(
        description="Scrape HVAC/General contractors by city, with auto nearby-city discovery.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single city with auto nearby discovery (default: 50 miles)
  python3 scrape_contractors.py --city "Seaford,DE" --no-defaults

  # Custom radius
  python3 scrape_contractors.py --city "Baltimore,MD" --no-defaults --nearby-miles 30

  # Skip nearby discovery (target city only)
  python3 scrape_contractors.py --city "Mansfield,TX" --no-defaults --no-nearby

  # Multiple cities
  python3 scrape_contractors.py --city "Borger,TX" --city "Sapulpa,OK" --no-defaults
        """,
    )
    parser.add_argument("--api-key", metavar="KEY",
                        default=os.environ.get("GOOGLE_MAPS_API_KEY", ""),
                        help="Google Maps Places API key (default: $GOOGLE_MAPS_API_KEY)")
    parser.add_argument("--skip-bbb", action="store_true",
                        help="Skip BBB scraping")
    parser.add_argument("--city", metavar="CITY,ST", action="append", default=[],
                        help="Add a city to scrape, e.g. --city 'Brighton,CO' (repeatable)")
    parser.add_argument("--no-defaults", action="store_true",
                        help="Skip the default TX cities; only scrape cities from --city")
    parser.add_argument("--no-nearby", action="store_true",
                        help="Disable nearby city discovery; scrape target city only")
    parser.add_argument("--nearby-miles", type=float, default=DEFAULT_NEARBY_MILES,
                        help=f"Radius in miles for nearby city discovery (default: {DEFAULT_NEARBY_MILES})")
    parser.add_argument("--max-nearby", type=int, default=DEFAULT_MAX_NEARBY,
                        help=f"Max number of nearby cities to scrape (default: {DEFAULT_MAX_NEARBY})")
    parser.add_argument("--min-population", type=int, default=DEFAULT_MIN_POPULATION,
                        help=f"Minimum population for nearby cities (default: {DEFAULT_MIN_POPULATION:,})")
    parser.add_argument("--no-db", action="store_true",
                        help="Skip PostgreSQL DB save (CSV output only)")
    args = parser.parse_args()

    if args.no_defaults:
        cities = []
        for entry in args.city:
            parts = entry.split(",")
            if len(parts) != 2:
                print(f"WARN: Skipping invalid --city '{entry}' — expected 'City,ST'")
                continue
            city_name = parts[0].strip()
            state = parts[1].strip().upper()
            slug = city_name.replace(" ", "") + "_" + state
            cities.append({"name": city_name, "state": state,
                           "output": f"contractors_{slug}.csv"})
    else:
        cities = build_city_list(args.city)

    if not cities:
        print("No cities to process. Use --city 'Name,ST' or remove --no-defaults.")
        return

    # Connect to Postgres DB
    db = None
    if not args.no_db:
        try:
            from contractor_db import ContractorDB
            db = ContractorDB()
            print("  [DB] Connected to realestate_db (PostgreSQL)")
        except Exception as exc:
            print(f"  [DB] Warning: could not connect to DB — {exc}. Continuing without DB.")

    # Pre-fetch SA BILS data once (it covers all SA contractors)
    sa_bils_cache: list[Contractor] = []
    completed: list[dict] = []

    for city in cities:
        city_name = city["name"]
        state = city["state"]
        output_file = city.get("output", f"contractors_{city_name.replace(' ','')}_{state}.csv")
        store: dict[str, Contractor] = {}

        print(f"\n{'='*60}")
        print(f"  {city_name}, {state}")
        print(f"{'='*60}")

        # Check DB freshness — skip scraping if already done recently
        if db and not args.no_db and db.city_scraped("contractors", city_name, state):
            print(f"  [DB] {city_name}, {state} already scraped within 30 days — skipping.")
            completed.append(city)
            continue

        # Build the full list of cities to scrape: target + nearby
        cities_to_scrape: list[tuple[str, str]] = [(city_name, state)]
        if not args.no_nearby:
            nearby = get_nearby_cities(city_name, state, args.nearby_miles,
                                       min_population=args.min_population,
                                       max_nearby=args.max_nearby)
            cities_to_scrape.extend(nearby)

        total_cities = len(cities_to_scrape)
        total_steps  = total_cities * len(BBB_TERMS)  # one step per (city, term) pair
        print(f"\n  {total_cities} cities to scrape ({city_name} + {total_cities-1} nearby) "
              f"× {len(BBB_TERMS)} search terms = {total_steps} BBB queries\n")

        with tqdm(total=total_steps, unit="query", ncols=90, file=sys.stderr, disable=False,
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}") as pbar:

            for scrape_city, scrape_state in cities_to_scrape:
                is_target = (scrape_city == city_name and scrape_state == state)
                pbar.set_description(f"{'★' if is_target else ' '} {scrape_city}, {scrape_state}")
                scrape_latlon = _get_latlon(scrape_city, scrape_state)

                # ── BBB ──────────────────────────────────────────────────
                if not args.skip_bbb:
                    for term in BBB_TERMS:
                        for c in scrape_bbb(scrape_city, scrape_state, term, pbar=pbar,
                                            target_latlon=scrape_latlon,
                                            max_miles=args.nearby_miles):
                            merge_into(store, c)
                        pbar.update(1)
                        pbar.set_postfix(collected=len(store), refresh=True)
                        polite_sleep()
                else:
                    pbar.update(len(BBB_TERMS))

                # ── City-specific portal hooks ─────────────────────────────
                hook_results = run_city_hooks(scrape_city, scrape_state)
                if hook_results:
                    tqdm.write(f"  [City Portal] {scrape_city}, {scrape_state} → {len(hook_results)} records")
                    for c in hook_results:
                        merge_into(store, c)

                # ── Google Places API ─────────────────────────────────────
                if args.api_key:
                    tqdm.write(f"  [Google Places] {scrape_city}, {scrape_state}")
                    for c in scrape_google_places(scrape_city, scrape_state, args.api_key):
                        merge_into(store, c)

        # ── Filter plumbing-only / apprentice-only ────────────────
        contractors = [c for c in store.values() if not should_exclude(c)]

        # ── Write merged CSV for target city ─────────────────────
        out_path = Path(__file__).parent / "datasets" / output_file
        write_csv(contractors, str(out_path))

        # ── Save to PostgreSQL DB ─────────────────────────────────
        if db:
            n = db.upsert_contractors(contractors, city_name, state)
            print(f"  [DB] Upserted {n} contractors for {city_name}, {state}")

        completed.append(city)

        from collections import Counter
        src_counts = Counter()
        for c in contractors:
            for s in c.source.split(", "):
                src_counts[s.strip()] += 1
        for src, count in sorted(src_counts.items()):
            print(f"    {src}: {count}")

    print(f"\n{'='*60}")
    print("  All done!")
    print(f"{'='*60}")
    for city in completed:
        print(f"  {city['name'] + ', ' + city['state']:20} → {city['output']}")


if __name__ == "__main__":
    main()
