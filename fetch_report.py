import os
import sys
import json
import requests
from datetime import datetime, timedelta

PLAN_DAILY_BUDGET = 250          # $ в день
PLAN_MONTHLY_LEADS = 400         # план по лидам на месяц
DAYS_IN_MONTH = 30
PLAN_MONTHLY_BUDGET = PLAN_DAILY_BUDGET * DAYS_IN_MONTH
TARGET_CPL = round(PLAN_MONTHLY_BUDGET / PLAN_MONTHLY_LEADS, 2)

# Типы конверсий, которые считаем лидами.
# Если у тебя одновременно работают Pixel lead и Lead Ads на одну и ту же
# аудиторию — проверь в Events Manager, что это не задваивает счётчик.
LEAD_ACTION_TYPES = {'lead', 'offsite_conversion.fb_pixel_lead'}

API_VERSION = 'v25.0'

# ============================================================
# Ключи из секретов GitHub Actions
# ============================================================
ACCESS_TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('FACEBOOK_ACT_ID')

if not ACCESS_TOKEN or not ACCOUNT_ID:
    print("Ошибка: не заданы FACEBOOK_ACCESS_TOKEN или FACEBOOK_ACT_ID")
    sys.exit(1)

if not ACCOUNT_ID.startswith('act_'):
    ACCOUNT_ID = f'act_{ACCOUNT_ID}'

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
TIME_RANGE = json.dumps({'since': start_date, 'until': end_date})


def api_get(path, params):
    """GET к Graph API с обработкой ошибок и постраничной подгрузкой."""
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    params = {**params, 'access_token': ACCESS_TOKEN}
    all_data = []

    while url:
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Ошибка запроса к {path}: {e}")
            sys.exit(1)

        if 'error' in payload:
            print(f"Meta API вернул ошибку на {path}: {payload['error']}")
            sys.exit(1)

        all_data.extend(payload.get('data', []))

        # переход на следующую страницу, если есть
        next_url = payload.get('paging', {}).get('next')
        url = next_url
        params = {}  # next_url уже содержит все параметры

    return all_data


def count_leads(actions):
    total = 0
    for action in actions or []:
        if action.get('action_type') in LEAD_ACTION_TYPES:
            total += int(action.get('value', 0))
    return total


def parse_language(name):
    """Ищет EN/RU как отдельный сегмент в имени кампании вида 006_EN_EU_..."""
    parts = (name or '').split('_')
    for p in parts:
        if p.upper() in ('EN', 'RU'):
            return p.upper()
    return 'RU'


# ============================================================
# 1. Статистика по дням (для графиков)
# ============================================================
daily_raw = api_get(f"{ACCOUNT_ID}/insights", {
    'time_range': TIME_RANGE,
    'time_increment': 1,
    'fields': 'spend,clicks,impressions,actions',
    'limit': 100,
})

daily = []
for day in sorted(daily_raw, key=lambda x: x['date_start']):
    spend = float(day.get('spend', 0))
    leads = count_leads(day.get('actions'))
    clicks = int(day.get('clicks', 0))
    impressions = int(day.get('impressions', 0))
    daily.append({
        "date": day['date_start'],
        "spend": spend,
        "leads": leads,
        "clicks": clicks,
        "impressions": impressions,
        "cpl": round(spend / leads, 2) if leads else None,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0,
    })

# ============================================================
# 2. Статистика по кампаниям
# ============================================================
campaigns_raw = api_get(f"{ACCOUNT_ID}/insights", {
    'time_range': TIME_RANGE,
    'level': 'campaign',
    'fields': 'campaign_name,spend,clicks,impressions,actions',
    'limit': 100,
})

campaigns = []
for c in campaigns_raw:
    spend = float(c.get('spend', 0))
    leads = count_leads(c.get('actions'))
    clicks = int(c.get('clicks', 0))
    impressions = int(c.get('impressions', 0))
    campaigns.append({
        "name": c.get('campaign_name', ''),
        "language": parse_language(c.get('campaign_name')),
        "spend": spend,
        "leads": leads,
        "clicks": clicks,
        "impressions": impressions,
        "cpl": round(spend / leads, 2) if leads else None,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0,
    })
