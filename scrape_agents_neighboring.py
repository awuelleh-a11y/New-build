#!/usr/bin/env python3
"""
Run scrape_realestate_agents for all nearby cities of a target city,
then merge results into a single neighboring-cities CSV.

Usage:
    python3 scrape_agents_neighboring.py --city "Des Moines" --state IA --nearby-miles 50
"""

import argparse
import csv
import time
from pathlib import Path

from scrape_contractors import get_nearby_cities
from scrape_realestate_agents import scrape_bbb, BBB_TERMS

DATASETS_DIR = Path(__file__).parent / "datasets"

FIELDNAMES = [
    'name','phone','address','city','state','zip_code','email','website',
    'category','license_number','license_type','license_expiry',
    'types_of_work','bbb_rating','google_rating','review_count',
    'bbb_profile_url','source'
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--nearby-miles", type=float, default=50.0)
    parser.add_argument("--min-population", type=int, default=5000)
    args = parser.parse_args()

    city = args.city.strip()
    state = args.state.strip().upper()
    city_slug = city.replace(" ", "")

    nearby = get_nearby_cities(city, state, args.nearby_miles,
                               min_population=args.min_population)
    print(f"Found {len(nearby)} nearby cities within {args.nearby_miles} mi of {city}, {state}")

    all_rows: list[dict] = []
    seen: set[str] = set()

    for near_city, near_state in nearby:
        print(f"\n  Scraping agents: {near_city}, {near_state}")
        try:
            new = 0
            for term in BBB_TERMS:
                rows = scrape_bbb(near_city, near_state, term)
                for row in rows:
                    key = f"{row.get('name','').lower()}|{row.get('phone','')}"
                    if key not in seen:
                        seen.add(key)
                        all_rows.append(row)
                        new += 1
                time.sleep(1.5)
            print(f"    → {new} new agents (total so far: {len(all_rows)})")
        except Exception as e:
            print(f"    ERROR: {e}")

    out_path = DATASETS_DIR / f"agents_{city_slug}_Neighboring_{state}.csv"
    DATASETS_DIR.mkdir(exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone. {len(all_rows)} agents saved → {out_path}")


if __name__ == "__main__":
    main()
