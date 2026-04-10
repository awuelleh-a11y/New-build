"""
Fetch seller (listing) agent name + phone from Redfin sold listings
for a given city, then merge into the existing contractors CSV.

Usage:
    python3 redfin_agents.py --city "Edinburg" --state TX
"""

import csv, json, time, re, io, argparse, urllib.request, urllib.parse

BASE = 'https://www.redfin.com'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.redfin.com/',
}
FIELDNAMES = ['name','phone','address','city','state','zip_code','email','website',
              'category','license_number','license_type','license_expiry',
              'types_of_work','bbb_rating','google_rating','review_count','bbb_profile_url','source']


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode('utf-8')
    if raw.startswith('{}&&'):
        raw = raw[4:]
    return json.loads(raw)


def fetch_text(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8')


def get_region(city, state):
    query = urllib.parse.quote(f'{city}, {state}')
    data = fetch_json(f'{BASE}/stingray/do/location-autocomplete?location={query}&v=2')
    rows = data['payload']['sections'][0]['rows']
    for row in rows:
        if row.get('subName', '').upper() == state.upper() or state.upper() in row.get('name', '').upper():
            rid = row['id']['tableId']
            rtype = row['id']['type']
            print(f"Region: {row['name']} | id={rid} type={rtype}")
            return rid, rtype
    # fallback to first result
    rid = rows[0]['id']['tableId']
    rtype = rows[0]['id']['type']
    print(f"Region (fallback): {rows[0]['name']} | id={rid} type={rtype}")
    return rid, rtype


def download_sold_csv(region_id, region_type, days=365):
    url = (f'{BASE}/stingray/api/gis-csv?al=1&num_homes=350'
           f'&region_id={region_id}&region_type={region_type}'
           f'&sold_within_days={days}&status=9&uipt=1,2,3,4,5,6,7&v=8')
    print(f"Downloading sold listings (last {days} days)...")
    raw = fetch_text(url)
    reader = csv.DictReader(io.StringIO(raw))
    props = list(reader)
    print(f"  → {len(props)} sold properties found")
    return props


def extract_property_id(url_str):
    m = re.search(r'/home/(\d+)', url_str)
    return m.group(1) if m else None


def get_agent_info(property_id):
    """Returns (agent_name, agent_phone, brokerage) or None."""
    url = f'{BASE}/stingray/api/home/details/aboveTheFold?propertyId={property_id}&accessLevel=1'
    try:
        data = fetch_json(url)
        payload = data.get('payload', {})

        # Try multiple possible locations in the response
        # 1. mainHouseInfo
        mhi = payload.get('mainHouseInfo', {})
        name = mhi.get('listingAgentName', '') or mhi.get('agentName', '')
        phone = mhi.get('listingAgentPhone', '') or mhi.get('agentPhone', '')
        brokerage = mhi.get('brokerageName', '') or mhi.get('listingBrokerageName', '')

        # 2. agentInfo block
        if not name:
            ai = payload.get('agentInfo', {})
            la = ai.get('listingAgent', {})
            name = la.get('name', '') or la.get('agentName', '')
            phone = la.get('phone', '') or la.get('phoneNumber', '')
            brokerage = la.get('brokerageName', '')

        # 3. Flatten search for any agentName field
        if not name:
            raw_str = json.dumps(payload)
            m = re.search(r'"(?:listing)?[Aa]gent[Nn]ame"\s*:\s*"([^"]+)"', raw_str)
            if m:
                name = m.group(1)
            m2 = re.search(r'"(?:listing)?[Aa]gent[Pp]hone"\s*:\s*"([^"]+)"', raw_str)
            if m2:
                phone = m2.group(1)
            m3 = re.search(r'"brokerage[Nn]ame"\s*:\s*"([^"]+)"', raw_str)
            if m3:
                brokerage = m3.group(1)

        if name:
            return name.strip(), phone.strip(), brokerage.strip()
    except Exception as e:
        pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--city', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--delay', type=float, default=0.8, help='Seconds between requests')
    args = parser.parse_args()

    city, state = args.city, args.state.upper()
    datasets_dir = '/Users/gunaranjanramireddy/lotusAI/sw/realestate-agent/datasets'
    city_slug = city.replace(' ', '')
    master_path = f'{datasets_dir}/contractors_{city_slug}_{state}.csv'
    output_path = f'{datasets_dir}/redfin_agents_{city_slug}_{state}.csv'

    # Step 1: Region lookup
    region_id, region_type = get_region(city, state)
    time.sleep(1)

    # Step 2: Sold listings CSV
    properties = download_sold_csv(region_id, region_type, days=args.days)
    time.sleep(1)

    # Step 3: Fetch agent info for each property
    agents = {}  # name.lower() -> row dict
    url_col = next((k for k in (properties[0].keys() if properties else []) if 'URL' in k.upper()), None)

    print(f"\nFetching agent info for {len(properties)} properties...")
    for i, prop in enumerate(properties):
        url_str = prop.get(url_col, '') if url_col else ''
        prop_id = extract_property_id(url_str)
        if not prop_id:
            continue

        result = get_agent_info(prop_id)
        if result:
            name, phone, brokerage = result
            key = name.lower()
            if key not in agents:
                agents[key] = {
                    'name': name,
                    'phone': phone,
                    'address': '',
                    'city': city,
                    'state': state,
                    'zip_code': prop.get('ZIP OR POSTAL CODE', '').strip(),
                    'email': '',
                    'website': '',
                    'category': 'Real Estate Agent',
                    'license_number': '',
                    'license_type': 'Real Estate Agent',
                    'license_expiry': '',
                    'types_of_work': brokerage,
                    'bbb_rating': '',
                    'google_rating': '',
                    'review_count': '',
                    'bbb_profile_url': '',
                    'source': 'redfin_sold',
                }
                print(f"  [{i+1}/{len(properties)}] {name} | {phone} | {brokerage}")

        time.sleep(args.delay)

    print(f"\nUnique listing agents found: {len(agents)}")

    # Step 4: Save standalone agents file
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(agents.values())
    print(f"Saved → {output_path}")

    # Step 5: Merge into master contractors CSV
    try:
        with open(master_path) as f:
            existing = list(csv.DictReader(f))
        existing_names = {r['name'].strip().lower() for r in existing}
        new_rows = [r for r in agents.values() if r['name'].strip().lower() not in existing_names]
        with open(master_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writerows(new_rows)
        print(f"Merged {len(new_rows)} new agents into {master_path}")
        print(f"Total records now: {len(existing) + len(new_rows)}")
    except FileNotFoundError:
        print(f"Master file not found: {master_path} — skipping merge")


if __name__ == '__main__':
    main()
