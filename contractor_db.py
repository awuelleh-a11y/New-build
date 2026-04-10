#!/usr/bin/env python3
"""
contractor_db.py — PostgreSQL persistence layer for contractors and real estate agents.

Tables:
  contractors   — HVAC/general contractors (BBB, city portals, etc.)
  agents        — Real estate listing agents (RentCast, BBB)
  scrape_log    — Tracks when each city was last scraped

Key behaviors:
  - Upsert by dedup_key (name+phone or name+address) — enriches existing records
  - city_scraped() — skip re-scraping if data is fresh (default: 30 days)
  - export_csv()   — dump any city slice to CSV on demand
  - stats()        — counts by city/state

Usage:
  from contractor_db import ContractorDB

  db = ContractorDB()
  db.upsert_contractors(contractors, city="Mansfield", state="TX")
  db.upsert_agents(agents, city="Baltimore", state="MD")

  if db.city_scraped("contractors", "Mansfield", "TX"):
      print("Already fresh — skipping scrape")

  db.export_csv("contractors", city="Mansfield", state="TX", path="out.csv")
  print(db.stats())
"""

import csv
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras

# ─────────────────────────────────────────────────────────────
# Connection config — reads from env or .env file
# ─────────────────────────────────────────────────────────────

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DEFAULT_DSN = os.environ.get(
    "REALESTATE_DATABASE_URL",
    "postgresql://gunaranjanramireddy@localhost:5432/realestate_db"
)

CONTRACTOR_COLS = [
    "name", "phone", "address", "city", "state", "zip_code",
    "email", "website", "category", "license_number", "license_type",
    "license_expiry", "types_of_work", "bbb_rating", "google_rating",
    "review_count", "bbb_profile_url", "source",
]

# ─────────────────────────────────────────────────────────────
# Contractor type classification
# ─────────────────────────────────────────────────────────────

_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("HVAC", [
        "hvac", "air condition", "heating", "cooling", "ac repair",
        "heat pump", "furnace", "refrigeration", "ventilation",
        "mechanical", "plumbing heating", "heating and air",
    ]),
    ("Plumbing", [
        "plumb", "drain", "sewer", "pipe", "water heater",
    ]),
    ("Electrical", [
        "electric", "wiring", "electrician", "solar", "generator",
    ]),
    ("Roofing", [
        "roof", "gutter", "shingle",
    ]),
    ("General", [
        "general contractor", "construction", "remodel", "renovation",
        "handyman", "home improvement", "building",
    ]),
]


def classify_contractor(category: str, types_of_work: str, name: str) -> str:
    """
    Classify a contractor into one of: HVAC, Plumbing, Electrical, Roofing, General, Other.
    Checks category + types_of_work + name fields (case-insensitive).
    """
    text = " ".join([category, types_of_work, name]).lower()
    for ctype, keywords in _TYPE_RULES:
        if any(kw in text for kw in keywords):
            return ctype
    return "Other"

AGENT_COLS = ["name", "phone", "email", "office", "city", "state", "source"]


