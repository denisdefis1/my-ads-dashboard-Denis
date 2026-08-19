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
MIN_LEADS = 10
MIN_MATCH_RATE = 50

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


# Zero-width space, ZWNJ, ZWJ, LRM, RLM, BOM, NBSP — built from code points
# rather than embedded literally so the invisible characters can't get
# mangled in transit.
_INVISIBLE_CODEPOINTS = (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0xA0)
INVISIBLE_CHARS_RE = re.compile('[' + ''.join(chr(c) for c in _INVISIBLE_CODEPOINTS) + ']')


def clean_invisible(text):
    """Убирает zero-width пробелы, BOM и неразрывный пробел — Google Sheets
    иногда вставляет их незаметно для глаза, из-за чего точное сравнение строк
    молча ломается (например, ячейка выглядит как "квал", а на самом деле
    содержит "квал" с невидимым символом внутри)."""
    return INVISIBLE_CHARS_RE.sub('', text or '').strip()


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


CRM_DATE_FORMATS = (
    '%d.%m.%Y %H:%M:%S',
    '%d.%m.%Y %H:%M',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
)


def parse_crm_date(value):
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in CRM_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
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


def parse_leads(data_rows):
    """Turns raw CRM sheet rows (header excluded) into deduplicated lead
    dicts, keyed by deal_id — the last occurrence of a given deal_id wins,
    since a repeated deal_id means the same deal was written to the sheet
    more than once, not two distinct leads.

    Returns (leads, duplicate_rows, suspicious_qual_values).
    """
    leads_by_deal_id = {}
    duplicate_rows = 0
    suspicious_qual_values = set()

    for row in data_rows:
        if len(row) < EXPECTED_HEADER_LEN:
            row = row + [''] * (EXPECTED_HEADER_LEN - len(row))

        deal_id = row[1].strip()
        if not deal_id:
            continue

        if deal_id in leads_by_deal_id:
            duplicate_rows += 1

        created_at = parse_crm_date(row[0])
        stage = row[5].strip()
        country = row[6].strip()
        referer = row[7].strip()
        utm_content_col = row[9].strip()
        utm_id_col = row[11].strip()
        utm_term_col = row[12].strip()
        qual_raw = clean_invisible(row[14]).lower()
        if qual_raw and 'квал' in qual_raw and qual_raw not in (QUAL_VALUE, NOT_QUAL_VALUE):
            suspicious_qual_values.add(repr(row[14]))

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

        leads_by_deal_id[deal_id] = {
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
        }

    return list(leads_by_deal_id.values()), duplicate_rows, suspicious_qual_values


def build_lead_form_entries(fetch_csv_fn=fetch_csv):
    lead_form_entries = []
    for sheet_id in LEAD_FORM_SHEETS:
        url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid=0'
        try:
            sheet_rows = fetch_csv_fn(url)
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
    return lead_form_entries


def load_phone_bridge(bridge_path):
    name_to_phone = {}
    if not os.path.exists(bridge_path):
        print("Файл-мост crm_phone_bridge.csv не найден, пропускаю этот этап.")
        return name_to_phone

    with open(bridge_path, encoding='utf-8-sig') as f:
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
    return name_to_phone


def match_leads(leads, lead_form_entries, name_to_phone):
    """Attributes each lead to an ad/campaign, mutating the lead dicts in
    place. Returns a dict of match-type counters."""
    counters = {"matched_ad": 0, "matched_campaign": 0, "matched_bridge": 0, "matched_fuzzy": 0, "unmatched": 0}

    phone_to_lead_form = {}
    for entry in lead_form_entries:
        if entry["phone_norm"] and (entry["ad_id"] or entry["campaign_id"]):
            phone_to_lead_form.setdefault(entry["phone_norm"], entry)

    for lead in leads:
        if lead["match_type"]:
            if lead["match_type"] == 'ad':
                counters["matched_ad"] += 1
            else:
                counters["matched_campaign"] += 1
            continue

        if not lead["name_norm"]:
            counters["unmatched"] += 1
            continue

        phone = name_to_phone.get(lead["name_norm"])
        bridge_hit = phone_to_lead_form.get(phone) if phone else None

        if bridge_hit:
            lead["ad_id"] = bridge_hit["ad_id"]
            lead["adset_id"] = bridge_hit["adset_id"]
            lead["campaign_id"] = bridge_hit["campaign_id"]
            lead["match_type"] = 'bridge_phone'
            lead["match_confidence"] = 1.0
            counters["matched_bridge"] += 1
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
            counters["matched_fuzzy"] += 1
        else:
            counters["unmatched"] += 1

    return counters


def validate_before_write(total, match_rate, min_leads=MIN_LEADS, min_match_rate=MIN_MATCH_RATE):
    """Raises ValueError if the freshly-parsed dataset looks broken, so a
    bad fetch (empty sheet, wrong columns, auth wall, ...) can never
    silently overwrite a good data/crm.json with garbage."""
    if total < min_leads:
        raise ValueError(f"Подозрительно мало строк в CRM: {total}.")
    if match_rate < min_match_rate:
        raise ValueError(f"Match rate подозрительно низкий: {match_rate}%. Останавливаюсь, чтобы не записать мусор.")


def finalize_leads(leads):
    """Converts created_at to an ISO-ish string and drops the internal
    name_norm field used only for matching. Mutates and returns leads."""
    for lead in leads:
        created_at = lead["created_at"]
        lead["created_at"] = created_at.strftime('%Y-%m-%d %H:%M:%S') if created_at else None
        lead.pop("name_norm", None)
    return leads


