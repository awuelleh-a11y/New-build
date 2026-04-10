"""
Fetch real estate listing agent contacts from a configurable data provider.

Provider is set via AGENTS_PROVIDER env var (default: rentcast).
To switch providers: set AGENTS_PROVIDER=redfin (or mls, etc.) in .env

Usage:
    # Single city + auto-discover neighbors (default)
    python3 rentcast_agents.py --city Edinburg --state TX

    # Skip neighbor discovery
    python3 rentcast_agents.py --city Edinburg --state TX --no-nearby

    # Explicit neighbor list (overrides auto-discovery)
    python3 rentcast_agents.py --cities "Copperas Cove,TX;Killeen,TX;Belton,TX" \
        --target-city Kempner --target-state TX
"""

import argparse, csv, os
from pathlib import Path

DATASETS_DIR = Path(__file__).parent / "datasets"

FIELDNAMES = ['name', 'phone', 'email', 'office', 'city', 'state',
              'target_city', 'target_state', 'is_neighbor', 'source']


def fetch_and_save(city: str, state: str, provider, db,
                   target_city: str, target_state: str,
                   is_neighbor: bool, days: int,
                   all_agents: list):
    """Fetch agents for one city, append to all_agents list, optionally upsert to DB."""
    print(f"\n  {'[neighbor]' if is_neighbor else '[target] '} {city}, {state}")

    agents = provider.fetch_agents(city, state, days)
    print(f"    → {len(agents)} agents found")

    for a in agents:
        a['target_city']  = target_city
        a['target_state'] = target_state
        a['is_neighbor']  = is_neighbor

    all_agents.extend(agents)

    if db:
        try:
            n = db.upsert_agents(agents, city, state,
                                 target_city=target_city,
                                 target_state=target_state,
                                 is_neighbor=is_neighbor)
            print(f"    → [DB] upserted {n}")
        except Exception as exc:
            print(f"    → [DB] warning: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Fetch real estate agents via configurable provider")
    # Single-city mode
    parser.add_argument("--city",  default="", help="City name (single-city mode)")
    parser.add_argument("--state", default="", help="State abbreviation (single-city mode)")
    # Batch mode
    parser.add_argument("--cities", default="",
                        help='Semicolon-separated "City,State" pairs, e.g. "Killeen,TX;Belton,TX"')
    # Target city (hub) — used when fetching neighbors
    parser.add_argument("--target-city",  default="", help="Hub/target city to merge into")
    parser.add_argument("--target-state", default="", help="Hub/target state")
    # Common
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--provider", default="",
                        help="Provider: rentcast, redfin, mls (default: $AGENTS_PROVIDER or rentcast)")
    parser.add_argument("--no-db", action="store_true", help="Skip PostgreSQL DB save")
    # Nearby city auto-discovery (mirrors scrape_contractors.py flags)
    parser.add_argument("--no-nearby", action="store_true",
                        help="Skip auto-discovery of neighboring cities")
    parser.add_argument("--nearby-miles", type=float, default=15,
                        help="Starting radius in miles for nearby city discovery (default: 15)")
    parser.add_argument("--min-population", type=int, default=5000,
                        help="Minimum population for nearby cities (default: 5,000)")
    parser.add_argument("--max-nearby", type=int, default=30,
                        help="Max number of nearby cities (default: 30)")
    # Adaptive spanout
    parser.add_argument("--adaptive-target", type=int, default=2000,
                        help="Expand radius 1 mi at a time until this many agents are collected (default: 2000)")
    parser.add_argument("--adaptive-max-miles", type=float, default=25,
                        help="Hard cap on adaptive radius expansion (default: 25 mi)")
    args = parser.parse_args()

    from agents_api import get_provider
    provider = get_provider(args.provider)

    # Build list of (city, state, is_neighbor) tuples to process
    cities_to_fetch: list[tuple[str, str, bool]] = []

    if args.cities:
        # Explicit batch mode — all entries are neighbors of target-city
        for entry in args.cities.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.rsplit(",", 1)
            if len(parts) != 2:
                print(f"  [warn] Skipping malformed entry: '{entry}'")
                continue
            cities_to_fetch.append((parts[0].strip(), parts[1].strip().upper(), True))

    if args.city and args.state:
        is_nb = bool(args.target_city and
                     args.city.lower() != args.target_city.lower())
        cities_to_fetch.insert(0, (args.city, args.state.upper(), is_nb))

    if not cities_to_fetch:
        parser.error("Provide --city/--state or --cities")

    target_city  = args.target_city  or cities_to_fetch[0][0]
    target_state = args.target_state or cities_to_fetch[0][1]

    target_slug      = target_city.replace(" ", "")
    output_path      = DATASETS_DIR / f"rentcast_agents_{target_slug}_{target_state}.csv"
    cityonly_path    = DATASETS_DIR / f"rentcast_agents_{target_slug}_{target_state}_cityonly.csv"
    neighboring_path = DATASETS_DIR / f"rentcast_agents_{target_slug}_{target_state}_neighboring_cities.csv"

    # Connect to DB
    db = None
    if not args.no_db:
        try:
            from contractor_db import ContractorDB
            db = ContractorDB()
        except Exception as exc:
            print(f"[DB] Warning: could not connect — {exc}. Continuing without DB.")

    import re

    def _dedup(agents: list[dict]) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for a in agents:
            name  = re.sub(r"[^a-z0-9]", "", a.get("name", "").lower())
            phone = re.sub(r"\D", "", a.get("phone", ""))
            email = a.get("email", "").lower().strip()
            key   = f"{name}|{phone or email}"
            if key not in seen:
                seen.add(key)
                out.append(a)
        return out

    all_agents: list[dict] = []

    # ── Step 1: fetch explicit / batch cities (static list) ───
    print(f"\n{'='*60}")
    print(f"  Agent fetch [{provider.name}]")
    print(f"  Target: {target_city}, {target_state}")
    print(f"{'='*60}")

    for city, state, is_neighbor in cities_to_fetch:
        fetch_and_save(city, state, provider, db,
                       target_city, target_state, is_neighbor,
                       args.days, all_agents)

    # ── Step 2: adaptive spanout (single-city mode only) ──────
    adaptive = (not args.no_nearby and not args.cities
                and args.city and args.state)

    if adaptive:
        try:
            from scrape_contractors import get_nearby_cities
        except Exception as exc:
            print(f"  [adaptive] Cannot import get_nearby_cities — {exc}")
            adaptive = False

    if adaptive:
        fetched_cities: set[tuple[str, str]] = {
            (c.lower(), s.upper()) for c, s, _ in cities_to_fetch
        }
        current_radius = args.nearby_miles
        max_radius     = args.adaptive_max_miles
        target_count   = args.adaptive_target

        deduped = _dedup(all_agents)
        print(f"\n  [adaptive] Starting at {current_radius:.0f} mi | "
              f"{len(deduped)} agents | target={target_count} | max={max_radius:.0f} mi")

        while len(deduped) < target_count and current_radius < max_radius:
            current_radius += 1
            all_at_radius = get_nearby_cities(
                target_city, target_state,
                max_miles=current_radius,
                min_population=args.min_population,
                max_nearby=args.max_nearby,
            )
            # Only fetch cities we haven't scraped yet (the new ring)
            new_cities = [
                (nc, ns) for nc, ns in all_at_radius
                if (nc.lower(), ns.upper()) not in fetched_cities
            ]
            if new_cities:
                print(f"  [adaptive] radius={current_radius:.0f} mi → "
                      f"{len(new_cities)} new city/cities to fetch")
                for nc, ns in new_cities:
                    fetch_and_save(nc, ns, provider, db,
                                   target_city, target_state, True,
                                   args.days, all_agents)
                    fetched_cities.add((nc.lower(), ns.upper()))
                deduped = _dedup(all_agents)
                print(f"  [adaptive] total unique agents so far: {len(deduped)}")
            else:
                print(f"  [adaptive] radius={current_radius:.0f} mi → no new cities, expanding…")

        stop_reason = (
            f"reached target ({len(deduped)} ≥ {target_count})"
            if len(deduped) >= target_count
            else f"hit max radius ({max_radius:.0f} mi) with {len(deduped)} agents"
        )
        print(f"\n  [adaptive] Done — {stop_reason}")

    deduped = _dedup(all_agents)

    # Split into city-only vs neighboring
    city_rows      = [a for a in deduped if not str(a.get("is_neighbor","")).lower() in ("true","1")]
    neighbor_rows  = [a for a in deduped if     str(a.get("is_neighbor","")).lower() in ("true","1")]

    def _write(path, rows):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    # Combined (all cities)
    _write(output_path, deduped)
    # City only
    _write(cityonly_path, city_rows)
    # Neighboring cities only
    _write(neighboring_path, neighbor_rows)

    print(f"\n{'='*60}")
    print(f"  Saved {len(deduped)} agents → {output_path.name}")
    print(f"  Saved {len(city_rows)} agents → {cityonly_path.name}")
    print(f"  Saved {len(neighbor_rows)} agents → {neighboring_path.name}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
