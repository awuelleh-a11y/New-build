import csv, sys, urllib.request, io

CONTRACTOR_PREFIXES = {'EC', 'ME', 'JW', 'JP', 'MP', 'PE', 'AP', 'REG'}
PREFIX_LABELS = {
    'EC': 'Electrical Contractor', 'ME': 'Master Electrician',
    'JW': 'Journeyman Electrician', 'JP': 'Journeyman Plumber',
    'MP': 'Master Plumber', 'PE': 'Professional Engineer',
    'AP': 'Apprentice Plumber', 'REG': 'Registered Contractor'
}
FIELDNAMES = ['name','phone','address','city','state','zip_code','email','website',
              'category','license_number','license_type','license_expiry',
              'types_of_work','bbb_rating','google_rating','review_count','bbb_profile_url','source']

url = "https://data.colorado.gov/api/views/7s5z-vewr/rows.csv?accessType=DOWNLOAD"
print("Downloading Colorado DORA dataset...")
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req) as resp:
    data = resp.read().decode('utf-8')

reader = csv.DictReader(io.StringIO(data))
new_rows = []
for r in reader:
    city = (r.get('city','') or '').strip().upper()
    state = (r.get('state','') or '').strip().upper()
    if city != 'BRIGHTON' or state != 'CO':
        continue
    prefix = (r.get('licensePrefix','') or '').strip()
    if prefix not in CONTRACTOR_PREFIXES:
        continue
    first = (r.get('firstName','') or '').strip()
    last = (r.get('lastName','') or '').strip()
    entity = (r.get('entityName','') or '').strip()
    name = entity or f'{first} {last}'.strip()
    if not name:
        continue
    expiry = (r.get('licenseExpirationDate','') or '')[:10]
    new_rows.append({
        'name': name, 'phone': '', 'address': '',
        'city': 'Brighton', 'state': 'CO',
        'zip_code': (r.get('mailZipCode','') or '').strip(),
        'email': '', 'website': '',
        'category': PREFIX_LABELS.get(prefix, prefix),
        'license_number': (r.get('licenseNumber','') or '').strip(),
        'license_type': PREFIX_LABELS.get(prefix, prefix),
        'license_expiry': expiry,
        'types_of_work': (r.get('subCategory','') or '').strip(),
        'bbb_rating': '', 'google_rating': '', 'review_count': '', 'bbb_profile_url': '',
        'source': 'colorado_dora'
    })

existing_path = '/Users/gunaranjanramireddy/lotusAI/sw/wc-agent/scripts/contractors_Brighton_CO.csv'
with open(existing_path) as f:
    existing = list(csv.DictReader(f))
existing_names = {r['name'].strip().lower() for r in existing}

unique = [r for r in new_rows if r['name'].strip().lower() not in existing_names]

with open(existing_path, 'a', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
    writer.writerows(unique)

print(f'DORA records found for Brighton: {len(new_rows)}')
print(f'New (not in existing {len(existing)}): {len(unique)}')
print(f'Total Brighton now: {len(existing) + len(unique)}')
