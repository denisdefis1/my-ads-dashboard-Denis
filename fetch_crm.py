import os
import re
import csv
import io
import sys
import json
import difflib
import requests
from datetime import datetime, timezone

SHEET_ID = '1gcHoNF5FMpnhwYyi0sOEr-ZSD6NQ3LZ2Xz5aYkPBlq4'
SHEET_GID = '0'
QUAL_VALUE = 'квал'
NOT_QUAL_VALUE = 'не квал'
EXPECTED_HEADER_LEN = 16

CSV_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}'

LEAD_FORM_SHEETS = [
    '1odo97vIg5Il5AFV-bb4wMbFMuy9Txy7PnIOuHdeTZlk',
    '1lv4EFZxUkvE9N_0Rmhf63eo7LHbvjqIj1S4HwzPr97U',
    '1KfugmWce9tZw1NA2iMJqfFAZxeNwfektQirllqYC7u0',
    '1y6qYq5cl3BlSpIsUqPL1FcNEBmGlPS8sAQULM457Nps',
]

# Предположение: "Дата создания" в CRM записана в тбилисском времени (UTC+4).
# Если совпадения будут систематически промахиваться на фиксированное число
# часов — значит это предположение неверное, поправить тут одну цифру.
# Точное время в разных источниках не согласовано (проверено вручную —
# расхождение может быть нефиксированным, не просто разница часовых поясов).
# Поэтому матчим только по имени, без временного окна. Порог выше, чем был
# бы при опоре на время, чтобы не путать разных людей с похожими именами.
MATCH_NAME_MIN_RATIO = 0.72


