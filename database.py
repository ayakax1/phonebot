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


async def set_luck_boost(user_id: int, percent: float):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET luck_boost_percent = $1 WHERE user_id = $2", percent, user_id
        )


async def consume_luck_boost(user_id: int) -> float:
    """Возвращает текущий буст удачи игрока и сразу сбрасывает его —
    буст действует ровно на один следующий «тел»."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT luck_boost_percent FROM users WHERE user_id = $1", user_id
        )
        boost = float(row["luck_boost_percent"]) if row and row["luck_boost_percent"] else 0.0
        if boost:
            await conn.execute(
                "UPDATE users SET luck_boost_percent = 0 WHERE user_id = $1", user_id
            )
        return boost


async def record_pull(user_id: int, phone_id: int, status: str, price: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_phones (user_id, phone_id, status, price, obtained_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, phone_id, status, price, time.time(),
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
    у игрока реально есть."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.rarity, COUNT(*) AS cnt "
            "FROM user_phones up JOIN phones p ON p.id = up.phone_id "
            "WHERE up.user_id = $1 GROUP BY p.rarity",
            user_id,
        )
        return {r["rarity"]: r["cnt"] for r in rows}


async def get_user_phones_by_rarity(user_id: int, rarity: str):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT up.id AS up_id, up.status, up.price, up.obtained_at, "
            "p.model, p.rarity, p.photo_file_id, p.base_price "
            "FROM user_phones up JOIN phones p ON p.id = up.phone_id "
            "WHERE up.user_id = $1 AND p.rarity = $2 "
            "ORDER BY up.obtained_at DESC",
            user_id, rarity,
        )


async def get_user_phone_item(up_id: int):
    """Возвращает конкретный экземпляр телефона по ID записи в user_phones,
    вместе с user_id владельца — для проверки, что запрашивает тот же игрок."""
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT up.id AS up_id, up.user_id, up.status, up.price, up.obtained_at, "
            "p.model, p.rarity, p.photo_file_id, p.base_price "
            "FROM user_phones up JOIN phones p ON p.id = up.phone_id "
            "WHERE up.id = $1",
            up_id,
        )


async def get_user_used_phones(user_id: int):
    """Телефоны игрока со статусом Б/У — кандидаты на починку статуса."""
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT up.id AS up_id, up.status, up.price, up.obtained_at, "
            "p.model, p.rarity, p.photo_file_id, p.base_price "
            "FROM user_phones up JOIN phones p ON p.id = up.phone_id "
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