def compute_data_quality(leads, google_sheets_total_rows, duplicate_rows, suspicious_qual_values, output_timestamp):
    """Computes the diagnostics block. Expects `leads` already finalized
    (created_at is a string or None, as it will be in data/crm.json)."""
    qualified_leads = [l for l in leads if l["qualified"] is True]
    not_qualified_leads = [l for l in leads if l["qualified"] is False]
    pending_leads = [l for l in leads if l["qualified"] is None]
    qualified_with_date = [l for l in qualified_leads if l["created_at"]]
    qualified_without_date = [l for l in qualified_leads if not l["created_at"]]
    august_2026_qualified = [l for l in qualified_with_date if l["created_at"].startswith('2026-08')]
    latest_created_at = max((l["created_at"] for l in leads if l["created_at"]), default=None)

    return {
        "google_sheets_total_rows": google_sheets_total_rows,
        "total_crm_leads": len(leads),
        "qualified": len(qualified_leads),
        "not_qualified": len(not_qualified_leads),
        "pending": len(pending_leads),
        "qualified_with_date": len(qualified_with_date),
        "qualified_without_date": len(qualified_without_date),
        "duplicates_removed": duplicate_rows,
        "august_2026_qualified": len(august_2026_qualified),
        "latest_created_at": latest_created_at,
        "suspicious_qual_values": sorted(suspicious_qual_values),
        "output_timestamp": output_timestamp,
    }


def print_diagnostic(data_quality, output_timestamp, matched, total):
    print("=" * 60)
    print("CRM DIAGNOSTIC")
    print("=" * 60)
    print(f"Fetch timestamp (UTC): {output_timestamp}")
    print(f"Google Sheets total rows: {data_quality['google_sheets_total_rows']}")
    print(f"Total CRM leads (after dedup by deal_id): {data_quality['total_crm_leads']}")
    print(f"Qualified: {data_quality['qualified']}")
    print(f"Not qualified: {data_quality['not_qualified']}")
    print(f"Pending: {data_quality['pending']}")
    print(f"Qualified with date: {data_quality['qualified_with_date']}")
    print(f"Qualified without date: {data_quality['qualified_without_date']}")
    print(f"Duplicates removed (by deal_id): {data_quality['duplicates_removed']}")
    print(f"August 2026 qualified: {data_quality['august_2026_qualified']}")
    print(f"Latest CRM created_at: {data_quality['latest_created_at']}")
    print(f"Output data/crm.json timestamp: {output_timestamp}")
    print("=" * 60)
    print(
        f"CRM: {total} лидов, по ad_id {matched['matched_ad']}, по campaign_id {matched['matched_campaign']}, "
        f"по телефонному мосту {matched['matched_bridge']}, приближённо {matched['matched_fuzzy']}, "
        f"не сматчено {matched['unmatched']} ({matched['match_rate']}%)."
    )
    if data_quality["suspicious_qual_values"]:
        print(
            "Подозрительные значения в колонке 'Квал' (содержат 'квал', но не равны точно 'квал'/'не квал'): "
            f"{data_quality['suspicious_qual_values']}"
        )


def main():
    rows = fetch_csv(CSV_URL)

    if len(rows) < 2:
        print("CRM-таблица пустая или недоступна.")
        sys.exit(1)

    if len(rows[0]) < EXPECTED_HEADER_LEN:
        print(f"Структура CRM-таблицы изменилась: ожидалось {EXPECTED_HEADER_LEN} колонок, получено {len(rows[0])}.")
        sys.exit(1)

    leads, duplicate_rows, suspicious_qual_values = parse_leads(rows[1:])

    lead_form_entries = build_lead_form_entries()
    name_to_phone = load_phone_bridge('crm_phone_bridge.csv')

    counters = match_leads(leads, lead_form_entries, name_to_phone)

    total = len(leads)
    matched_total = counters["matched_ad"] + counters["matched_campaign"] + counters["matched_bridge"] + counters["matched_fuzzy"]
    match_rate = round(matched_total / total * 100, 1) if total else 0
    counters["match_rate"] = match_rate

    try:
        validate_before_write(total, match_rate)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    finalize_leads(leads)

    output_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    data_quality = compute_data_quality(leads, len(rows) - 1, duplicate_rows, suspicious_qual_values, output_timestamp)

    crm_data = {
        "fetched_at": datetime.now().strftime('%d.%m.%Y, %H:%M'),
        "total_leads": total,
        "matched_by_ad": counters["matched_ad"],
        "matched_by_campaign": counters["matched_campaign"],
        "matched_bridge": counters["matched_bridge"],
        "matched_fuzzy": counters["matched_fuzzy"],
        "unmatched": counters["unmatched"],
        "match_rate": match_rate,
        "data_quality": data_quality,
        "leads": leads,
    }

    os.makedirs('data', exist_ok=True)
    crm_path = os.path.join('data', 'crm.json')
    crm_tmp_path = crm_path + '.tmp'
    with open(crm_tmp_path, 'w', encoding='utf-8') as f:
        json.dump(crm_data, f, ensure_ascii=False, indent=2)
    os.replace(crm_tmp_path, crm_path)

    print_diagnostic(data_quality, output_timestamp, counters, total)


if __name__ == '__main__':
    main()
