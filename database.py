import time
import random

import asyncpg

import config

_pool: asyncpg.Pool | None = None


async def init_db():
    """Создаёт пул подключений к Postgres и таблицы, если их ещё нет."""
    global _pool
    # statement_cache_size=0 — обязательно для Neon: их пулер соединений
    # (PgBouncer) несовместим с кэшированием планов запросов в asyncpg,
    # без этого рано или поздно вылетает InvalidCachedStatementError.
    _pool = await asyncpg.create_pool(
        config.DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )

    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS phones (
                id SERIAL PRIMARY KEY,
                model TEXT NOT NULL,
                rarity TEXT NOT NULL,
                base_price INTEGER NOT NULL,
                photo_file_id TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                last_pull_at DOUBLE PRECISION DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_phones (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                phone_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                price INTEGER NOT NULL,
                obtained_at DOUBLE PRECISION NOT NULL
            )
        """)

        # Новые колонки для промокодов — ALTER, а не пересоздание таблицы,
        # чтобы не потерять уже накопленные данные игроков
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance INTEGER NOT NULL DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS luck_boost_percent "
            "DOUBLE PRECISION NOT NULL DEFAULT 0"
        )
        # Сколько ещё "тел" подряд действует буст удачи — раньше буст был
        # разовым (1 использование), теперь может действовать на N раз подряд
        # (например, реферальная награда — 3 "тел" подряд)
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS luck_boost_pulls_remaining "
            "INTEGER NOT NULL DEFAULT 0"
        )

        # Колонки для рефералов
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        )

        # Колонки для профиля
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at "
            "DOUBLE PRECISION DEFAULT extract(epoch from now())"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS reputation "
            "DOUBLE PRECISION NOT NULL DEFAULT 0"
        )
        # "Снимок" редкости в момент получения — чтобы статистика (например,
        # максимальная редкость за всё время) не ломалась, если телефон
        # потом удалят из каталога через /delphone
        await conn.execute(
            "ALTER TABLE user_phones ADD COLUMN IF NOT EXISTS rarity TEXT"
        )
        # Разовая доливка для записей, сделанных до этого обновления —
        # берём редкость из каталога, пока телефон там ещё есть
        await conn.execute("""
            UPDATE user_phones up SET rarity = p.rarity
            FROM phones p
            WHERE up.phone_id = p.id AND up.rarity IS NULL
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                reward_type TEXT NOT NULL,
                reward_value TEXT,
                max_activations INTEGER NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_activations (
                id SERIAL PRIMARY KEY,
                promo_id INTEGER NOT NULL REFERENCES promocodes(id),
                user_id BIGINT NOT NULL,
                activated_at DOUBLE PRECISION NOT NULL,
                UNIQUE (promo_id, user_id)
            )
        """)

        # Метеориты — премиум-валюта для апгрейда телефонов
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS meteorites INTEGER NOT NULL DEFAULT 0"
        )
        # Ранг апгрейда конкретного экземпляра телефона (F/B/C/A/S/SS/SSS),
        # NULL — телефон ещё не апгрейжен
        await conn.execute(
            "ALTER TABLE user_phones ADD COLUMN IF NOT EXISTS rank TEXT"
        )

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                event_type TEXT NOT NULL,
                percent DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION NOT NULL,
                ends_at DOUBLE PRECISION NOT NULL,
                created_by TEXT NOT NULL
            )
        """)


# ---------- ТЕЛЕФОНЫ ----------

async def add_phone(model: str, rarity: str, base_price: int, photo_file_id: str | None):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO phones (model, rarity, base_price, photo_file_id) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            model, rarity, base_price, photo_file_id,
        )
        return row["id"]


async def get_all_phones():
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM phones ORDER BY id")


