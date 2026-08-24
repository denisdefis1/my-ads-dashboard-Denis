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
    '1kZz8hsXcrRKUkGibIcJd-fhrVyD_TOD2gh5VEExGIjs',  # eng лиды lagoon
    '1IgZLp169K4XvjmjZUpl_BeOjDUBuKacShitcgHMdSkk',  # лиды лагун 7
]

# Предположение: "Дата создания" в CRM записана в тбилисском времени (UTC+4).
# Если совпадения будут систематически промахиваться на фиксированное число
# часов — значит это предположение неверное, поправить тут одну цифру.
# Точное время в разных источниках не согласовано (проверено вручную —
# расхождение может быть нефиксированным, не просто разница часовых поясов).
# Поэтому матчим только по имени, без временного окна. Порог выше, чем был
# бы при опоре на время, чтобы не путать разных людей с похожими именами.
MATCH_NAME_MIN_RATIO = 0.72


INVISIBLE_CHARS_RE = re.compile(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\xa0]')


def clean_invisible(text):
    """Убирает zero-width пробелы, BOM и неразрывный пробел — Google Sheets
    иногда вставляет их незаметно для глаза, из-за чего точное сравнение строк
    молча ломается (например, ячейка выглядит как "квал", а на самом деле
    содержит "квал\u200b")."""
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


# Раньше дата парсилась только одним жёстким форматом "%d.%m.%Y %H:%M:%S".
# Оказалось, что часть строк в CRM-таблице (судя по всему — недавно
# добавленные) записаны в столбце "Дата создания" чуть иначе: без секунд,
# в ISO-формате, с двузначным годом и т.п. Раньше при несовпадении формата
# parse_crm_date молча возвращала None, и такая сделка получала
# created_at = null — а дашборд фильтрует лиды по датам (crmLeadsInRange),
# поэтому лид с null-датой пропадал из АБСОЛЮТНО ЛЮБОГО периода на
# дашборде (сегодня/7 дней/30 дней/всё время — везде), даже если он есть
# в data/crm.json и даже если у него честно проставлен квал. Именно так
# терялась часть свежих квал-лидов. Теперь пробуем несколько форматов.
CRM_DATE_FORMATS = (
    '%d.%m.%Y %H:%M:%S',
    '%d.%m.%Y %H:%M',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%d.%m.%y %H:%M:%S',
    '%d.%m.%y %H:%M',
    '%m/%d/%Y %H:%M:%S',
    '%m/%d/%Y %H:%M',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y %H:%M',
)


def parse_crm_date(value):
    value = clean_invisible(value)
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


rows = fetch_csv(CSV_URL)

if len(rows) < 2:
    print("CRM-таблица пустая или недоступна.")
    sys.exit(1)

if len(rows[0]) < EXPECTED_HEADER_LEN:
    print(f"Структура CRM-таблицы изменилась: ожидалось {EXPECTED_HEADER_LEN} колонок, получено {len(rows[0])}.")
    sys.exit(1)

leads = []
suspicious_qual_values = set()
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
    # Раньше статус ставился только при точном совпадении со строкой "квал"/
    # "не квал" — любая другая формулировка в этой колонке (пробел, точка,
    # пометка в скобках, лишнее слово и т.п.) молча уходила в "неизвестно" и не
    # попадала в подсчёт квал-лидов. Это и было причиной заниженного счётчика
    # на дашборде: диагностика (.github/workflows/diagnose-crm.yml) на реальных
    # данных нашла 146 фактических квалов против 25 распознанных точным
    # сравнением. Теперь распознаём по вхождению "квал" в ячейке (со снятыми
    # невидимыми символами и обрезанной пунктуацией по краям), а "не квал"/
    # "нет квал" и т.п. по-прежнему считаем отрицательным статусом.
    qual_raw = clean_invisible(row[14]).lower().strip(' .,!?:;-–—')

    if qual_raw and qual_raw not in (QUAL_VALUE, NOT_QUAL_VALUE):
        # Любое нестандартное непустое значение в колонке "Квал" — логируем,
        # чтобы видеть в выводе экшена, какие ещё формулировки использует
        # отдел продаж, и при необходимости расширять классификатор дальше.
        suspicious_qual_values.add(repr(row[14]))

    if not qual_raw:
        qualified = None  # пустая ячейка — сделка ещё не размечена, это не баг
    elif qual_raw in (QUAL_VALUE, NOT_QUAL_VALUE):
        qualified = qual_raw == QUAL_VALUE
    elif re.search(r'^(не|нет)\b.*квал', qual_raw) or re.search(r'\bне\s*квал', qual_raw):
        qualified = False
    elif 'квал' in qual_raw:
        qualified = True
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

    # Короткие/распространённые имена (Alex, Victor, Артем и т.п.) с ростом
    # числа lead-form таблиц стали встречаться в нескольких из них в разные
    # месяцы — раньше это делало совпадение "неоднозначным" (ambiguous) и
    # честный матч отбрасывался, хотя на самом деле это разные люди, просто
    # с похожими именами. Дата почти всегда решает эту неоднозначность: сузим
    # кандидатов до тех, что в пределах пары дней от даты сделки в CRM (запас
    # на несогласованность часовых поясов между CRM и лидформами — см.
    # комментарий у MATCH_NAME_MIN_RATIO), и только если в этом окне вообще
    # никого нет — откатываемся к сравнению по всей истории, как раньше.
    MATCH_DATE_WINDOW_DAYS = 2
    candidates = lead_form_entries
    if lead["created_at"]:
        windowed = [
            entry for entry in lead_form_entries
            if entry["created_at"] and abs((entry["created_at"].replace(tzinfo=None) - lead["created_at"]).days) <= MATCH_DATE_WINDOW_DAYS
        ]
        if windowed:
            candidates = windowed

    best = None
    best_score = 0.0
    second_score = 0.0
    for entry in candidates:
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
crm_path = os.path.join('data', 'crm.json')
crm_tmp_path = crm_path + '.tmp'
with open(crm_tmp_path, 'w', encoding='utf-8') as f:
    json.dump(crm_data, f, ensure_ascii=False, indent=2)
os.replace(crm_tmp_path, crm_path)

print(f"CRM: {total} лидов, по ad_id {matched_ad}, по campaign_id {matched_campaign}, по телефонному мосту {matched_bridge}, приближённо {matched_fuzzy}, не сматчено {unmatched} ({match_rate}%).")
if suspicious_qual_values:
    print(f"Нестандартные значения в колонке 'Квал' (не пустые и не равны точно 'квал'/'не квал', классифицированы по вхождению 'квал'/'не квал'): {sorted(suspicious_qual_values)}")
