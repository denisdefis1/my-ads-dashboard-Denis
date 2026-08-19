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


# Кодпоинты вместо литеральных символов в исходнике: ZERO WIDTH SPACE,
# ZERO WIDTH NON-JOINER, ZERO WIDTH JOINER, LEFT-TO-RIGHT MARK,
# RIGHT-TO-LEFT MARK, BOM/ZERO WIDTH NO-BREAK SPACE. Литералы (в т.ч.
# bidi-символы U+200E/U+200F) в исходном коде — плохая идея сама по себе
# (см. "Trojan Source", CVE-2021-42574).
_ZERO_WIDTH_CODEPOINTS = (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF)
ZERO_WIDTH_CHARS_RE = re.compile('[' + ''.join(chr(cp) for cp in _ZERO_WIDTH_CODEPOINTS) + ']')
# NO-BREAK SPACE — заменяем на обычный пробел, а не вырезаем: если он стоит
# между датой и временем (или другими токенами), простое удаление склеило
# бы их в один нераспознаваемый кусок.
NBSP = chr(0xA0)

# Порядок важен: пробуем более специфичные/частые форматы Google Sheets
# первыми. Дата уже прогнана через clean_invisible() к этому моменту.
CRM_DATE_FORMATS = (
    '%d.%m.%Y %H:%M:%S',
    '%d.%m.%Y %H:%M',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y %H:%M',
    '%Y/%m/%d %H:%M:%S',
    '%Y/%m/%d %H:%M',
    '%d.%m.%Y',
    '%Y-%m-%d',
)


def clean_invisible(text):
    """Убирает zero-width пробелы и BOM, заменяет неразрывный пробел на
    обычный — Google Sheets иногда вставляет их незаметно для глаза, из-за
    чего точное сравнение строк молча ломается (например, ячейка выглядит
    как "квал", а на самом деле содержит невидимый zero-width пробел)."""
    text = ZERO_WIDTH_CHARS_RE.sub('', text or '')
    text = text.replace(NBSP, ' ')
    return text.strip()


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
    """Разбирает дату из CRM-таблицы. Всегда сначала чистит невидимые
    символы и пробелы по краям — иначе строгий strptime молча промахивается
    мимо валидной даты. Никогда не придумывает дату: если ни один из
    поддерживаемых форматов не подошёл, возвращает None."""
    cleaned = clean_invisible(value)
    if not cleaned:
        return None
    for fmt in CRM_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace('Z', '+00:00'))
    except ValueError:
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


# Явный список вместо strftime('%B') — locale раннера GitHub Actions не
# гарантированно английский, а strftime('%B') отдаёт название месяца на
# языке текущей locale.
_MONTH_NAMES_EN = (
    'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
    'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER',
)


def month_bounds_str(reference_date):
    """Возвращает (начало, конец) месяца reference_date как строки в том же
    формате, в котором лежит created_at ('%Y-%m-%d %H:%M:%S') — сравнение
    строк для этого формата эквивалентно сравнению дат."""
    year, month = reference_date.year, reference_date.month
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


def build_data_quality(leads, csv_row_count, unparseable_rows, reference_date=None):
    """Считает диагностику по уже обработанным лидам (created_at — строка
    '%Y-%m-%d %H:%M:%S' или None). Чистая функция без side-effects — легко
    тестируется без сети и без записи файлов."""
    reference_date = reference_date or datetime.now()
    month_start, month_end = month_bounds_str(reference_date)

    qualified = [l for l in leads if l['qualified'] is True]
    not_qualified = [l for l in leads if l['qualified'] is False]
    pending = [l for l in leads if l['qualified'] is None]
    qualified_with_date = [l for l in qualified if l['created_at']]
    qualified_without_date = [l for l in qualified if not l['created_at']]

    deal_counts = {}
    for l in leads:
        deal_counts[l['deal_id']] = deal_counts.get(l['deal_id'], 0) + 1
    duplicate_deal_ids = sorted(d for d, cnt in deal_counts.items() if cnt > 1)

    dated = [l['created_at'] for l in leads if l['created_at']]
    qualified_dated = [l['created_at'] for l in qualified if l['created_at']]

    month_qualified = sorted(
        (l for l in qualified if l['created_at'] and month_start <= l['created_at'] < month_end),
        key=lambda l: l['created_at'],
    )

    return {
        "reference_month_label": f"{_MONTH_NAMES_EN[reference_date.month - 1]} {reference_date.year}",
        "csv_rows": csv_row_count,
        "crm_leads": len(leads),
        "qualified": len(qualified),
        "not_qualified": len(not_qualified),
        "pending": len(pending),
        "qualified_with_date": len(qualified_with_date),
        "qualified_without_date": len(qualified_without_date),
        "rows_with_unparseable_date": len(unparseable_rows),
        "duplicate_deal_ids": duplicate_deal_ids,
        "latest_created_at": max(dated) if dated else None,
        "latest_qualified_created_at": max(qualified_dated) if qualified_dated else None,
        "month_qualified": month_qualified,
        "qualified_without_date_leads": qualified_without_date,
        "unparseable_rows": unparseable_rows,
    }