async def get_phone(phone_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM phones WHERE id = $1", phone_id)


async def delete_phone(phone_id: int) -> bool:
    """Удаляет телефон из каталога. Возвращает True, если что-то было удалено."""
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM phones WHERE id = $1", phone_id)
        return result.endswith(" 1")


async def get_phones_by_rarity(rarity: str):
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM phones WHERE rarity = $1", rarity)


async def pick_random_phone(boost_percent: float = 0):
    """Сначала выбираем редкость по весам (только те, где есть хотя бы 1 телефон),
    затем случайную модель внутри этой редкости. boost_percent (0-100+) слегка
    повышает шанс более редких категорий — чем выше редкость, тем сильнее буст,
    но даже на максимуме топовые редкости остаются маловероятными."""
    all_phones = await get_all_phones()
    available_rarities = {row["rarity"] for row in all_phones}
    if not available_rarities:
        return None

    order = config.RARITY_ORDER
    max_index = len(order) - 1 if len(order) > 1 else 1
    boost_fraction = (boost_percent or 0) / 100

    weighted = []
    for r, w in config.RARITY_WEIGHTS.items():
        if r not in available_rarities:
            continue
        idx = order.index(r)
        factor = 1 + boost_fraction * (idx / max_index)
        weighted.append((r, w * factor))

    rarities, weights = zip(*weighted)
    chosen_rarity = random.choices(rarities, weights=weights, k=1)[0]

    candidates = [p for p in all_phones if p["rarity"] == chosen_rarity]
    return random.choice(candidates)


# ---------- ПОЛЬЗОВАТЕЛИ / КУЛДАУН ----------

async def get_user(user_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)


async def ensure_user(user_id: int, username: str | None):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, last_pull_at) VALUES ($1, $2, 0) "
            "ON CONFLICT (user_id) DO UPDATE SET username = $2",
            user_id, username,
        )


async def seconds_until_ready(user_id: int) -> int:
    user = await get_user(user_id)
    if user is None:
        return 0
    elapsed = time.time() - user["last_pull_at"]
    remaining = config.COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining))


async def set_last_pull_now(user_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_pull_at = $1 WHERE user_id = $2",
            time.time(), user_id,
        )


async def reset_cooldown(user_id: int):
    """Сбрасывает кулдаун на «тел», как будто игрок ещё не фармил."""
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_pull_at = 0 WHERE user_id = $1", user_id)


async def add_balance(user_id: int, amount: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id
        )


async def get_balance(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        return row["balance"] if row else 0


async def set_luck_boost(user_id: int, percent: float, pulls: int = 1):
    """Задаёт буст удачи, действующий на следующие `pulls` "тел" подряд.
    По умолчанию 1 (разовое использование, как у промокода 'luck')."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET luck_boost_percent = $1, luck_boost_pulls_remaining = $2 "
            "WHERE user_id = $3",
            percent, pulls, user_id,
        )


async def consume_luck_boost(user_id: int) -> float:
    """Возвращает текущий буст удачи игрока и уменьшает счётчик оставшихся
    "тел", на которые он действует. Когда счётчик доходит до 0 — буст
    сгорает полностью."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT luck_boost_percent, luck_boost_pulls_remaining "
            "FROM users WHERE user_id = $1",
            user_id,
        )
        if not row or not row["luck_boost_pulls_remaining"]:
            return 0.0

        boost = float(row["luck_boost_percent"])
        remaining = row["luck_boost_pulls_remaining"] - 1

        if remaining <= 0:
            await conn.execute(
                "UPDATE users SET luck_boost_percent = 0, luck_boost_pulls_remaining = 0 "
                "WHERE user_id = $1",
                user_id,
            )
        else:
            await conn.execute(
                "UPDATE users SET luck_boost_pulls_remaining = $1 WHERE user_id = $2",
                remaining, user_id,
            )

        return boost


async def record_pull(user_id: int, phone_id: int, status: str, price: int, rarity: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_phones (user_id, phone_id, status, price, obtained_at, rarity) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            user_id, phone_id, status, price, time.time(), rarity,
        )


async def get_user_phone_count(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM user_phones WHERE user_id = $1", user_id
        )
        return row["cnt"]


