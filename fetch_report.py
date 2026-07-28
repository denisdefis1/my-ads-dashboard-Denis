import os
import json
import requests
from datetime import datetime

# 1. Забираем секреты из настроек репозитория
RAW_TOKEN = os.getenv("FB_ACCESS_TOKEN")
RAW_ACCOUNT_ID = os.getenv("FB_AD_ACCOUNT_ID")

if not RAW_TOKEN or not RAW_ACCOUNT_ID:
    raise ValueError("Ошибка: Проверьте FB_ACCESS_TOKEN и FB_AD_ACCOUNT_ID в GitHub Secrets!")

# Очищаем токен и ID от возможных пробелов
ACCESS_TOKEN = str(RAW_TOKEN).strip()
clean_id = ''.join(filter(str.isdigit, str(RAW_ACCOUNT_ID)))
AD_ACCOUNT_ID = f"act_{clean_id}"

# ИСПРАВЛЕННЫЙ ОФИЦИАЛЬНЫЙ URL (теперь строго ://facebook.com со всеми слэшами)
url = f"https://://facebook.com/v19.0/{AD_ACCOUNT_ID}/insights"

# 2. Настраиваем временной диапазон (с 9 марта по сегодняшний день)
today_str = datetime.now().strftime('%Y-%m-%d')
time_range = {'since': '2026-03-09', 'until': today_str}

# 3. Параметры запроса к API Meta
params = {
    'access_token': ACCESS_TOKEN,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,impressions,inline_link_clicks,actions,date_start',
    'time_range': json.dumps(time_range),
    'time_increment': 1,
    'filtering': json.dumps([
        {'field': 'campaign.delivery_info', 'operator': 'IN', 'value': ['active', 'scheduled', 'paused']}
    ]),
    'limit': 1000
}

try:
    print(f"Запрос отправляется по правильному адресу: {url}")
    response = requests.get(url, params=params)
    response.raise_for_status()
    raw_data = response.json().get('data', [])
    
    processed_data = []
    
    # 4. Собираем массив статистики по дням
    for item in raw_data:
        actions = item.get('actions', [])
        
        leads = sum(int(a.get('value', 0)) for a in actions if a.get('action_type') in ['lead', 'offsite_conversion.fb_pixel_lead', 'onsite_conversion.lead_grouped'])
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

    # 5. Записываем чистый файл для интерактивного календаря на фронтенде
    os.makedirs('data', exist_ok=True)
    with open('data/report.json', 'w', encoding='utf-8') as f:
        json.dump(processed_data, f, ensure_ascii=False, indent=4)
        
    print(f"Данные успешно обновлены! Строк обработано: {len(processed_data)}")

except requests.exceptions.HTTPError as err:
    print(f"Facebook отклонил запрос. Ответ сервера: {err.response.text}")
    exit(1)
except Exception as e:
    print(f"Системная ошибка: {e}")
    exit(1)
