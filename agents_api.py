#!/usr/bin/env python3
"""
agents_api.py — Pluggable real estate agent data provider.

Swap out the data source by setting AGENTS_PROVIDER in .env:
  AGENTS_PROVIDER=rentcast      (default, uses RENTCAST_API_KEY)
  AGENTS_PROVIDER=redfin        (future)
  AGENTS_PROVIDER=mls           (future)

Each provider implements fetch_agents(city, state, days) → list[dict]
with keys: name, phone, email, office, city, state, source

Usage:
  from agents_api import get_provider
  provider = get_provider()
  agents = provider.fetch_agents("Mansfield", "TX", days=365)
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

# Load .env
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


# ─────────────────────────────────────────────────────────────
# Provider protocol (interface)
# ─────────────────────────────────────────────────────────────

class AgentsProvider(Protocol):
    name: str

    def fetch_agents(self, city: str, state: str, days: int = 365) -> list[dict]:
        """
        Fetch unique listing agents for recently sold homes.
        Returns list of dicts with keys:
          name, phone, email, office, city, state, source
        """
        ...


# ─────────────────────────────────────────────────────────────
# RentCast provider
# ─────────────────────────────────────────────────────────────

class RentCastProvider:
    name = "rentcast"
    BASE_URL = "https://api.rentcast.io/v1"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("RENTCAST_API_KEY", "")
        if not self.api_key:
            raise ValueError("RENTCAST_API_KEY not set. Add it to .env or pass api_key=")

    def _api_get(self, path: str, params: dict) -> tuple[list | dict, int]:
        qs = urllib.parse.urlencode(params)
        url = f"{self.BASE_URL}{path}?{qs}"
        req = urllib.request.Request(url, headers={
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("X-Total-Count", 0))
            return json.loads(r.read().decode()), total

    def fetch_agents(self, city: str, state: str, days: int = 365) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        offset, limit = 0, 500
        seen: dict[str, dict] = {}

        print(f"  [RentCast] Fetching sold listings for {city}, {state} (last {days} days)…")
        while True:
            params = {
                "city": city, "state": state, "status": "Inactive",
                "limit": limit, "offset": offset, "includeTotalCount": "true",
            }
            data, total = self._api_get("/listings/sale", params)
            batch = data if isinstance(data, list) else data.get("listings", [])
            print(f"    Fetched {offset + len(batch)} / {total} listings")

            for listing in batch:
                removed = listing.get("removedDate") or listing.get("lastSeenDate") or ""
                if removed:
                    try:
                        if datetime.fromisoformat(removed.replace("Z", "+00:00")) < cutoff:
                            continue
                    except Exception:
                        pass

                agent = listing.get("listingAgent") or {}
                office = listing.get("listingOffice") or {}
                name = (agent.get("name") or "").strip()
                if not name:
                    continue

                key = name.lower()
                if key in seen:
                    continue

                seen[key] = {
                    "name": name,
                    "phone": (agent.get("phone") or office.get("phone") or "").strip(),
                    "email": (agent.get("email") or office.get("email") or "").strip(),
                    "office": (office.get("name") or "").strip(),
                    "city": (listing.get("city") or city).strip(),
                    "state": (listing.get("state") or state).strip(),
                    "source": "RentCast",
                }

            offset += len(batch)
            if offset >= total or len(batch) < limit:
                break
            time.sleep(0.3)

        agents = list(seen.values())
        print(f"  [RentCast] {len(agents)} unique agents found")
        return agents


# ─────────────────────────────────────────────────────────────
# Future provider stubs (add implementations later)
# ─────────────────────────────────────────────────────────────

class RedfinProvider:
    name = "redfin"

    def fetch_agents(self, city: str, state: str, days: int = 365) -> list[dict]:
        raise NotImplementedError("Redfin provider not yet implemented")


class MLSProvider:
    name = "mls"

    def fetch_agents(self, city: str, state: str, days: int = 365) -> list[dict]:
        raise NotImplementedError("MLS provider not yet implemented")


# ─────────────────────────────────────────────────────────────
# Provider factory
# ─────────────────────────────────────────────────────────────

_PROVIDERS = {
    "rentcast": RentCastProvider,
    "redfin": RedfinProvider,
    "mls": MLSProvider,
}


def get_provider(provider_name: str = "") -> AgentsProvider:
    """
    Return the configured agents provider.
    Reads AGENTS_PROVIDER from env if provider_name not passed.
    Defaults to 'rentcast'.
    """
    name = (provider_name or os.environ.get("AGENTS_PROVIDER", "rentcast")).lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider '{name}'. Available: {list(_PROVIDERS)}")
    return cls()


def list_providers() -> list[str]:
    return list(_PROVIDERS.keys())