# ---------- ИНВЕНТАРЬ ----------

async def get_user_owned_rarities(user_id: int) -> dict:
    """Возвращает {редкость: количество} только для тех редкостей, которые
    у игрока реально есть. Использует сохранённый 'снимок' редкости
    (up.rarity), чтобы удалённые из каталога телефоны не пропадали из
    инвентаря игрока."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COALESCE(up.rarity, p.rarity) AS rarity, COUNT(*) AS cnt "
            "FROM user_phones up LEFT JOIN phones p ON p.id = up.phone_id "
            "WHERE up.user_id = $1 GROUP BY COALESCE(up.rarity, p.rarity)",
            user_id,
        )
        return {r["rarity"]: r["cnt"] for r in rows}


async def get_user_phones_by_rarity(user_id: int, rarity: str):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT up.id AS up_id, up.status, up.price, up.obtained_at, up.rank, "
            "COALESCE(p.model, 'Модель удалена из каталога') AS model, "
            "COALESCE(up.rarity, p.rarity) AS rarity, p.photo_file_id, p.base_price "
            "FROM user_phones up LEFT JOIN phones p ON p.id = up.phone_id "
            "WHERE up.user_id = $1 AND COALESCE(up.rarity, p.rarity) = $2 "
            "ORDER BY up.obtained_at DESC",
            user_id, rarity,
        )


async def get_user_phone_item(up_id: int):
    """Возвращает конкретный экземпляр телефона по ID записи в user_phones,
    вместе с user_id владельца — для проверки, что запрашивает тот же игрок."""
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT up.id AS up_id, up.user_id, up.status, up.price, up.obtained_at, up.rank, "
            "COALESCE(p.model, 'Модель удалена из каталога') AS model, "
            "COALESCE(up.rarity, p.rarity) AS rarity, p.photo_file_id, p.base_price "
            "FROM user_phones up LEFT JOIN phones p ON p.id = up.phone_id "
            "WHERE up.id = $1",
            up_id,
        )


async def get_user_used_phones(user_id: int):
    """Телефоны игрока со статусом Б/У — кандидаты на починку статуса."""
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT up.id AS up_id, up.status, up.price, up.obtained_at, up.rank, "
            "COALESCE(p.model, 'Модель удалена из каталога') AS model, "
            "COALESCE(up.rarity, p.rarity) AS rarity, p.photo_file_id, p.base_price "
            "FROM user_phones up LEFT JOIN phones p ON p.id = up.phone_id "
            "WHERE up.user_id = $1 AND up.status = 'Б/У' "
            "ORDER BY up.obtained_at DESC",
            user_id,
        )


async def fix_phone_status(up_id: int, new_price: int):
    """Чинит статус конкретного экземпляра телефона на 'Новый' и
    выставляет цену без бонуса/скидки (ровно базовая цена)."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_phones SET status = 'Новый', price = $1 WHERE id = $2",
            new_price, up_id,
        )


# ---------- ПРОМОКОДЫ ----------

async def add_promo(code: str, reward_type: str, reward_value: str, max_activations: int):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO promocodes (code, reward_type, reward_value, max_activations, created_at) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            code, reward_type, reward_value, max_activations, time.time(),
        )
        return row["id"]


async def get_promo_by_code(code: str):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM promocodes WHERE code = $1", code)


async def get_promo(promo_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM promocodes WHERE id = $1", promo_id)


async def get_promo_activation_count(promo_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM promo_activations WHERE promo_id = $1", promo_id
        )
        return row["cnt"]


async def has_user_activated(promo_id: int, user_id: int) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM promo_activations WHERE promo_id = $1 AND user_id = $2",
            promo_id, user_id,
        )
        return row is not None


async def record_promo_activation(promo_id: int, user_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promo_activations (promo_id, user_id, activated_at) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            promo_id, user_id, time.time(),
        )


# ---------- ПРОФИЛЬ ----------