campaigns.sort(key=lambda x: x['spend'], reverse=True)

# ============================================================
# 3. Статистика по объявлениям (креативам)
# ============================================================
ads_insights_raw = api_get(f"{ACCOUNT_ID}/insights", {
    'time_range': TIME_RANGE,
    'level': 'ad',
    'fields': 'ad_id,ad_name,spend,clicks,impressions,actions',
    'limit': 200,
})

# превью картинки тянем отдельным запросом и мёрджим по ad_id
ads_meta_raw = api_get(f"{ACCOUNT_ID}/ads", {
    'fields': 'id,creative{thumbnail_url}',
    'limit': 200,
})
thumb_by_ad_id = {
    a['id']: a.get('creative', {}).get('thumbnail_url')
    for a in ads_meta_raw
}

creatives = []
for a in ads_insights_raw:
    spend = float(a.get('spend', 0))
    leads = count_leads(a.get('actions'))
    clicks = int(a.get('clicks', 0))
    impressions = int(a.get('impressions', 0))
    creatives.append({
        "name": a.get('ad_name', ''),
        "thumbnail_url": thumb_by_ad_id.get(a.get('ad_id')),
        "spend": spend,
        "leads": leads,
        "clicks": clicks,
        "impressions": impressions,
        "cpl": round(spend / leads, 2) if leads else None,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0,
    })
creatives.sort(key=lambda x: x['leads'], reverse=True)

# ============================================================
# 4. Итоги, воронка, план, потоки EN/RU
# ============================================================
total_spend = sum(d['spend'] for d in daily)
total_leads = sum(d['leads'] for d in daily)
total_clicks = sum(d['clicks'] for d in daily)
total_impressions = sum(d['impressions'] for d in daily)

flows = {}
for c in campaigns:
    f = flows.setdefault(c['language'], {'spend': 0, 'leads': 0, 'clicks': 0, 'impressions': 0})
    f['spend'] += c['spend']
    f['leads'] += c['leads']
    f['clicks'] += c['clicks']
    f['impressions'] += c['impressions']

flows_list = []
for lang, f in flows.items():
    flows_list.append({
        "language": lang,
        "spend": round(f['spend'], 2),
        "share": round(f['spend'] / total_spend * 100, 1) if total_spend else 0,
        "leads": f['leads'],
        "cpl": round(f['spend'] / f['leads'], 2) if f['leads'] else None,
        "ctr": round(f['clicks'] / f['impressions'] * 100, 2) if f['impressions'] else 0,
    })

report_data = {
    "last_updated": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "period": {"since": start_date, "until": end_date},
    "plan": {
        "monthly_budget": PLAN_MONTHLY_BUDGET,
        "monthly_leads": PLAN_MONTHLY_LEADS,
        "target_cpl": TARGET_CPL,
    },
    "totals": {
        "spend": round(total_spend, 2),
        "leads": total_leads,
        "clicks": total_clicks,
        "impressions": total_impressions,
        "cpl": round(total_spend / total_leads, 2) if total_leads else None,
        "cpm": round(total_spend / total_impressions * 1000, 2) if total_impressions else None,
        "cpc": round(total_spend / total_clicks, 2) if total_clicks else None,
        "ctr": round(total_clicks / total_impressions * 100, 2) if total_impressions else 0,
    },
    "daily": daily,
    "campaigns": campaigns,
    "creatives": creatives,
    "flows": flows_list,
}

os.makedirs('data', exist_ok=True)
with open('data/report.json', 'w', encoding='utf-8') as f:
    json.dump(report_data, f, ensure_ascii=False, indent=4)

print(f"Готово: {len(daily)} дней, {len(campaigns)} кампаний, {len(creatives)} объявлений.")
