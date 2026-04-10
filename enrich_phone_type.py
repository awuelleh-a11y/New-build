#!/usr/bin/env python3
"""
Phone Type Enrichment — Twilio Lookup API
Reads contractors_*.csv, adds a `phone_type` column (mobile/landline/voip/unknown),
then writes filtered *_mobile_only.csv files containing only mobile numbers.

Features:
  - Deduplicates phone numbers — each unique number looked up only once
  - Caches results in phone_type_cache.json — safe to re-run / resume after interruption
  - Skips rows with no phone number
  - Shows cost estimate before proceeding

Usage:
  python3 enrich_phone_type.py                   # enrich all 3 city CSVs
  python3 enrich_phone_type.py --dry-run          # show cost estimate only, no API calls
  python3 enrich_phone_type.py --input custom.csv # enrich a specific file
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ── Load .env ────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ───────────────────────────────────────────────────

INPUT_FILES = [
    "contractors_Odessa_TX.csv",
    "contractors_SanAntonio_TX.csv",
    "contractors_Edinburg_TX.csv",
]

CACHE_FILE = Path("phone_type_cache.json")
TWILIO_LOOKUP_URL = "https://lookups.twilio.com/v1/PhoneNumbers/{phone}?Type=carrier"
COST_PER_LOOKUP = 0.005  # USD per Twilio Lookup v1 carrier lookup


# ── Helpers ──────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    """Strip formatting, return E.164 +1XXXXXXXXXX or '' if invalid."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return ""


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def twilio_lookup(e164: str, sid: str, token: str) -> str:
    """
    Returns one of: 'mobile', 'landline', 'voip', 'unknown'
    Twilio carrier lookup returns line_type_intelligence or carrier.type.
    """
    url = f"https://lookups.twilio.com/v2/PhoneNumbers/{e164}?Fields=line_type_intelligence"
    try:
        resp = requests.get(url, auth=HTTPBasicAuth(sid, token), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            lti = data.get("line_type_intelligence") or {}
            line_type = lti.get("type", "unknown").lower()
            # Twilio v2 types: mobile, landline, voip, nonFixedVoip, tollFree, etc.
            if "mobile" in line_type:
                return "mobile"
            if "landline" in line_type or "fixed" in line_type:
                return "landline"
            if "voip" in line_type.lower():
                return "voip"
            return line_type or "unknown"
        if resp.status_code == 404:
            return "unknown"
        print(f"  [Twilio] HTTP {resp.status_code} for {e164}: {resp.text[:120]}")
        return "unknown"
    except Exception as exc:
        print(f"  [Twilio error] {e164}: {exc}")
        return "unknown"


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show cost estimate only, make no API calls")
    parser.add_argument("--input", nargs="+", metavar="FILE",
                        help="Specific CSV file(s) to process (default: all 3 city files)")
    args = parser.parse_args()

    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        print("ERROR: TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in scripts/.env")
        sys.exit(1)

    input_files = args.input or INPUT_FILES

    # ── Collect all unique phone numbers across all files ─────
    file_rows: dict[str, list[dict]] = {}       # filename → rows
    file_fieldnames: dict[str, list[str]] = {}  # filename → csv columns

    all_phones: set[str] = set()  # E.164 format

    for fname in input_files:
        path = Path(fname)
        if not path.exists():
            print(f"WARN: {fname} not found, skipping")
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            file_rows[fname] = rows
            file_fieldnames[fname] = reader.fieldnames or []
        for row in rows:
            e164 = normalize_phone(row.get("phone", ""))
            if e164:
                all_phones.add(e164)

    # ── Load cache, find uncached phones ─────────────────────
    cache = load_cache()
    uncached = [p for p in sorted(all_phones) if p not in cache]

    total_unique = len(all_phones)
    cached_count = total_unique - len(uncached)
    estimated_cost = len(uncached) * COST_PER_LOOKUP

    print(f"\nPhone number summary:")
    print(f"  Total unique phones : {total_unique}")
    print(f"  Already cached      : {cached_count}")
    print(f"  Need Twilio lookup  : {len(uncached)}")
    print(f"  Estimated cost      : ${estimated_cost:.2f} USD")

    if args.dry_run:
        print("\n[dry-run] No API calls made.")
        return

    if uncached:
        print(f"\nLooking up {len(uncached)} phone numbers via Twilio…")
        for i, e164 in enumerate(uncached, 1):
            phone_type = twilio_lookup(e164, sid, token)
            cache[e164] = phone_type
            if i % 50 == 0 or i == len(uncached):
                print(f"  {i}/{len(uncached)} — last: {e164} → {phone_type}")
                save_cache(cache)  # checkpoint every 50
            time.sleep(0.05)  # ~20 req/s, well within Twilio limits

        save_cache(cache)
        print("Cache saved to phone_type_cache.json")

    # ── Enrich CSVs and write filtered mobile-only versions ───
    print()
    for fname, rows in file_rows.items():
        fieldnames = file_fieldnames[fname]
        if "phone_type" not in fieldnames:
            fieldnames = fieldnames + ["phone_type"]

        enriched_path = Path(fname)           # overwrite with phone_type added
        mobile_path = Path(fname.replace(".csv", "_mobile_only.csv"))

        mobile_rows = []
        type_counts: dict[str, int] = {}

        for row in rows:
            e164 = normalize_phone(row.get("phone", ""))
            phone_type = cache.get(e164, "unknown") if e164 else "no_phone"
            row["phone_type"] = phone_type
            type_counts[phone_type] = type_counts.get(phone_type, 0) + 1
            if phone_type == "mobile":
                mobile_rows.append(row)

        # Write enriched (all rows + phone_type column)
        with open(enriched_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Write mobile-only filtered file
        with open(mobile_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(mobile_rows)

        print(f"{fname}:")
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"  {t:12}: {c}")
        print(f"  → {mobile_path.name}: {len(mobile_rows)} mobile numbers")
        print()

    print("Done!")


if __name__ == "__main__":
    main()