async def get_inventory_value(user_id: int) -> int:
    """Суммарная цена всех телефонов игрока (используем сохранённую цену
    покупки, не текущую базовую — так корректно, даже если каталог менялся)."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(price), 0) AS total FROM user_phones WHERE user_id = $1",
            user_id,
        )
        return row["total"]


async def get_max_rarity(user_id: int) -> str | None:
    """Самая высокая редкость, которую игрок когда-либо выбивал (по
    сохранённому 'снимку' редкости, не зависит от текущего каталога)."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT rarity FROM user_phones WHERE user_id = $1 AND rarity IS NOT NULL",
            user_id,
        )
    if not rows:
        return None
    owned = {r["rarity"] for r in rows}
    for rarity in reversed(config.RARITY_ORDER):
        if rarity in owned:
            return rarity
    return None


async def get_balance_rank(user_id: int) -> int:
    """ROW_NUMBER, а не RANK — иначе все игроки с одинаковым балансом
    (например, все новички с 0₽) показывали бы одно и то же место в топе."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT rn FROM (
                SELECT user_id,
                       ROW_NUMBER() OVER (ORDER BY balance DESC, user_id ASC) AS rn
                FROM users
            ) t WHERE user_id = $1
            """,
            user_id,
        )
        return row["rn"] if row else 1


async def get_inventory_rank(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT rn FROM (
                SELECT u.user_id,
                       ROW_NUMBER() OVER (
                           ORDER BY COALESCE(SUM(up.price), 0) DESC, u.user_id ASC
                       ) AS rn
                FROM users u
                LEFT JOIN user_phones up ON up.user_id = u.user_id
                GROUP BY u.user_id
            ) t WHERE user_id = $1
            """,
            user_id,
        )
        return row["rn"] if row else 1


async def get_promo_used_count(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM promo_activations WHERE user_id = $1", user_id
        )
        return row["cnt"]


# ---------- РЕФЕРАЛЫ ----------

async def set_referrer_if_none(user_id: int, referrer_id: int):
    """Фиксирует пригласившего только один раз — если у игрока уже есть
    referred_by, повторный переход по чужой ссылке ничего не меняет."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET referred_by = $1 WHERE user_id = $2 AND referred_by IS NULL",
            referrer_id, user_id,
        )


async def get_referrer(user_id: int) -> int | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referred_by FROM users WHERE user_id = $1", user_id
        )
        return row["referred_by"] if row else None


async def is_referral_rewarded(user_id: int) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referral_rewarded FROM users WHERE user_id = $1", user_id
        )
        return bool(row["referral_rewarded"]) if row else False


async def mark_referral_rewarded(user_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET referral_rewarded = TRUE WHERE user_id = $1", user_id
        )


async def get_referral_count(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM users WHERE referred_by = $1", user_id
        )
        return row["cnt"]


# ---------- МЕТЕОРИТЫ И АПГРЕЙД ----------

async def get_meteorites(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT meteorites FROM users WHERE user_id = $1", user_id
        )
        return row["meteorites"] if row else 0


async def add_meteorites(user_id: int, amount: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET meteorites = meteorites + $1 WHERE user_id = $2",
            amount, user_id,
        )


async def apply_upgrade(up_id: int, new_price: int, new_status: str, rank: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_phones SET price = $1, status = $2, rank = $3 WHERE id = $4",
            new_price, new_status, rank, up_id,
        )


# ---------- ПРОДАЖА ----------

async def delete_user_phone(up_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM user_phones WHERE id = $1", up_id)


# ---------- ИВЕНТЫ ----------

async def create_event(event_type: str, percent: float, duration_seconds: int, created_by: str):
    now = time.time()
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events (event_type, percent, started_at, ends_at, created_by) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            event_type, percent, now, now + duration_seconds, created_by,
        )
        return row["id"]


async def get_active_event():
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM events WHERE ends_at > $1 ORDER BY started_at DESC LIMIT 1",
            time.time(),
        )


async def get_all_user_ids() -> list[int]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]
