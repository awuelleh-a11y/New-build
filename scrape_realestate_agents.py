"""
Scrape real estate agents from BBB for a given city, then merge into
the existing contractors CSV for that city.

Usage:
    python3 scrape_realestate_agents.py --city "Edinburg" --state TX
"""

import argparse, csv, math, os, re, time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────
BBB_TERMS = [
    "real estate agents",
    "real estate brokers",
    "realtors",
    "real estate",
]
MAX_BBB_PAGES = 12
DEFAULT_MAX_MILES = 50

CITIES_DB_PATH = Path(__file__).parent / ".cache" / "uscities.csv"

FIELDNAMES = ['name','phone','address','city','state','zip_code','email','website',
              'category','license_number','license_type','license_expiry',
              'types_of_work','bbb_rating','google_rating','review_count','bbb_profile_url','source']

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DATASETS_DIR = Path(__file__).parent / "datasets"


# ── City lookup + distance ────────────────────────────────────
def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


_city_latlon_cache: dict[tuple[str, str], Optional[tuple[float, float]]] = {}

def get_city_latlon(city: str, state: str) -> Optional[tuple[float, float]]:
    key = (city.lower(), state.upper())
    if key in _city_latlon_cache:
        return _city_latlon_cache[key]
    result = None
    if CITIES_DB_PATH.exists():
        with open(CITIES_DB_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("city", "").lower() == key[0] and row.get("state_id", "").upper() == key[1]:
                    try:
                        result = (float(row["lat"]), float(row["lng"]))
                    except (KeyError, ValueError):
                        pass
                    break
    _city_latlon_cache[key] = result
    return result


# ── BBB helpers ───────────────────────────────────────────────
def get_soup(url: str) -> Optional[BeautifulSoup]:
    time.sleep(1.2 + 0.5 * (hash(url) % 3))
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200:
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"    [fetch error] {e}")
        return None


def parse_bbb_card(card, city: str, state: str) -> Optional[dict]:
    try:
        name_el = card.select_one("h3.result-business-name a span[translate='no']")
        if not name_el:
            name_el = card.select_one("h3.result-business-name a")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            return None

        profile_url = ""
        link_el = card.select_one("h3.result-business-name a")
        if link_el and link_el.get("href"):
            profile_url = "https://www.bbb.org" + link_el["href"]

        bbb_rating = ""
        rating_el = card.select_one("span.result-rating") or card.select_one("summary.result-rating")
        if rating_el:
            m = re.search(r"BBB Rating:\s*([A-F][+-]?|NR)", rating_el.get_text())
            if m:
                bbb_rating = m.group(1)

        phone, address, zip_code = "", "", ""
        addr_city, addr_state = city, state
        info_div = card.select_one("div.result-business-info")
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
                    zip_code = m.group(4).split("-")[0]
                else:
                    address = raw.split(",")[0].strip()

        return {
            "name": name,
            "phone": phone,
            "address": address,
            "city": addr_city,
            "state": addr_state,
            "zip_code": zip_code,
            "email": "",
            "website": "",
            "category": "Real Estate Agent",
            "license_number": "",
            "license_type": "Real Estate Agent",
            "license_expiry": "",
            "types_of_work": "",
            "bbb_rating": bbb_rating,
            "google_rating": "",
            "review_count": "",
            "bbb_profile_url": profile_url,
            "source": "BBB",
        }
    except Exception as e:
        print(f"    [parse error] {e}")
        return None


def scrape_bbb(city: str, state: str, term: str,
               target_latlon: Optional[tuple[float, float]] = None,
               max_miles: float = DEFAULT_MAX_MILES) -> list[dict]:
    results = []
    skipped = 0
    total_pages = None
    for page in range(1, MAX_BBB_PAGES + 1):
        url = (
            f"https://www.bbb.org/search"
            f"?find_text={requests.utils.quote(term)}"
            f"&find_loc={requests.utils.quote(city + ', ' + state)}"
            f"&page={page}"
        )
        print(f"    [BBB] {city} / '{term}' / page {page}")
        soup = get_soup(url)
        if soup is None:
            break

        cards = soup.select("div.card.result-card")
        if not cards:
            break

        for card in cards:
            r = parse_bbb_card(card, city, state)
            if not r:
                continue
            # Distance filter: drop results whose city is known and too far away
            if target_latlon:
                biz_latlon = get_city_latlon(r["city"], r["state"])
                if biz_latlon:
                    dist = _haversine_miles(*target_latlon, *biz_latlon)
                    if dist > max_miles:
                        skipped += 1
                        continue
            results.append(r)

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

    if skipped:
        print(f"    [distance filter] dropped {skipped} out-of-area results (>{max_miles} mi)")
    return results


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--max-miles", type=float, default=DEFAULT_MAX_MILES,
                        help=f"Drop BBB results whose city is farther than this from the target (default {DEFAULT_MAX_MILES} mi)")
    args = parser.parse_args()

    city, state = args.city, args.state.upper()
    city_slug = city.replace(" ", "")
    output_path = DATASETS_DIR / f"realestate_agents_{city_slug}_{state}.csv"
    master_path = DATASETS_DIR / f"contractors_{city_slug}_{state}.csv"

    target_latlon = get_city_latlon(city, state)
    if target_latlon:
        print(f"  [distance filter] {city}, {state} → lat={target_latlon[0]:.4f}, lng={target_latlon[1]:.4f}, max={args.max_miles} mi")
    else:
        print(f"  [distance filter] {city}, {state} not found in cities DB — filter disabled")

    print(f"\n{'='*60}")
    print(f"  Real estate agents: {city}, {state}")
    print(f"{'='*60}\n")

    # Scrape
    all_rows = []
    seen_names = set()
    for term in BBB_TERMS:
        rows = scrape_bbb(city, state, term, target_latlon=target_latlon, max_miles=args.max_miles)
        for r in rows:
            key = r["name"].strip().lower()
            if key not in seen_names:
                seen_names.add(key)
                all_rows.append(r)

    print(f"\nUnique real estate agents found: {len(all_rows)}")

    # Save standalone file
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Saved → {output_path}")

    print(f"Agents saved to standalone file only (not merged into contractors CSV)")


if __name__ == "__main__":
    main()