def print_data_quality(dq):
    print()
    print("=== CRM DATA QUALITY ===")
    print(f"CSV rows: {dq['csv_rows']}")
    print(f"CRM leads: {dq['crm_leads']}")
    print(f"qualified: {dq['qualified']}")
    print(f"not qualified: {dq['not_qualified']}")
    print(f"pending: {dq['pending']}")
    print(f"qualified_with_date: {dq['qualified_with_date']}")
    print(f"qualified_without_date: {dq['qualified_without_date']}")
    print(f"rows_with_unparseable_date: {dq['rows_with_unparseable_date']}")
    print(f"duplicates: {len(dq['duplicate_deal_ids'])}")
    if dq['duplicate_deal_ids']:
        print(f"  duplicate deal_id(s): {', '.join(dq['duplicate_deal_ids'])}")
    print(f"latest_created_at: {dq['latest_created_at'] or 'N/A'}")
    print(f"latest_qualified_created_at: {dq['latest_qualified_created_at'] or 'N/A'}")

    print()
    header = f"{dq['reference_month_label']} QUALIFIED"
    print(header)
    print('-' * len(header))
    print(f"count = {len(dq['month_qualified'])}")
    if dq['month_qualified']:
        print("deal_id | created_at | qualified | campaign_id | adset_id | ad_id")
        for l in dq['month_qualified']:
            print(f"{l['deal_id']} | {l['created_at']} | {l['qualified']} | {l['campaign_id']} | {l['adset_id']} | {l['ad_id']}")

    print()
    print("QUALIFIED WITHOUT DATE")
    print("----------------------")
    if dq['qualified_without_date_leads']:
        print("deal_id | raw_date | created_at")
        for l in dq['qualified_without_date_leads']:
            print(f"{l['deal_id']} | {l.get('_raw_date', '')!r} | {l['created_at']}")
        print(
            f"WARNING: {dq['qualified_without_date']} qualified lead(s) have no parseable "
            "created_at and are excluded from every date-range calculation "
            "(month/custom range/all-time all filter by created_at)."
        )
    else:
        print("(none)")

    if dq['unparseable_rows']:
        print()
        print("ROWS WITH UNPARSEABLE DATE")
        print("--------------------------")
        for r in dq['unparseable_rows']:
            print(f"{r['deal_id']} | {r['raw_date']!r}")


def main():
    rows = fetch_csv(CSV_URL)

    if len(rows) < 2:
        print("CRM-таблица пустая или недоступна.")
        sys.exit(1)

    if len(rows[0]) < EXPECTED_HEADER_LEN:
        print(f"Структура CRM-таблицы изменилась: ожидалось {EXPECTED_HEADER_LEN} колонок, получено {len(rows[0])}.")
        sys.exit(1)

    leads = []
    suspicious_qual_values = set()
    unparseable_rows = []
    for row in rows[1:]:
        if len(row) < EXPECTED_HEADER_LEN:
            row = row + [''] * (EXPECTED_HEADER_LEN - len(row))

        deal_id = row[1].strip()
        if not deal_id:
            continue

        raw_date = clean_invisible(row[0])
        created_at = parse_crm_date(row[0])
        if raw_date and created_at is None:
            unparseable_rows.append({"deal_id": deal_id, "raw_date": raw_date})

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

        leads.append({
            "deal_id": deal_id,
            "created_at": created_at,
            "_raw_date": raw_date,
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

    dq = build_data_quality(leads, csv_row_count=len(rows) - 1, unparseable_rows=unparseable_rows)
    print_data_quality(dq)

    for lead in leads:
        del lead["_raw_date"]

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
        "data_quality": {
            "csv_rows": dq["csv_rows"],
            "crm_leads": dq["crm_leads"],
            "qualified": dq["qualified"],
            "not_qualified": dq["not_qualified"],
            "pending": dq["pending"],
            "qualified_with_date": dq["qualified_with_date"],
            "qualified_without_date": dq["qualified_without_date"],
            "rows_with_unparseable_date": dq["rows_with_unparseable_date"],
            "duplicate_deal_ids": dq["duplicate_deal_ids"],
            "latest_created_at": dq["latest_created_at"],
            "latest_qualified_created_at": dq["latest_qualified_created_at"],
            "unparseable_rows": dq["unparseable_rows"],
            "qualified_without_date_deal_ids": [l["deal_id"] for l in dq["qualified_without_date_leads"]],
        },
    }

    os.makedirs('data', exist_ok=True)
    crm_path = os.path.join('data', 'crm.json')
    crm_tmp_path = crm_path + '.tmp'
    with open(crm_tmp_path, 'w', encoding='utf-8') as f:
        json.dump(crm_data, f, ensure_ascii=False, indent=2)
    os.replace(crm_tmp_path, crm_path)

    print()
    print(f"CRM: {total} лидов, по ad_id {matched_ad}, по campaign_id {matched_campaign}, по телефонному мосту {matched_bridge}, приближённо {matched_fuzzy}, не сматчено {unmatched} ({match_rate}%).")
    if suspicious_qual_values:
        print(f"Подозрительные значения в колонке 'Квал' (содержат 'квал', но не равны точно 'квал'/'не квал'): {sorted(suspicious_qual_values)}")


if __name__ == '__main__':
    main()
