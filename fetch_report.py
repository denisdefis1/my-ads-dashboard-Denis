import os
import json
import requests
from datetime import datetime

# 1. Получаем секреты из настроек репозитория
ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
RAW_ACCOUNT_ID = os.getenv("FB_AD_ACCOUNT_ID")

if not ACCESS_TOKEN or not RAW_ACCOUNT_ID:
    raise ValueError("Ошибка: Проверьте FB_ACCESS_TOKEN и FB_AD_ACCOUNT_ID в GitHub Secrets!")

# Защита от опечаток: очищаем ID от пробелов и дубликатов приставки "act_"
clean_id = str(RAW_ACCOUNT_ID).strip().replace('act_', '')
AD_ACCOUNT_ID = f"act_{clean_id}"

# Строго проверяем структуру ссылки, чтобы адрес не склеивался
url = f"https://facebook.com{AD_ACCOUNT_ID}/insights"

# 2. Настраиваем временной диапазон (с 9 марта по сегодняшний день)
today_str = datetime.now().strftime('%Y-%m-%d')
time_range = {'since': '2026-03-09', 'until': today_str}

# 3. Параметры запроса для получения точных цифр по дням
params = {
    'access_token': ACCESS_TOKEN,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,impressions,inline_link_clicks,actions,date_start',
    'time_range': json.dumps(time_range),
    'time_increment': 1,  # Включаем разбивку по дням для календаря на фронтенде
    'filtering': json.dumps([
        {'field': 'campaign.delivery_info', 'operator': 'IN', 'value': ['active', 'scheduled', 'paused']}
    ]),
    'limit': 1000
}

try:
    print(f"Отправка запроса к Meta API для аккаунта: {AD_ACCOUNT_ID}...")
    response = requests.get(url, params=params)
    response.raise_for_status()
    raw_data = response.json().get('data', [])
    
    processed_data = []
    
    # 4. Обрабатываем полученные строки данных
    for item in raw_data:
        actions = item.get('actions', [])
        
        # Считаем лиды (Lead Ads + Пиксель на сайте)
        leads = sum(int(a.get('value', 0)) for a in actions if a.get('action_type') in ['lead', 'offsite_conversion.fb_pixel_lead', 'onsite_conversion.lead_grouped'])
        # Считаем покупки
        purchases = sum(int(a.get('value', 0)) for a in actions if a.get('action_type') in ['purchase', 'offsite_conversion.fb_pixel_purchase'])

        processed_data.append({
            'date': item.get('date_start'),
            'id': item.get('campaign_id'),
            'name': item.get('campaign_name'),
            'spend': float(item.get('spend', 0)),
            'impressions': int(item.get('impressions', 0)),
            'clicks': int(item.get('inline_link_clicks', item.get('clicks', 0))),
            'leads': leads,
            'purchases': purchases
        })

    # 5. Создаем папку и сохраняем чистый JSON файл для вашего index.html
    os.makedirs('data', exist_ok=True)
    with open('data/report.json', 'w', encoding='utf-8') as f:
        json.dump(processed_data, f, ensure_ascii=False, indent=4)
        
    print(f"Успех! Скрипт выгрузил {len(processed_data)} строк статистики.")

except requests.exceptions.HTTPError as err:
    print(f"Ошибка со стороны API Facebook: {err.response.text}")
    exit(1)
except Exception as e:
    print(f"Непредвиденная ошибка в коде: {e}")
    exit(1)