class ContractorDB:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self._dsn = dsn
        self._con = psycopg2.connect(dsn)
        self._con.autocommit = False
        self._create_tables()

    # ─────────────────────────────────────────────────────────
    # Schema
    # ─────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        with self._con.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS contractors (
                id              SERIAL PRIMARY KEY,
                dedup_key       TEXT    NOT NULL UNIQUE,
                name            TEXT    NOT NULL,
                phone           TEXT    DEFAULT '',
                address         TEXT    DEFAULT '',
                city            TEXT    DEFAULT '',
                state           TEXT    DEFAULT '',
                zip_code        TEXT    DEFAULT '',
                email           TEXT    DEFAULT '',
                website         TEXT    DEFAULT '',
                category        TEXT    DEFAULT '',
                license_number  TEXT    DEFAULT '',
                license_type    TEXT    DEFAULT '',
                license_expiry  TEXT    DEFAULT '',
                types_of_work   TEXT    DEFAULT '',
                bbb_rating      TEXT    DEFAULT '',
                google_rating   TEXT    DEFAULT '',
                review_count    TEXT    DEFAULT '',
                bbb_profile_url TEXT    DEFAULT '',
                source          TEXT    DEFAULT '',
                scrape_date     DATE    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_contractors_city_state
                ON contractors (LOWER(city), UPPER(state));

            CREATE TABLE IF NOT EXISTS agents (
                id           SERIAL PRIMARY KEY,
                dedup_key    TEXT    NOT NULL UNIQUE,
                name         TEXT    NOT NULL,
                phone        TEXT    DEFAULT '',
                email        TEXT    DEFAULT '',
                office       TEXT    DEFAULT '',
                city         TEXT    DEFAULT '',
                state        TEXT    DEFAULT '',
                target_city  TEXT    DEFAULT '',
                target_state TEXT    DEFAULT '',
                is_neighbor  BOOLEAN DEFAULT FALSE,
                source       TEXT    DEFAULT '',
                scrape_date  DATE    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_agents_city_state
                ON agents (LOWER(city), UPPER(state));

            CREATE TABLE IF NOT EXISTS scrape_log (
                id           SERIAL PRIMARY KEY,
                table_name   TEXT NOT NULL,
                city         TEXT NOT NULL,
                state        TEXT NOT NULL,
                scraped_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                record_count INTEGER DEFAULT 0
            );
            """)

            # Migrations — add new columns to existing tables safely
            migrations = [
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS target_city  TEXT    DEFAULT ''",
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS target_state TEXT    DEFAULT ''",
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_neighbor  BOOLEAN DEFAULT FALSE",
                "CREATE INDEX IF NOT EXISTS idx_agents_target ON agents (LOWER(target_city), UPPER(target_state))",
            ]
            for sql in migrations:
                cur.execute(sql)

        self._con.commit()

    # ─────────────────────────────────────────────────────────
    # Dedup key helpers
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _contractor_key(name: str, phone: str, address: str, city: str) -> str:
        norm_name = re.sub(r"[^a-z0-9]", "", name.lower())
        norm_phone = re.sub(r"\D", "", phone)
        if norm_phone:
            return f"{norm_name}|{norm_phone}"
        norm_loc = re.sub(r"[^a-z0-9]", "", (address + city).lower())
        return f"{norm_name}|{norm_loc}"

    @staticmethod
    def _agent_key(name: str, phone: str, email: str) -> str:
        norm_name = re.sub(r"[^a-z0-9]", "", name.lower())
        norm_phone = re.sub(r"\D", "", phone)
        if norm_phone:
            return f"{norm_name}|{norm_phone}"
        return f"{norm_name}|{email.lower().strip()}"

    # ─────────────────────────────────────────────────────────
    # Scrape freshness check
    # ─────────────────────────────────────────────────────────

    def city_scraped(self, table: str, city: str, state: str, max_age_days: int = 30) -> bool:
        """Returns True if city/state was scraped within max_age_days — skip re-scrape."""
        with self._con.cursor() as cur:
            cur.execute("""
                SELECT scraped_at FROM scrape_log
                WHERE table_name=%s AND LOWER(city)=LOWER(%s) AND UPPER(state)=UPPER(%s)
                ORDER BY scraped_at DESC LIMIT 1
            """, (table, city, state))
            row = cur.fetchone()
        if not row:
            return False
        age_days = (datetime.now(timezone.utc) - row[0]).days
        return age_days <= max_age_days

    def _log_scrape(self, cur: Any, table: str, city: str, state: str, count: int) -> None:
        cur.execute(
            "INSERT INTO scrape_log (table_name, city, state, scraped_at, record_count) "
            "VALUES (%s, %s, %s, NOW(), %s)",
            (table, city, state, count)
        )

    # ─────────────────────────────────────────────────────────
    # Upsert contractors
    # ─────────────────────────────────────────────────────────

    def upsert_contractors(self, contractors: list[Any], city: str, state: str) -> int:
        """
        Insert or update contractors. Accepts Contractor dataclass instances or dicts.
        Enriches existing records (fills blank fields) rather than overwriting.
        Returns count inserted/updated.
        """
        today = date.today()
        count = 0

        with self._con.cursor() as cur:
            for c in contractors:
                row = c if isinstance(c, dict) else {f: getattr(c, f) for f in CONTRACTOR_COLS}
                name = row.get("name", "").strip()
                if not name:
                    continue

                key = self._contractor_key(
                    name,
                    row.get("phone", ""),
                    row.get("address", ""),
                    row.get("city", ""),
                )
                ctype = classify_contractor(
                    row.get("category", ""),
                    row.get("types_of_work", ""),
                    name,
                )

                cur.execute("""
                    INSERT INTO contractors
                        (dedup_key, name, phone, address, city, state, zip_code,
                         email, website, category, contractor_type, license_number, license_type,
                         license_expiry, types_of_work, bbb_rating, google_rating,
                         review_count, bbb_profile_url, source, scrape_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (dedup_key) DO UPDATE SET
                        phone           = CASE WHEN contractors.phone='' AND EXCLUDED.phone!='' THEN EXCLUDED.phone ELSE contractors.phone END,
                        address         = CASE WHEN contractors.address='' AND EXCLUDED.address!='' THEN EXCLUDED.address ELSE contractors.address END,
                        email           = CASE WHEN contractors.email='' AND EXCLUDED.email!='' THEN EXCLUDED.email ELSE contractors.email END,
                        website         = CASE WHEN contractors.website='' AND EXCLUDED.website!='' THEN EXCLUDED.website ELSE contractors.website END,
                        zip_code        = CASE WHEN contractors.zip_code='' AND EXCLUDED.zip_code!='' THEN EXCLUDED.zip_code ELSE contractors.zip_code END,
                        license_number  = CASE WHEN contractors.license_number='' AND EXCLUDED.license_number!='' THEN EXCLUDED.license_number ELSE contractors.license_number END,
                        license_type    = CASE WHEN contractors.license_type='' AND EXCLUDED.license_type!='' THEN EXCLUDED.license_type ELSE contractors.license_type END,
                        bbb_rating      = CASE WHEN contractors.bbb_rating='' AND EXCLUDED.bbb_rating!='' THEN EXCLUDED.bbb_rating ELSE contractors.bbb_rating END,
                        google_rating   = CASE WHEN contractors.google_rating='' AND EXCLUDED.google_rating!='' THEN EXCLUDED.google_rating ELSE contractors.google_rating END,
                        bbb_profile_url = CASE WHEN contractors.bbb_profile_url='' AND EXCLUDED.bbb_profile_url!='' THEN EXCLUDED.bbb_profile_url ELSE contractors.bbb_profile_url END,
                        source          = CASE WHEN contractors.source NOT LIKE '%%' || EXCLUDED.source || '%%' THEN contractors.source || ', ' || EXCLUDED.source ELSE contractors.source END,
                        contractor_type = EXCLUDED.contractor_type,
                        scrape_date     = EXCLUDED.scrape_date
                """, (
                    key, name,
                    row.get("phone", ""), row.get("address", ""),
                    row.get("city", city), row.get("state", state),
                    row.get("zip_code", ""), row.get("email", ""),
                    row.get("website", ""), row.get("category", ""),
                    ctype,
                    row.get("license_number", ""), row.get("license_type", ""),
                    row.get("license_expiry", ""), row.get("types_of_work", ""),
                    row.get("bbb_rating", ""), row.get("google_rating", ""),
                    row.get("review_count", ""), row.get("bbb_profile_url", ""),
                    row.get("source", ""), today,
                ))
                count += 1

            self._log_scrape(cur, "contractors", city, state, count)
        self._con.commit()
        return count

    # ─────────────────────────────────────────────────────────
    # Upsert agents
    # ─────────────────────────────────────────────────────────

    def upsert_agents(self, agents: list[dict], city: str, state: str,
                      target_city: str = "", target_state: str = "",
                      is_neighbor: bool = False) -> int:
        """
        Insert or update agents. Each agent is a dict with keys:
        name, phone, email, office, city, state, source.
        target_city/target_state mark which hub city this agent belongs to.
        is_neighbor=True when city != target_city.
        Returns count inserted/updated.
        """
        today = date.today()
        count = 0
        tgt_city  = target_city  or city
        tgt_state = target_state or state

        with self._con.cursor() as cur:
            for a in agents:
                name = a.get("name", "").strip()
                if not name:
                    continue

                key = self._agent_key(name, a.get("phone", ""), a.get("email", ""))

                cur.execute("""
                    INSERT INTO agents (dedup_key, name, phone, email, office, city, state,
                                        target_city, target_state, is_neighbor, source, scrape_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (dedup_key) DO UPDATE SET
                        phone        = CASE WHEN agents.phone='' AND EXCLUDED.phone!='' THEN EXCLUDED.phone ELSE agents.phone END,
                        email        = CASE WHEN agents.email='' AND EXCLUDED.email!='' THEN EXCLUDED.email ELSE agents.email END,
                        office       = CASE WHEN agents.office='' AND EXCLUDED.office!='' THEN EXCLUDED.office ELSE agents.office END,
                        target_city  = EXCLUDED.target_city,
                        target_state = EXCLUDED.target_state,
                        is_neighbor  = EXCLUDED.is_neighbor,
                        source       = CASE WHEN agents.source NOT LIKE '%%' || EXCLUDED.source || '%%' THEN agents.source || ', ' || EXCLUDED.source ELSE agents.source END,
                        scrape_date  = EXCLUDED.scrape_date
                """, (
                    key, name,
                    a.get("phone", ""), a.get("email", ""),
                    a.get("office", ""), a.get("city", city),
                    a.get("state", state), tgt_city, tgt_state, is_neighbor,
                    a.get("source", ""), today,
                ))
                count += 1

            self._log_scrape(cur, "agents", city, state, count)
        self._con.commit()
        return count

    # ─────────────────────────────────────────────────────────
    # Query
    # ─────────────────────────────────────────────────────────

    def get_contractors(self, city: Optional[str] = None, state: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM contractors WHERE 1=1"
        params: list[str] = []
        if city:
            q += " AND LOWER(city)=LOWER(%s)"
            params.append(city)
        if state:
            q += " AND UPPER(state)=UPPER(%s)"
            params.append(state)
        q += " ORDER BY name"
        with self._con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            return [dict(r) for r in cur.fetchall()]

    def get_agents(self, city: Optional[str] = None, state: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM agents WHERE 1=1"
        params: list[str] = []
        if city:
            q += " AND LOWER(city)=LOWER(%s)"
            params.append(city)
        if state:
            q += " AND UPPER(state)=UPPER(%s)"
            params.append(state)
        q += " ORDER BY name"
        with self._con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        with self._con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM contractors")
            total_contractors = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM agents")
            total_agents = cur.fetchone()["n"]
            cur.execute("SELECT city, state, COUNT(*) AS cnt FROM contractors GROUP BY city, state ORDER BY cnt DESC")
            contractor_cities = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT city, state, COUNT(*) AS cnt FROM agents GROUP BY city, state ORDER BY cnt DESC")
            agent_cities = [dict(r) for r in cur.fetchall()]
        return {
            "total_contractors": total_contractors,
            "total_agents": total_agents,
            "contractor_cities": contractor_cities,
            "agent_cities": agent_cities,
        }

    # ─────────────────────────────────────────────────────────
    # Export to CSV
    # ─────────────────────────────────────────────────────────

    def export_csv(self, table: str, path: str,
                   city: Optional[str] = None, state: Optional[str] = None) -> int:
        if table == "contractors":
            rows = self.get_contractors(city, state)
            cols = CONTRACTOR_COLS + ["scrape_date"]
        else:
            rows = self.get_agents(city, state)
            cols = AGENT_COLS + ["scrape_date"]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def close(self) -> None:
        self._con.close()


# ─────────────────────────────────────────────────────────────
# CLI — stats / export
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Real Estate DB utility")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("stats", help="Show DB stats")

    p_export = sub.add_parser("export", help="Export city slice to CSV")
    p_export.add_argument("--table", choices=["contractors", "agents"], default="contractors")
    p_export.add_argument("--city", required=True)
    p_export.add_argument("--state", required=True)
    p_export.add_argument("--out", required=True)

    args = parser.parse_args()
    db = ContractorDB()

    if args.cmd == "stats":
        s = db.stats()
        print(f"\nContractors : {s['total_contractors']:,}")
        print(f"Agents      : {s['total_agents']:,}\n")
        print("Contractors by city:")
        for r in s["contractor_cities"]:
            print(f"  {r['city']}, {r['state']}: {r['cnt']:,}")
        print("\nAgents by city:")
        for r in s["agent_cities"]:
            print(f"  {r['city']}, {r['state']}: {r['cnt']:,}")

    elif args.cmd == "export":
        n = db.export_csv(args.table, args.out, args.city, args.state)
        print(f"Exported {n} rows → {args.out}")

    db.close()
