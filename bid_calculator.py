"""
ServiceLink Auction Bid Calculator

Fetches property data from ServiceLink + market value from RentCast AVM,
then calculates your optimal bid ceiling given a target discount.

Usage (with ServiceLink URL):
    python3 bid_calculator.py --url "https://www.servicelinkauction.com/Property/Details/XXXXX" --discount 20

Usage (manual property details):
    python3 bid_calculator.py --address "123 Main St, Edinburg, TX 78539" \
        --starting-bid 85000 --increment 1000 --discount 20 --no-premium
"""

import argparse, json, re, sys, urllib.request, urllib.parse
from pathlib import Path

RENTCAST_API_KEY = "bcc95aeda24744088fd177cf798d1ae8"
RENTCAST_BASE = "https://api.rentcast.io/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── RentCast AVM ──────────────────────────────────────────────
def get_avm(address: str, property_type: str = "Single Family") -> dict:
    """Returns {price, priceRangeLow, priceRangeHigh, latitude, longitude}"""
    params = urllib.parse.urlencode({
        "address": address,
        "propertyType": property_type,
    })
    url = f"{RENTCAST_BASE}/avm/value?{params}"
    req = urllib.request.Request(url, headers={"X-Api-Key": RENTCAST_API_KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [RentCast AVM error] {e}")
        return {}


def get_rent_estimate(address: str, property_type: str = "Single Family") -> dict:
    """Returns monthly rent estimate for ROI calc."""
    params = urllib.parse.urlencode({
        "address": address,
        "propertyType": property_type,
    })
    url = f"{RENTCAST_BASE}/avm/rent/long-term?{params}"
    req = urllib.request.Request(url, headers={"X-Api-Key": RENTCAST_API_KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {}


# ── ServiceLink scraper ───────────────────────────────────────
def scrape_servicelink(url: str) -> dict:
    """
    Attempt to scrape property details from a ServiceLink listing page.
    Returns dict with keys: address, starting_bid, current_bid, increment,
    has_premium, reserve_met, beds, baths, sqft, property_type
    """
    import requests
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  [scraper] beautifulsoup4 not installed — use manual mode")
        return {}

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            print(f"  [scraper] HTTP {r.status_code} — use manual mode")
            return {}

        soup = BeautifulSoup(r.text, "html.parser")

        def text(sel):
            el = soup.select_one(sel)
            return el.get_text(strip=True) if el else ""

        def find_amount(patterns):
            for pat in patterns:
                m = re.search(pat, r.text, re.IGNORECASE)
                if m:
                    return int(m.group(1).replace(",", ""))
            return None

        # Address
        address = (
            text("h1.property-address") or
            text("[class*='address']") or
            text("h1")
        )

        # Bids — look in JSON data embedded in page or HTML elements
        starting_bid = find_amount([
            r'"startingBid"\s*:\s*([\d,]+)',
            r'Starting Bid[^$]*\$([\d,]+)',
            r'class="starting-bid"[^>]*>\s*\$([\d,]+)',
        ])
        current_bid = find_amount([
            r'"currentBid"\s*:\s*([\d,]+)',
            r'Current Bid[^$]*\$([\d,]+)',
            r'class="current-bid"[^>]*>\s*\$([\d,]+)',
        ])
        increment = find_amount([
            r'"bidIncrement"\s*:\s*([\d,]+)',
            r'Bid Increment[^$]*\$([\d,]+)',
            r'increment[^$]*\$([\d,]+)',
        ])

        # Buyer premium
        has_premium = "buyer" in r.text.lower() and "premium" in r.text.lower()
        no_premium_flag = bool(re.search(r'no buyer.{0,10}premium', r.text, re.IGNORECASE))
        has_premium = has_premium and not no_premium_flag

        # Reserve
        reserve_met = bool(re.search(r'reserve\s+met', r.text, re.IGNORECASE))

        # Property details
        beds = find_amount([r'"bedrooms"\s*:\s*(\d+)', r'(\d+)\s*[Bb]ed'])
        baths = find_amount([r'"bathrooms"\s*:\s*(\d+)', r'(\d+)\s*[Bb]ath'])
        sqft = find_amount([r'"squareFootage"\s*:\s*([\d,]+)', r'([\d,]+)\s*[Ss]q\s*[Ff]t'])
        prop_type = text("[class*='property-type']") or "Single Family"

        return {
            "address": address,
            "starting_bid": starting_bid,
            "current_bid": current_bid,
            "increment": increment or 1000,
            "has_premium": has_premium,
            "reserve_met": reserve_met,
            "beds": beds,
            "baths": baths,
            "sqft": sqft,
            "property_type": prop_type,
        }
    except Exception as e:
        print(f"  [scraper error] {e}")
        return {}


# ── Bid calculation engine ────────────────────────────────────
def calculate_bid(
    market_value: float,
    market_low: float,
    market_high: float,
    starting_bid: int,
    current_bid: int,
    increment: int,
    discount_pct: float,       # e.g. 20 = buy at 20% below market
    has_premium: bool,
    monthly_rent: float = 0,
) -> dict:
    """Core bidding algorithm."""

    # 1. Max acceptable price before fees (your true ceiling)
    map_price = market_value * (1 - discount_pct / 100)

    # 2. Adjust for buyer's premium (5% + $2,500 min)
    if has_premium:
        # bid_ceiling + max(bid_ceiling * 0.05, 2500) = map_price
        # bid_ceiling * 1.05 = map_price - 2500  → conservative
        bid_ceiling_raw = (map_price - 2500) / 1.05
        premium_cost = max(bid_ceiling_raw * 0.05, 2500)
    else:
        bid_ceiling_raw = map_price
        premium_cost = 0

    # 3. Round DOWN to nearest increment, then +$1 (beats round-number proxy bids)
    bid_ceiling = int(bid_ceiling_raw // increment) * increment + 1

    # 4. Minimum viable bid (must beat current or starting)
    min_next_bid = (current_bid or starting_bid or 0) + increment

    # 5. Deal quality
    total_cost = bid_ceiling + premium_cost
    discount_achieved = (1 - total_cost / market_value) * 100
    is_deal = bid_ceiling >= min_next_bid

    # 6. ROI if rented
    annual_rent = monthly_rent * 12
    cap_rate = (annual_rent / total_cost * 100) if monthly_rent and total_cost else 0

    return {
        "market_value": market_value,
        "market_low": market_low,
        "market_high": market_high,
        "map_price": map_price,
        "bid_ceiling": bid_ceiling,
        "bid_ceiling_raw": bid_ceiling_raw,
        "premium_cost": premium_cost,
        "total_cost": total_cost,
        "discount_achieved": discount_achieved,
        "min_next_bid": min_next_bid,
        "can_compete": bid_ceiling >= min_next_bid,
        "snipe_bid": bid_ceiling,  # place this as proxy in final 2 min
        "cap_rate": cap_rate,
        "monthly_rent": monthly_rent,
    }


def print_report(prop: dict, result: dict, discount_pct: float):
    print(f"\n{'='*60}")
    print(f"  SERVICELINK BID ANALYSIS")
    print(f"{'='*60}")

    if prop.get("address"):
        print(f"\n  Property : {prop['address']}")
    if prop.get("beds"):
        print(f"  Details  : {prop['beds']}bd / {prop['baths']}ba — {prop.get('sqft','?')} sqft")
    if prop.get("reserve_met") is not None:
        status = "YES" if prop["reserve_met"] else "NO"
        print(f"  Reserve  : {status}")

    print(f"\n── Market Value (RentCast AVM) ──────────────────────────")
    print(f"  Estimated : ${result['market_value']:,.0f}")
    print(f"  Range     : ${result['market_low']:,.0f} – ${result['market_high']:,.0f}")

    print(f"\n── Auction Details ──────────────────────────────────────")
    print(f"  Starting bid  : ${prop.get('starting_bid', 0):,}")
    if prop.get("current_bid"):
        print(f"  Current bid   : ${prop['current_bid']:,}")
    print(f"  Bid increment : ${prop.get('increment', 1000):,}")
    print(f"  Buyer premium : {'5% + $2,500 min' if prop.get('has_premium') else 'None (foreclosure)'}")

    print(f"\n── Your Bid Strategy ({discount_pct:.0f}% below market) ────────────────")
    print(f"  Target max price (before fees) : ${result['map_price']:,.0f}")
    print(f"  Estimated premium cost         : ${result['premium_cost']:,.0f}")
    print(f"  YOUR BID CEILING               : ${result['bid_ceiling']:,.0f}  ← place as proxy")
    print(f"  Total cost if you win          : ${result['total_cost']:,.0f}")
    print(f"  Actual discount achieved       : {result['discount_achieved']:.1f}% below market")

    if result['monthly_rent']:
        print(f"\n── Rental ROI ───────────────────────────────────────────")
        print(f"  Estimated monthly rent : ${result['monthly_rent']:,.0f}")
        print(f"  Annual gross rent      : ${result['monthly_rent']*12:,.0f}")
        print(f"  Cap rate               : {result['cap_rate']:.1f}%")

    print(f"\n── Recommendation ───────────────────────────────────────")
    if not result['can_compete']:
        print(f"  ⚠  Current bid (${prop.get('current_bid',0):,}) already exceeds your ceiling.")
        print(f"     PASS on this property at your target discount.")
    else:
        gap = result['bid_ceiling'] - (prop.get('current_bid') or prop.get('starting_bid') or 0)
        print(f"  ✓  You have ${gap:,} of room before hitting your ceiling.")
        print(f"  → Place a proxy bid of ${result['bid_ceiling']:,} in the final 2 minutes.")
        print(f"     The system will auto-bid incrementally up to that amount.")
        print(f"     The +$1 over round number is intentional — beats proxy bids set at round figures.")
    print(f"{'='*60}\n")


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ServiceLink Auction Bid Calculator")
    parser.add_argument("--url", help="ServiceLink property listing URL")
    parser.add_argument("--address", help="Property address (for AVM lookup)")
    parser.add_argument("--discount", type=float, default=20,
                        help="Target %% below market value (default: 20)")
    parser.add_argument("--starting-bid", type=int, help="Auction starting bid ($)")
    parser.add_argument("--current-bid", type=int, help="Current highest bid ($)")
    parser.add_argument("--increment", type=int, default=1000, help="Bid increment ($)")
    parser.add_argument("--no-premium", action="store_true",
                        help="No buyer's premium (foreclosure property)")
    parser.add_argument("--property-type", default="Single Family",
                        help="Property type for AVM (default: Single Family)")
    args = parser.parse_args()

    prop = {}

    # Step 1: Get property data
    if args.url:
        print(f"Fetching ServiceLink listing...")
        prop = scrape_servicelink(args.url)
        if prop:
            print(f"  ✓ Scraped: {prop.get('address', 'address not found')}")
        else:
            print("  Could not auto-scrape — using manual inputs")

    # Override with manual args
    if args.address:
        prop["address"] = args.address
    if args.starting_bid:
        prop["starting_bid"] = args.starting_bid
    if args.current_bid:
        prop["current_bid"] = args.current_bid
    if args.increment:
        prop["increment"] = args.increment
    if args.no_premium:
        prop["has_premium"] = False
    elif "has_premium" not in prop:
        prop["has_premium"] = True  # default: assume premium applies

    if not prop.get("address"):
        print("Error: provide --url or --address")
        sys.exit(1)

    # Step 2: RentCast AVM
    print(f"Getting market value from RentCast for: {prop['address']}")
    avm = get_avm(prop["address"], args.property_type)
    if not avm or not avm.get("price"):
        print("Error: could not get AVM — check address or RentCast subscription")
        sys.exit(1)

    market_value = avm["price"]
    market_low = avm.get("priceRangeLow", market_value * 0.9)
    market_high = avm.get("priceRangeHigh", market_value * 1.1)
    print(f"  ✓ AVM: ${market_value:,.0f} (range ${market_low:,.0f}–${market_high:,.0f})")

    # Step 3: Rent estimate for ROI
    rent_data = get_rent_estimate(prop["address"], args.property_type)
    monthly_rent = rent_data.get("rent", 0)
    if monthly_rent:
        print(f"  ✓ Rent estimate: ${monthly_rent:,.0f}/mo")

    # Step 4: Calculate
    result = calculate_bid(
        market_value=market_value,
        market_low=market_low,
        market_high=market_high,
        starting_bid=prop.get("starting_bid", 0),
        current_bid=prop.get("current_bid", 0),
        increment=prop.get("increment", 1000),
        discount_pct=args.discount,
        has_premium=prop.get("has_premium", True),
        monthly_rent=monthly_rent,
    )

    # Step 5: Report
    print_report(prop, result, args.discount)


if __name__ == "__main__":
    main()
