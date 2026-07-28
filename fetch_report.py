import os
import json
import requests
from datetime import datetime

ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
AD_ACCOUNT_ID = os.getenv("FB_AD_ACCOUNT_ID")

if not ACCESS_TOKEN or not AD_ACCOUNT_ID:
    raise ValueError("Ошибка: Проверьте FB_ACCESS_TOKEN и FB_AD_ACCOUNT_ID в GitHub Secrets!")

if not AD_ACCOUNT_ID.startswith('act_'):
    AD_ACCOUNT_ID = f"act_{AD_ACCOUNT_ID}"

url = f"https://facebook.com{AD_ACCOUNT_ID}/insights"

# Запрашиваем данные с 9 марта 2026 по сегодняшний день с разбивкой по дням
today_str = datetime.now().strftime('%Y-%m-%d')
time_range = {'since': '2026-03-09', 'until': today_str}

params = {
    'access_token': ACCESS_TOKEN,
    'level': 'campaign',
    # Добавляем date_start и date_stop в ответ, чтобы знать, к какому дню относятся метрики
    'fields': 'campaign_id,campaign_name,spend,impressions,inline_link_clicks,actions,date_start',
    'time_range': json.dumps(time_range),
    'time_increment': 1,  # ВАЖНО: разбивка по дням для работы календаря
    'filtering': json.dumps([
        {'field': 'campaign.delivery_info', 'operator': 'IN', 'value': ['active', 'scheduled', 'paused']}
    ]),
    'limit': 1000
}

try:
    response = requests.get(url, params=params)
    response.raise_for_status()
    raw_data = response.json().get('data', [])
    
    processed_data = []
    for item in raw_data:
        actions = item.get('actions', [])
        leads = sum(int(a.get('value', 0)) for a in actions if a.get('action_type') in ['lead', 'offsite_conversion.fb_pixel_lead', 'onsite_conversion.lead_grouped'])
        purchases = sum(int(a.get('value', 0)) for a in actions if a.get('action_type') in ['purchase', 'offsite_conversion.fb_pixel_purchase'])

        processed_data.append({
            'date': item.get('date_start'), # Дата конкретного дня
            'id': item.get('campaign_id'),
            'name': item.get('campaign_name'),
            'spend': float(item.get('spend', 0)),
            'impressions': int(item.get('impressions', 0)),
            'clicks': int(item.get('inline_link_clicks', item.get('clicks', 0))),
            'leads': leads,
            'purchases': purchases
        })

    os.makedirs('data', exist_ok=True)
    with open('data/report.json', 'w', encoding='utf-8') as f:
        json.dump(processed_data, f, ensure_ascii=False, indent=4)
        
    print(f"Успешно выгружено строк данных: {len(processed_data)}")

except Exception as e:
    print(f"Ошибка: {e}")
    exit(1)
