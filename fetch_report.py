import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta

PLAN_DAILY_BUDGET = 250
PLAN_MONTHLY_LEADS = 400
DAYS_IN_MONTH = 30
PLAN_MONTHLY_BUDGET = PLAN_DAILY_BUDGET * DAYS_IN_MONTH
TARGET_CPL = round(PLAN_MONTHLY_BUDGET / PLAN_MONTHLY_LEADS, 2)
TARGET_CPQL = 200
TARGET_QUAL_RATE = 30

# FETCH_SINCE зафиксирован и не сдвигается автоматически, поэтому объём
# исторического запроса растёт с каждым днём без ограничения. Полноценный
# incremental merge (дозапрос только новых/изменённых дней с объединением
# в существующий report.json) требует отдельного изменения архитектуры
# хранения данных и здесь сознательно не реализован — см. рекомендацию в PR.
FETCH_SINCE = '2026-03-09'
CHUNK_DAYS = 30
# Для adset/ad level=insights запросы значительно "тяжелее" (детализация по
# сущностям), Meta API на них стабильно отдаёт HTTP 400 code=2/subcode=1504044
# ("Service temporarily unavailable") при большом time_range. Уменьшаем окно
# только для этих двух уровней, остальная логика чанкинга не меняется.
GRANULAR_CHUNK_DAYS = 7
API_VERSION = 'v25.0'
LEAD_ACTION_TYPES = {'lead'}

MAX_RETRIES = 3
# code=2 / error_subcode=1504044 — известная нестабильность Meta на тяжёлых
# granular-запросах. Повторять его много раз бессмысленно (получим тот же
# ответ) — ограничиваем ретраи и полагаемся на fallback с уменьшением диапазона.
HEAVY_QUERY_ERROR = (2, 1504044)
HEAVY_QUERY_MAX_RETRIES = 2
MIN_CHUNK_DAYS = 1


class MetaAPIError(Exception):
    """Ошибка Meta API после исчерпания ретраев в рамках одного запроса."""

    def __init__(self, message, status_code=None, code=None, error_subcode=None, is_transient=None, retries=0):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.error_subcode = error_subcode
        self.is_transient = is_transient
        self.retries = retries

ACCESS_TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('FACEBOOK_ACT_ID')

if not ACCESS_TOKEN or not ACCOUNT_ID:
    print("Ошибка: не заданы FACEBOOK_ACCESS_TOKEN или FACEBOOK_ACT_ID")
    sys.exit(1)

if not ACCOUNT_ID.startswith('act_'):
    ACCOUNT_ID = f'act_{ACCOUNT_ID}'

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = FETCH_SINCE


def date_chunks(since_str, until_str, chunk_days):
    since = datetime.strptime(since_str, '%Y-%m-%d')
    until = datetime.strptime(until_str, '%Y-%m-%d')
    cur = since
    while cur <= until:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), until)
        yield cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')
        cur = chunk_end + timedelta(days=1)


def log_api_error(path, params, level, status_code, code, subcode, is_transient, retry_number, fallback=None):
    date_range = params.get('time_range')
    if date_range:
        try:
            tr = json.loads(date_range)
            date_range = f"{tr.get('since')}..{tr.get('until')}"
        except (TypeError, ValueError):
            pass
    print(
        "[meta-api-error] "
        f"endpoint={path} level={level or 'account'} date_range={date_range or '-'} "
        f"http_status={status_code} code={code} subcode={subcode} is_transient={is_transient} "
        f"retry={retry_number} fallback={fallback or '-'}"
    )