def clean_id(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith('{{') or not value.isdigit():
        return None
    return value


def strip_prefix(value):
    if not value:
        return None
    return re.sub(r'^[a-z]+:', '', value.strip())


def extract_ad_id_from_referer(referer):
    if not referer:
        return None
    match = re.search(r'[?&]utm_content=([^&#]+)', referer)
    if not match:
        return None
    return clean_id(match.group(1))


def parse_crm_date(value):
    try:
        return datetime.strptime(value.strip(), '%d.%m.%Y %H:%M:%S')
    except (ValueError, AttributeError):
        return None


def parse_lead_form_date(value):
    try:
        return datetime.fromisoformat(value.strip()).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def normalize_name(value):
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def normalize_phone(value):
    digits = re.sub(r'\D', '', value or '')
    return digits[-9:] if len(digits) >= 9 else (digits or None)


def name_similarity(a, b):
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def fetch_csv(url):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    return list(csv.reader(io.StringIO(resp.text)))


rows = fetch_csv(CSV_URL)

if len(rows) < 2:
    print("CRM-таблица пустая или недоступна.")
    sys.exit(1)

if len(rows[0]) < EXPECTED_HEADER_LEN:
    print(f"Структура CRM-таблицы изменилась: ожидалось {EXPECTED_HEADER_LEN} колонок, получено {len(rows[0])}.")
    sys.exit(1)

leads = []
for row in rows[1:]:
    if len(row) < EXPECTED_HEADER_LEN:
        row = row + [''] * (EXPECTED_HEADER_LEN - len(row))

    deal_id = row[1].strip()
    if not deal_id:
        continue

    created_at = parse_crm_date(row[0])
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
    elif campaign_id:
        match_type = 'campaign'
    else:
        match_type = None

    leads.append({
        "deal_id": deal_id,
        "created_at": created_at,
        "name_norm": normalize_name(row[4]),
        "stage": stage,
        "country": country,
        "qualified": qualified,
        "ad_id": ad_id,
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "match_type": match_type,
        "match_confidence": 1.0 if match_type else None,
    })

lead_form_entries = []
for sheet_id in LEAD_FORM_SHEETS:
    url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid=0'
    try:
        sheet_rows = fetch_csv(url)
    except requests.exceptions.RequestException as e:
        print(f"Не удалось прочитать lead-form таблицу {sheet_id}: {e}")
        continue

    if len(sheet_rows) < 2:
        continue

    header = [h.strip() for h in sheet_rows[0]]
    name_col = 'full_name' if 'full_name' in header else 'полное_имя' if 'полное_имя' in header else None
    phone_col = 'phone_number' if 'phone_number' in header else 'номер_телефона' if 'номер_телефона' in header else None
    try:
        idx_created = header.index('created_time')
        idx_ad = header.index('ad_id')
        idx_adset = header.index('adset_id')
        idx_campaign = header.index('campaign_id')
        idx_name = header.index(name_col) if name_col else -1
        idx_phone = header.index(phone_col) if phone_col else -1
        if idx_name == -1:
            raise ValueError
    except ValueError:
        print(f"Таблица {sheet_id} имеет неожиданную структуру, пропускаю.")
        continue

    for row in sheet_rows[1:]:
        if len(row) <= max(idx_created, idx_ad, idx_adset, idx_campaign, idx_name):
            continue
        created = parse_lead_form_date(row[idx_created])
        if not created:
            continue
        phone = normalize_phone(row[idx_phone]) if idx_phone != -1 and idx_phone < len(row) else None
        lead_form_entries.append({
            "created_at": created,
            "ad_id": clean_id(strip_prefix(row[idx_ad])),
            "adset_id": clean_id(strip_prefix(row[idx_adset])),
            "campaign_id": clean_id(strip_prefix(row[idx_campaign])),
            "name_norm": normalize_name(row[idx_name]),
            "phone_norm": phone,
        })

matched_ad = matched_campaign = matched_bridge = matched_fuzzy = unmatched = 0

BRIDGE_PATH = 'crm_phone_bridge.csv'
name_to_phone = {}
if os.path.exists(BRIDGE_PATH):
    with open(BRIDGE_PATH, encoding='utf-8-sig') as f:
        bridge_rows = list(csv.reader(f))
    if bridge_rows:
        b_header = [h.strip() for h in bridge_rows[0]]
        try:
            idx_b_name = b_header.index('Имя')
            idx_b_phone = b_header.index('Телефон')
        except ValueError:
            idx_b_name = idx_b_phone = -1
        if idx_b_name != -1:
            for row in bridge_rows[1:]:
                if len(row) <= max(idx_b_name, idx_b_phone):
                    continue
                name_norm = normalize_name(row[idx_b_name])
                phone_norm = normalize_phone(row[idx_b_phone])
                if name_norm and phone_norm and name_norm not in name_to_phone:
                    name_to_phone[name_norm] = phone_norm
    print(f"Мост телефонов: {len(name_to_phone)} записей имя→телефон.")
else:
    print("Файл-мост crm_phone_bridge.csv не найден, пропускаю этот этап.")

phone_to_lead_form = {}
for entry in lead_form_entries:
    if entry["phone_norm"] and (entry["ad_id"] or entry["campaign_id"]):
        phone_to_lead_form.setdefault(entry["phone_norm"], entry)

for lead in leads:
    if lead["match_type"]:
        if lead["match_type"] == 'ad':
            matched_ad += 1
        else:
            matched_campaign += 1
        continue

    if not lead["name_norm"]:
        unmatched += 1
        continue

    phone = name_to_phone.get(lead["name_norm"])
    bridge_hit = phone_to_lead_form.get(phone) if phone else None

    if bridge_hit:
        lead["ad_id"] = bridge_hit["ad_id"]
        lead["adset_id"] = bridge_hit["adset_id"]
        lead["campaign_id"] = bridge_hit["campaign_id"]
        lead["match_type"] = 'bridge_phone'
        lead["match_confidence"] = 1.0
        matched_bridge += 1
        continue

    best = None
    best_score = 0.0
    second_score = 0.0
    for entry in lead_form_entries:
        if not (entry["ad_id"] or entry["campaign_id"]):
            continue
        score = name_similarity(lead["name_norm"], entry["name_norm"])
        if score > best_score:
            second_score = best_score
            best_score = score
            best = entry
        elif score > second_score:
            second_score = score

    ambiguous = best_score - second_score < 0.05 and second_score > 0

    if best and best_score >= MATCH_NAME_MIN_RATIO and not ambiguous:
        lead["ad_id"] = best["ad_id"]
        lead["adset_id"] = best["adset_id"]
        lead["campaign_id"] = best["campaign_id"]
        lead["match_type"] = 'fuzzy'
        lead["match_confidence"] = round(best_score, 2)
        matched_fuzzy += 1
    else:
        unmatched += 1

total = len(leads)
match_rate = round((matched_ad + matched_campaign + matched_bridge + matched_fuzzy) / total * 100, 1) if total else 0

if total < 10:
    print(f"Подозрительно мало строк в CRM: {total}.")
    sys.exit(1)

if match_rate < 50:
    print(f"Match rate подозрительно низкий: {match_rate}%. Останавливаюсь, чтобы не записать мусор.")
    sys.exit(1)

for lead in leads:
    lead["created_at"] = lead["created_at"].strftime('%Y-%m-%d %H:%M:%S') if lead["created_at"] else None
    del lead["name_norm"]

crm_data = {
    "fetched_at": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "total_leads": total,
    "matched_by_ad": matched_ad,
    "matched_by_campaign": matched_campaign,
    "matched_bridge": matched_bridge,
    "matched_fuzzy": matched_fuzzy,
    "unmatched": unmatched,
    "match_rate": match_rate,
    "leads": leads,
}

os.makedirs('data', exist_ok=True)
with open('data/crm.json', 'w', encoding='utf-8') as f:
    json.dump(crm_data, f, ensure_ascii=False, indent=2)

print(f"CRM: {total} лидов, по ad_id {matched_ad}, по campaign_id {matched_campaign}, по телефонному мосту {matched_bridge}, приближённо {matched_fuzzy}, не сматчено {unmatched} ({match_rate}%).")
