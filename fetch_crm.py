import os
import re
import csv
import io
import sys
import json
import requests
from datetime import datetime

SHEET_ID = '1gcHoNF5FMpnhwYyi0sOEr-ZSD6NQ3LZ2Xz5aYkPBlq4'
SHEET_GID = '0'
QUAL_VALUE = 'квал'
NOT_QUAL_VALUE = 'не квал'
EXPECTED_HEADER_LEN = 16

CSV_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}'


def clean_id(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith('{{') or not value.isdigit():
        return None
    return value


def extract_ad_id_from_referer(referer):
    if not referer:
        return None
    match = re.search(r'[?&]utm_content=([^&#]+)', referer)
    if not match:
        return None
    return clean_id(match.group(1))


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%d.%m.%Y %H:%M:%S')
    except (ValueError, AttributeError):
        return None


resp = requests.get(CSV_URL, timeout=60)
resp.raise_for_status()
resp.encoding = 'utf-8'
rows = list(csv.reader(io.StringIO(resp.text)))

if len(rows) < 2:
    print("CRM-таблица пустая или недоступна.")
    sys.exit(1)

if len(rows[0]) < EXPECTED_HEADER_LEN:
    print(f"Структура CRM-таблицы изменилась: ожидалось {EXPECTED_HEADER_LEN} колонок, получено {len(rows[0])}.")
    sys.exit(1)

leads = []
matched_ad = 0
matched_campaign = 0
unmatched = 0

for row in rows[1:]:
    if len(row) < EXPECTED_HEADER_LEN:
        row = row + [''] * (EXPECTED_HEADER_LEN - len(row))

    deal_id = row[1].strip()
    if not deal_id:
        continue

    created_at = parse_date(row[0])
    stage = row[5].strip()
    country = row[6].strip()
    referer = row[7].strip()
    utm_content_col = row[9].strip()
    utm_id_col = row[11].strip()
    utm_term_col = row[12].strip()
    qual_raw = row[14].strip().lower()

    if qual_raw == QUAL_VALUE:
        qualified = True
    elif qual_raw == NOT_QUAL_VALUE:
        qualified = False
    else:
        qualified = None

    ad_id = extract_ad_id_from_referer(referer) or clean_id(utm_content_col)
    campaign_id = clean_id(utm_id_col)
    adset_id = clean_id(utm_term_col)

    if ad_id:
        match_type = 'ad'
        matched_ad += 1
    elif campaign_id:
        match_type = 'campaign'
        matched_campaign += 1
    else:
        match_type = None
        unmatched += 1

    leads.append({
        "deal_id": deal_id,
        "created_at": created_at.strftime('%Y-%m-%d %H:%M:%S') if created_at else None,
        "stage": stage,
        "country": country,
        "qualified": qualified,
        "ad_id": ad_id,
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "match_type": match_type,
    })

total = len(leads)
match_rate = round((matched_ad + matched_campaign) / total * 100, 1) if total else 0

if total < 10:
    print(f"Подозрительно мало строк в CRM: {total}.")
    sys.exit(1)

if match_rate < 50:
    print(f"Match rate подозрительно низкий: {match_rate}%. Останавливаюсь, чтобы не записать мусор.")
    sys.exit(1)

crm_data = {
    "fetched_at": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "total_leads": total,
    "matched_by_ad": matched_ad,
    "matched_by_campaign": matched_campaign,
    "unmatched": unmatched,
    "match_rate": match_rate,
    "leads": leads,
}

os.makedirs('data', exist_ok=True)
with open('data/crm.json', 'w', encoding='utf-8') as f:
    json.dump(crm_data, f, ensure_ascii=False, indent=2)

print(f"CRM: {total} лидов, по ad_id {matched_ad}, по campaign_id {matched_campaign}, не сматчено {unmatched} ({match_rate}%).")
