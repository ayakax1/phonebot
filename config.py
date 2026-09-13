import os

# === НАСТРОЙКИ БОТА ===

# На Render токен и адрес базы задаются в переменных окружения (Environment)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8910067961:AAF18Pao4oW1IkWcE4Me70l_wQjNiIGn50s")

# Строка подключения к базе Postgres (Neon). Пример:
# postgresql://user:password@ep-xxx.neon.tech/neondb?sslmode=require
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Telegram user_id тех, кому разрешено добавлять телефоны (/addphone)
ADMIN_IDS = [8847038707]  # замените на свой user_id

# Перезарядка между выдачами (в секундах)
COOLDOWN_SECONDS = 3 * 60 * 60  # 3 часа

# Шанс статуса "Б/У" (в процентах). Оставшееся — "Новый"
USED_STATUS_CHANCE = 85

# Диапазон изменения цены (доля от базовой стоимости)
PRICE_ADJUST_MIN = 0.15  # минимум 15%
PRICE_ADJUST_MAX = 0.50  # максимум 50%

# Порядок редкостей от самой частой до самой редкой
# Вес — относительная "частота" выпадения. Можно менять как угодно.
RARITY_WEIGHTS = {
    "Мусор": 300,
    "Необычный": 220,
    "Редкий": 150,
    "Эпический": 90,
    "Легендарный": 50,
    "Мифический": 25,
    "Хроматический": 12,
    "Коллекционный": 6,
    "Секретный": 3,
    "Космический": 1,
}

# Порядок для красивого вывода/сортировки (от низшей к высшей)
RARITY_ORDER = list(RARITY_WEIGHTS.keys())