def _request_with_retry(url, params, path, level):
    """Один HTTP-запрос (с учётом пагинации URL уже содержит параметры) с
    умным ретраем: анализирует HTTP-статус, error.code, error_subcode и
    is_transient, чтобы не повторять бесконечно один и тот же тяжёлый запрос."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, params=params, timeout=120)
        except requests.exceptions.RequestException as e:
            if attempt >= MAX_RETRIES:
                raise MetaAPIError(f"Ошибка сети при запросе к {path}: {e}") from e
            time.sleep(2 ** attempt)
            continue

        if resp.status_code >= 500:
            if attempt >= MAX_RETRIES:
                raise MetaAPIError(
                    f"Meta API стабильно возвращает {resp.status_code} на {path}",
                    status_code=resp.status_code, retries=attempt,
                )
            time.sleep(2 ** attempt)
            continue

        if resp.status_code >= 400:
            error = {}
            try:
                error = resp.json().get('error', {}) or {}
            except ValueError:
                pass
            code = error.get('code')
            subcode = error.get('error_subcode')
            is_transient = bool(error.get('is_transient'))
            is_heavy_query_error = (code, subcode) == HEAVY_QUERY_ERROR
            retry_limit = HEAVY_QUERY_MAX_RETRIES if is_heavy_query_error else MAX_RETRIES
            retryable = is_transient or is_heavy_query_error
            exhausted = (not retryable) or attempt >= retry_limit
            log_api_error(
                path, params, level, resp.status_code, code, subcode, is_transient, attempt,
                fallback='raise-to-caller' if exhausted else None,
            )
            if exhausted:
                raise MetaAPIError(
                    error.get('message') or f"Meta API вернул {resp.status_code} на {path}",
                    status_code=resp.status_code, code=code, error_subcode=subcode,
                    is_transient=is_transient, retries=attempt,
                )
            time.sleep((5 if is_heavy_query_error else 30) * attempt)
            continue

        return resp.json()


def api_get(path, params, level=None):
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    call_params = {**params, 'access_token': ACCESS_TOKEN}
    all_data = []

    while url:
        payload = _request_with_retry(url, call_params, path, level)
        if 'error' in payload:
            error = payload['error'] or {}
            code = error.get('code')
            subcode = error.get('error_subcode')
            is_transient = bool(error.get('is_transient'))
            log_api_error(path, call_params, level, 200, code, subcode, is_transient, 1, fallback='raise-to-caller')
            raise MetaAPIError(
                error.get('message', 'Unknown Meta API error'),
                code=code, error_subcode=subcode, is_transient=is_transient,
            )

        all_data.extend(payload.get('data', []))
        url = payload.get('paging', {}).get('next')
        call_params = {}

    return all_data


def split_range(since_str, until_str):
    """Делит диапазон дат пополам. Возвращает None, если диапазон уже
    состоит из одного дня и дальше делить некуда."""
    since = datetime.strptime(since_str, '%Y-%m-%d')
    until = datetime.strptime(until_str, '%Y-%m-%d')
    total_days = (until - since).days + 1
    if total_days <= MIN_CHUNK_DAYS:
        return None
    mid = since + timedelta(days=total_days // 2 - 1)
    if mid < since:
        mid = since
    first = (since.strftime('%Y-%m-%d'), mid.strftime('%Y-%m-%d'))
    second = ((mid + timedelta(days=1)).strftime('%Y-%m-%d'), until.strftime('%Y-%m-%d'))
    return first, second


def fetch_range_resilient(path, base_params, since, until, level, failed_chunks):
    """Забирает один диапазон дат. При повторяющемся code=2/1504044 (или
    любой другой transient-ошибке) автоматически уменьшает диапазон вдвое и
    пробует снова, вместо того чтобы бесконечно повторять тот же запрос.
    Если даже минимальный чанк (1 день) не удаётся получить — не роняет
    pipeline, а запоминает проблемный chunk и продолжает с остальными."""
    params = {**base_params, 'time_range': json.dumps({'since': since, 'until': until})}
    try:
        return api_get(path, params, level=level)
    except MetaAPIError as e:
        is_heavy_query_error = (e.code, e.error_subcode) == HEAVY_QUERY_ERROR
        split = split_range(since, until) if (is_heavy_query_error or e.is_transient) else None
        if split:
            first, second = split
            print(
                f"[meta-api-fallback] endpoint={path} level={level or 'account'} "
                f"date_range={since}..{until} code={e.code} subcode={e.error_subcode} "
                f"is_transient={e.is_transient} fallback=split-range->{first[0]}..{first[1]},{second[0]}..{second[1]}"
            )
            first_data = fetch_range_resilient(path, base_params, first[0], first[1], level, failed_chunks)
            second_data = fetch_range_resilient(path, base_params, second[0], second[1], level, failed_chunks)
            return first_data + second_data

        print(
            f"[meta-api-fallback] endpoint={path} level={level or 'account'} date_range={since}..{until} "
            f"code={e.code} subcode={e.error_subcode} is_transient={e.is_transient} "
            "fallback=give-up-on-chunk (данные за этот диапазон будут отсутствовать в отчёте)"
        )
        failed_chunks.append({
            "endpoint": path,
            "level": level or "account",
            "since": since,
            "until": until,
            "http_status": e.status_code,
            "code": e.code,
            "error_subcode": e.error_subcode,
            "is_transient": e.is_transient,
            "message": str(e),
        })
        return []


def api_get_chunked(path, base_params, since, until, chunk_days=CHUNK_DAYS, level=None, failed_chunks=None):
    all_data = []
    for chunk_since, chunk_until in date_chunks(since, until, chunk_days):
        if failed_chunks is not None:
            all_data.extend(fetch_range_resilient(path, base_params, chunk_since, chunk_until, level, failed_chunks))
        else:
            params = {**base_params, 'time_range': json.dumps({'since': chunk_since, 'until': chunk_until})}
            all_data.extend(api_get(path, params, level=level))
    return all_data


def safe_api_get(path, params, level, description, failed_sections, default):
    """Для некритичных вспомогательных запросов (мета-данные кампаний/объявлений,
    охват): при окончательном сбое не роняет весь pipeline, а возвращает
    значение по умолчанию и помечает секцию как неполную в отчёте."""
    try:
        return api_get(path, params, level=level)
    except MetaAPIError as e:
        print(f"[meta-api-section-failed] section={description} endpoint={path} code={e.code} subcode={e.error_subcode}: {e}")
        failed_sections.append({
            "section": description,
            "endpoint": path,
            "http_status": e.status_code,
            "code": e.code,
            "error_subcode": e.error_subcode,
            "is_transient": e.is_transient,
            "message": str(e),
        })
        return default


def count_leads(actions):
    total = 0
    for action in actions or []:
        if action.get('action_type') in LEAD_ACTION_TYPES:
            total += int(action.get('value', 0))
    return total


def parse_language(name):
    for p in (name or '').split('_'):
        upper = p.upper()
        if upper in ('EN', 'ENG', 'ENGLISH'):
            return 'EN'
        if upper in ('RU', 'RUS', 'RUSSIAN'):
            return 'RU'
    return 'unknown'


def day_metrics(raw):
    spend = float(raw.get('spend', 0))
    leads = count_leads(raw.get('actions'))
    clicks = int(raw.get('clicks', 0))
    link_clicks = int(raw.get('inline_link_clicks', 0))
    impressions = int(raw.get('impressions', 0))
    return spend, leads, clicks, link_clicks, impressions


def dedup_by_date(raw_rows):
    by_date = {}
    for r in raw_rows:
        by_date[r['date_start']] = r
    result = []
    for date, r in sorted(by_date.items()):
        spend, leads, clicks, link_clicks, impressions = day_metrics(r)
        result.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
    return result


def dedup_by_entity_date(raw_rows, id_field, name_field, parent_fields=None):
    by_id = {}
    for r in raw_rows:
        entity_id = r.get(id_field)
        entry = by_id.setdefault(entity_id, {
            "id": entity_id,
            "name": r.get(name_field, ''),
            "parents": {pf: r.get(pf) for pf in (parent_fields or [])},
            "daily_by_date": {},
        })
        entry["daily_by_date"][r['date_start']] = r

    entities = []
    for entity_id, entry in by_id.items():
        daily = []
        for date, r in sorted(entry["daily_by_date"].items()):
            spend, leads, clicks, link_clicks, impressions = day_metrics(r)
            daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
        entity = {"id": entity_id, "name": entry["name"], "daily": daily}
        entity.update(entry["parents"])
        entities.append(entity)
    return entities


# Проблемные chunks (окончательно не удалось получить после fallback) и
# некритичные секции, упавшие целиком, — попадают в data_quality отчёта,
# но не прерывают сбор остальных данных.
failed_chunks = []
failed_sections = []

account_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date, level='account', failed_chunks=failed_chunks)

account_daily = dedup_by_date(account_raw)

campaigns_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date, level='campaign', failed_chunks=failed_chunks)
campaigns = dedup_by_entity_date(campaigns_raw, 'campaign_id', 'campaign_name')

campaigns_meta_raw = safe_api_get(f"{ACCOUNT_ID}/campaigns", {
    'fields': 'id,effective_status',
    'limit': 500,
}, 'campaigns_meta', 'campaigns_meta', failed_sections, [])
status_by_campaign_id = {c['id']: c.get('effective_status') for c in campaigns_meta_raw}

for c in campaigns:
    c["language"] = parse_language(c["name"])
    c["status"] = status_by_campaign_id.get(c["id"], 'UNKNOWN')

print("Язык и статус по кампаниям:")
for c in campaigns:
    print(f"  [{c['language']}] [{c['status']}] {c['name']}")

adsets_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'adset',
    'fields': 'adset_id,adset_name,campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date, chunk_days=GRANULAR_CHUNK_DAYS, level='adset', failed_chunks=failed_chunks)
adsets = dedup_by_entity_date(adsets_raw, 'adset_id', 'adset_name', parent_fields=['campaign_id'])

ads_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'ad',
    'fields': 'ad_id,ad_name,adset_id,campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date, chunk_days=GRANULAR_CHUNK_DAYS, level='ad', failed_chunks=failed_chunks)
creatives = dedup_by_entity_date(ads_raw, 'ad_id', 'ad_name', parent_fields=['adset_id', 'campaign_id'])

ads_meta_raw = safe_api_get(f"{ACCOUNT_ID}/ads", {
    'fields': 'id,creative.thumbnail_width(720).thumbnail_height(720){thumbnail_url}',
    'limit': 25,
}, 'ads_meta', 'ads_meta', failed_sections, [])
thumb_by_ad_id = {a['id']: a.get('creative', {}).get('thumbnail_url') for a in ads_meta_raw}
for c in creatives:
    c["thumbnail_url"] = thumb_by_ad_id.get(c["id"])

age_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'breakdowns': 'age,gender',
    'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date, level='age_gender', failed_chunks=failed_chunks)

demo_by_bucket = {}
for r in age_raw:
    bucket = (r.get('age', 'unknown'), r.get('gender', 'unknown'))
    entry = demo_by_bucket.setdefault(bucket, {})
    entry[r['date_start']] = r

age_groups = []
for (age, gender), by_date in demo_by_bucket.items():
    daily = []
    for date, r in sorted(by_date.items()):
        spend, leads, clicks, link_clicks, impressions = day_metrics(r)
        daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
    age_groups.append({"age": age, "gender": gender, "daily": daily})

device_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'breakdowns': 'impression_device',
    'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date, level='device', failed_chunks=failed_chunks)

device_by_bucket = {}
for r in device_raw:
    bucket = r.get('impression_device', 'unknown')
    entry = device_by_bucket.setdefault(bucket, {})
    entry[r['date_start']] = r

devices = []
for device, by_date in device_by_bucket.items():
    daily = []
    for date, r in sorted(by_date.items()):
        spend, leads, clicks, link_clicks, impressions = day_metrics(r)
        daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
    devices.append({"device": device, "daily": daily})

def fetch_reach(since, until):
    raw = safe_api_get(f"{ACCOUNT_ID}/insights", {
        'time_range': json.dumps({'since': since, 'until': until}),
        'fields': 'reach',
        'limit': 1,
    }, 'reach', f'reach {since}..{until}', failed_sections, [])
    return int(raw[0].get('reach', 0)) if raw else 0


month_start = end_date[:8] + '01'
reach_by_preset = {
    '7d': fetch_reach((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=6)).strftime('%Y-%m-%d'), end_date),
    '14d': fetch_reach((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=13)).strftime('%Y-%m-%d'), end_date),
    '30d': fetch_reach((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=29)).strftime('%Y-%m-%d'), end_date),
    'month': fetch_reach(month_start, end_date),
    'all': fetch_reach(start_date, end_date),
}

report_data = {
    "last_updated": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "fetched_range": {"since": start_date, "until": end_date},
    "plan": {
        "monthly_budget": PLAN_MONTHLY_BUDGET,
        "monthly_leads": PLAN_MONTHLY_LEADS,
        "target_cpl": TARGET_CPL,
        "target_cpql": TARGET_CPQL,
        "target_qual_rate": TARGET_QUAL_RATE,
    },
    "account_daily": account_daily,
    "campaigns": campaigns,
    "adsets": adsets,
    "creatives": creatives,
    "age_groups": age_groups,
    "devices": devices,
    "reach_by_preset": reach_by_preset,
    "data_quality": {
        "partial": bool(failed_chunks) or bool(failed_sections),
        "failed_chunks": failed_chunks,
        "failed_sections": failed_sections,
    },
}

os.makedirs('data', exist_ok=True)
report_path = os.path.join('data', 'report.json')
report_tmp_path = report_path + '.tmp'
with open(report_tmp_path, 'w', encoding='utf-8') as f:
    json.dump(report_data, f, ensure_ascii=False, indent=2)
# Атомарная замена: report.json либо остаётся прежним, либо полностью
# заменяется новым файлом — никогда не бывает наполовину записан.
os.replace(report_tmp_path, report_path)

total_spend = sum(d['spend'] for d in account_daily)
total_leads = sum(d['leads'] for d in account_daily)
total_clicks = sum(d['clicks'] for d in account_daily)
total_link_clicks = sum(d['link_clicks'] for d in account_daily)
total_impressions = sum(d['impressions'] for d in account_daily)

print(f"Готово: {len(account_daily)} дней, {len(campaigns)} кампаний, {len(adsets)} аудиторий, {len(creatives)} объявлений, {len(age_groups)} демо-групп, {len(devices)} устройств.")
print(f"Итого за период {start_date} — {end_date}: расход ${total_spend:.2f}, лиды {total_leads}, клики {total_clicks}, клики по ссылке {total_link_clicks}, показы {total_impressions}.")
print(f"Охват по периодам: {reach_by_preset}")

if failed_chunks or failed_sections:
    print(
        f"ВНИМАНИЕ: отчёт частичный (partial). Проблемных chunks: {len(failed_chunks)}, "
        f"проблемных секций: {len(failed_sections)}. Подробности см. data_quality в report.json."
    )
    for fc in failed_chunks:
        print(f"  - chunk: endpoint={fc['endpoint']} level={fc['level']} range={fc['since']}..{fc['until']} code={fc['code']}/{fc['error_subcode']}")
    for fs in failed_sections:
        print(f"  - секция: {fs['section']} code={fs['code']}/{fs['error_subcode']}: {fs['message']}")
else:
    print("Отчёт собран полностью, без частичных сбоев.")
