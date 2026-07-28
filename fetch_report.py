import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta

# ============================================================
# НАСТРОЙКИ ПЛАНА — меняешь тут руками, из Meta API это не тянется
# ============================================================
PLAN_DAILY_BUDGET = 250
PLAN_MONTHLY_LEADS = 400
DAYS_IN_MONTH = 30
PLAN_MONTHLY_BUDGET = PLAN_DAILY_BUDGET * DAYS_IN_MONTH
TARGET_CPL = round(PLAN_MONTHLY_BUDGET / PLAN_MONTHLY_LEADS, 2)

# Сколько дней истории тянуть за раз. Фронтенд сам режет это на 7/14/30/
# месяц/произвольный период — глубже, чем FETCH_DAYS, выбрать будет нельзя.
# 270 дней ≈ 9 месяцев. Учти: чем больше диапазон, тем дольше отрабатывает
# workflow и тем тяжелее получается data/report.json (но для статики это
# всё равно не проблема — счёт на сотни КБ – единицы МБ).
FETCH_DAYS = 270

# Типы конверсий, которые считаем лидами.
# Если Pixel lead и Lead Ads форма стреляют одновременно на одно и то же
# событие — сверь в Events Manager, что это не задваивает счётчик.
LEAD_ACTION_TYPES = {'lead', 'offsite_conversion.fb_pixel_lead'}

API_VERSION = 'v25.0'

ACCESS_TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('FACEBOOK_ACT_ID')

if not ACCESS_TOKEN or not ACCOUNT_ID:
    print("Ошибка: не заданы FACEBOOK_ACCESS_TOKEN или FACEBOOK_ACT_ID")
    sys.exit(1)

if not ACCOUNT_ID.startswith('act_'):
    ACCOUNT_ID = f'act_{ACCOUNT_ID}'

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = (datetime.now() - timedelta(days=FETCH_DAYS)).strftime('%Y-%m-%d')

# Размер одного куска запроса в днях. На больших диапазонах с разбивкой по
# дням (time_increment=1) на уровне campaign/ad Meta иногда отдаёт
# 500 Internal Server Error — запрос слишком тяжёлый для одного вызова.
# Поэтому режем весь период на куски и делаем несколько запросов подряд.
CHUNK_DAYS = 30


def date_chunks(since_str, until_str, chunk_days):
    since = datetime.strptime(since_str, '%Y-%m-%d')
    until = datetime.strptime(until_str, '%Y-%m-%d')
    cur = since
    while cur <= until:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), until)
        yield cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')
        cur = chunk_end + timedelta(days=1)


def api_get(path, params):
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    params = {**params, 'access_token': ACCESS_TOKEN}
    all_data = []

    while url:
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=120)
                if resp.status_code >= 500:
                    print(f"Meta API {resp.status_code} на {path}, попытка {attempt + 1}/3, повтор через {2 ** attempt}с...")
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    print(f"Ошибка запроса к {path}: {e}")
                    sys.exit(1)
                time.sleep(2 ** attempt)
        else:
            print(f"Meta API стабильно возвращает 5xx на {path} после 3 попыток.")
            sys.exit(1)

        payload = resp.json()
        if 'error' in payload:
            print(f"Meta API вернул ошибку на {path}: {payload['error']}")
            sys.exit(1)

        all_data.extend(payload.get('data', []))
        url = payload.get('paging', {}).get('next')
        params = {}

    return all_data


def api_get_chunked(path, base_params, since, until):
    """Тянет insights кусками по CHUNK_DAYS, чтобы не словить 500 на большом диапазоне."""
    all_data = []
    for chunk_since, chunk_until in date_chunks(since, until, CHUNK_DAYS):
        params = {**base_params, 'time_range': json.dumps({'since': chunk_since, 'until': chunk_until})}
        all_data.extend(api_get(path, params))
    return all_data


def count_leads(actions):
    total = 0
    for action in actions or []:
        if action.get('action_type') in LEAD_ACTION_TYPES:
            total += int(action.get('value', 0))
    return total


def parse_language(name):
    for p in (name or '').split('_'):
        if p.upper() in ('EN', 'RU'):
            return p.upper()
    return 'RU'


def day_row(raw):
    spend = float(raw.get('spend', 0))
    leads = count_leads(raw.get('actions'))
    clicks = int(raw.get('clicks', 0))
    impressions = int(raw.get('impressions', 0))
    return {
        "date": raw['date_start'],
        "spend": round(spend, 2),
        "leads": leads,
        "clicks": clicks,
        "impressions": impressions,
    }


# ============================================================
# 1. Аккаунт — по дням
# ============================================================
account_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'fields': 'spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
account_daily = sorted((day_row(r) for r in account_raw), key=lambda d: d['date'])

# ============================================================
# 2. Кампании — по дням, сгруппировано по campaign_id
# ============================================================
campaigns_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

campaigns_by_id = {}
for r in campaigns_raw:
    cid = r.get('campaign_id')
    entry = campaigns_by_id.setdefault(cid, {
        "id": cid,
        "name": r.get('campaign_name', ''),
        "language": parse_language(r.get('campaign_name')),
        "daily": [],
    })
    entry["daily"].append(day_row(r))

campaigns = list(campaigns_by_id.values())
for c in campaigns:
    c["daily"].sort(key=lambda d: d['date'])

# ============================================================
# 3. Объявления — по дням, сгруппировано по ad_id + превью картинок
# ============================================================
ads_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'ad',
    'fields': 'ad_id,ad_name,spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

ads_by_id = {}
for r in ads_raw:
    aid = r.get('ad_id')
    entry = ads_by_id.setdefault(aid, {
        "id": aid,
        "name": r.get('ad_name', ''),
        "thumbnail_url": None,
        "daily": [],
    })
    entry["daily"].append(day_row(r))

ads_meta_raw = api_get(f"{ACCOUNT_ID}/ads", {
    'fields': 'id,creative{thumbnail_url}',
    'limit': 500,
})
thumb_by_ad_id = {a['id']: a.get('creative', {}).get('thumbnail_url') for a in ads_meta_raw}

creatives = list(ads_by_id.values())
for c in creatives:
    c["thumbnail_url"] = thumb_by_ad_id.get(c["id"])
    c["daily"].sort(key=lambda d: d['date'])

# ============================================================
# Итоговый файл
# ============================================================
report_data = {
    "last_updated": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "fetched_range": {"since": start_date, "until": end_date},
    "plan": {
        "monthly_budget": PLAN_MONTHLY_BUDGET,
        "monthly_leads": PLAN_MONTHLY_LEADS,
        "target_cpl": TARGET_CPL,
    },
    "account_daily": account_daily,
    "campaigns": campaigns,
    "creatives": creatives,
}

os.makedirs('data', exist_ok=True)
with open('data/report.json', 'w', encoding='utf-8') as f:
    json.dump(report_data, f, ensure_ascii=False, indent=2)

print(f"Готово: {len(account_daily)} дней аккаунта, {len(campaigns)} кампаний, {len(creatives)} объявлений.")
