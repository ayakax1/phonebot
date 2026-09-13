import asyncio
import os
import random
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

MSK = ZoneInfo("Europe/Moscow")

RARITY_EMOJI = {
    "Мусор": "🗑",
    "Необычный": "🔹",
    "Редкий": "🔷",
    "Эпический": "🟣",
    "Легендарный": "⭐",
    "Мифический": "✨",
    "Хроматический": "🌈",
    "Коллекционный": "📦",
    "Секретный": "🕵️",
    "Космический": "🌌",
}
STATUS_EMOJI = {"Б/У": "♻️", "Новый": "🆕"}


# --- Фейковый веб-сервер, чтобы Render считал сервис "Web Service" ---
# и не выключал его. Пингуйте URL сервиса снаружи (например, UptimeRobot),
# чтобы Render не отправлял его в сон.
async def handle_ping(request):
    return web.Response(text="OK, бот жив")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер для healthcheck запущен на порту {port}")


def format_seconds(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if not h and not m:
        parts.append(f"{s} сек")
    return " ".join(parts)


def round_down_to_5(n: float) -> int:
    return (int(n) // 5) * 5


def roll_status_and_price(base_price: int):
    """Возвращает (статус, финальная_цена, дельта). Дельта — всегда
    положительное число, округлённое вниз до кратного 5."""
    is_used = random.randint(1, 100) <= config.USED_STATUS_CHANCE
    adjust_fraction = random.uniform(config.PRICE_ADJUST_MIN, config.PRICE_ADJUST_MAX)
    delta = round_down_to_5(base_price * adjust_fraction)

    if is_used:
        price = base_price - delta
        status = "Б/У"
    else:
        price = base_price + delta
        status = "Новый"

    return status, price, delta


def format_msk_time(ts: float, short: bool = False) -> str:
    dt = datetime.fromtimestamp(ts, tz=MSK)
    if short:
        return dt.strftime("%d.%m %H:%M")
    return dt.strftime("%d.%m.%Y %H:%M МСК")


def display_username(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


def format_phone_card(
    *, username, model, status, rarity, base_price, delta, obtained_ts,
    added_note: bool, total_count: int | None = None,
) -> str:
    rarity_emoji = RARITY_EMOJI.get(rarity, "❔")
    status_emoji = STATUS_EMOJI.get(status, "")
    sign = "-" if status == "Б/У" else "+"
    time_str = format_msk_time(obtained_ts)

    lines = [
        f" {username}, получен новый телефон!" if added_note else "📱 Информация о телефоне",
        "",
        f"🕒 Дата получения: {time_str}",
        f"📱 Модель: {model}",
        "",
        f"{status_emoji} Статус: {status}",
        f"{rarity_emoji} Редкость: {rarity}",
        f"💰 Стоимость: {base_price}₽ ({sign}{delta}₽)",
    ]

    if added_note:
        lines.append("")
        lines.append("✅ Новый телефон уже добавлен в Ваш инвентарь!")
        if total_count is not None:
            lines.append(f"📊 Всего в коллекции: {total_count} шт.")

    return "\n".join(lines)


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "📱 Бот-фарм телефонов!\n\n"
        "Напиши слово «тел» (и только его) в этот чат или в группу, "
        "чтобы получить случайный телефон случайной редкости.\n"
        f"Перезарядка: {format_seconds(config.COOLDOWN_SECONDS)}."
    )


@dp.message(Command("addphone"))
async def cmd_add_phone(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    if not message.caption:
        await message.answer(
            "Отправь фото с подписью в формате:\n"
            "/addphone Модель;Редкость;Базовая_цена\n\n"
            "Пример: /addphone Xiaomi Redmi 6A;Мусор;3000"
        )
        return

    try:
        payload = message.caption.split(maxsplit=1)[1]
        model, rarity, price_str = [p.strip() for p in payload.split(";")]
        base_price = int(price_str)
    except (IndexError, ValueError):
        await message.answer(
            "Неверный формат. Пример:\n/addphone Xiaomi Redmi 6A;Мусор;3000"
        )
        return

    if rarity not in config.RARITY_WEIGHTS:
        await message.answer(
            f"Неизвестная редкость «{rarity}». Доступные: "
            + ", ".join(config.RARITY_ORDER)
        )
        return

    # Фото не скачиваем на диск — сохраняем только file_id, само фото
    # навсегда остаётся на серверах Telegram. Не удаляйте это сообщение
    # с фото у бота в личке — иначе file_id может перестать работать.
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id

    phone_id = await db.add_phone(model, rarity, base_price, photo_file_id)
    await message.answer(
        f"✅ Добавлен телефон #{phone_id}: {model} ({rarity}), база {base_price}₽"
    )


@dp.message(Command("listphones"))
async def cmd_list_phones(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    phones = await db.get_all_phones()
    if not phones:
        await message.answer("В базе пока нет ни одного телефона.")
        return

    lines = [
        f"#{p['id']} — {p['model']} ({p['rarity']}), база {p['base_price']}₽"
        for p in phones
    ]
    # Telegram режет длинные сообщения на 4096 символов — на всякий случай
    # шлём частями, если телефонов будет очень много
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


@dp.message(Command("delphone"))
async def cmd_del_phone(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer(
            "Использование: /delphone ID\n"
            "Посмотреть ID всех телефонов можно командой /listphones"
        )
        return

    phone_id = int(parts[1].strip())
    phone = await db.get_phone(phone_id)
    if phone is None:
        await message.answer(f"Телефон с ID {phone_id} не найден.")
        return

    await db.delete_phone(phone_id)
    await message.answer(f"🗑 Удалён: {phone['model']} ({phone['rarity']}), был #{phone_id}")


@dp.message(F.text)
async def handle_tel_pull(message: Message):
    text = message.text.strip().lower()

    # Реагируем только если сообщение СТРОГО равно "тел" и это не ответ на другое сообщение
    if text != "тел":
        return
    if message.reply_to_message is not None:
        return

    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    await db.ensure_user(user_id, username)

    remaining = await db.seconds_until_ready(user_id)
    if remaining > 0:
        await message.reply(
            f"⏳ Перезарядка ещё не прошла. Подожди {format_seconds(remaining)}."
        )
        return

    phone = await db.pick_random_phone()
    if phone is None:
        await message.reply("В базе пока нет ни одного телефона. Попроси админа добавить!")
        return

    status, price, delta = roll_status_and_price(phone["base_price"])
    await db.set_last_pull_now(user_id)
    await db.record_pull(user_id, phone["id"], status, price)
    total_count = await db.get_user_phone_count(user_id)

    caption = format_phone_card(
        username=display_username(message.from_user),
        model=phone["model"],
        status=status,
        rarity=phone["rarity"],
        base_price=phone["base_price"],
        delta=delta,
        obtained_ts=time.time(),
        added_note=True,
        total_count=total_count,
    )

    if phone["photo_file_id"]:
        await message.reply_photo(phone["photo_file_id"], caption=caption)
    else:
        await message.reply(caption + "\n\nФото телефона не было найдено.")


@dp.message(Command("inventory", "инв"))
async def cmd_inventory(message: Message):
    rarities = await db.get_user_owned_rarities(message.from_user.id)
    if not rarities:
        await message.answer(
            "В вашем инвентаре пока пусто. Напишите «тел», чтобы получить первый телефон!"
        )
        return
    await message.answer("📦 Выберите редкость:", reply_markup=build_rarity_keyboard(rarities))


def build_rarity_keyboard(rarities: dict):
    kb = InlineKeyboardBuilder()
    for rarity in config.RARITY_ORDER:
        if rarity in rarities:
            emoji = RARITY_EMOJI.get(rarity, "")
            kb.button(
                text=f"{emoji} {rarity} ({rarities[rarity]})",
                callback_data=f"inv_rarity:{rarity}",
            )
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("inv_rarity:"))
async def cb_inventory_rarity(callback: CallbackQuery):
    rarity = callback.data.split(":", 1)[1]
    items = await db.get_user_phones_by_rarity(callback.from_user.id, rarity)
    if not items:
        await callback.answer("Тут пока пусто", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for item in items:
        time_str = format_msk_time(item["obtained_at"], short=True)
        kb.button(text=f"{item['model']} ({time_str})", callback_data=f"inv_item:{item['up_id']}")
    kb.button(text="⬅️ Назад", callback_data="inv_back")
    kb.adjust(1)

    emoji = RARITY_EMOJI.get(rarity, "")
    await callback.message.edit_text(
        f"{emoji} {rarity} — ваши телефоны:", reply_markup=kb.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "inv_back")
async def cb_inventory_back(callback: CallbackQuery):
    rarities = await db.get_user_owned_rarities(callback.from_user.id)
    await callback.message.edit_text("📦 Выберите редкость:", reply_markup=build_rarity_keyboard(rarities))
    await callback.answer()


@dp.callback_query(F.data.startswith("inv_item:"))
async def cb_inventory_item(callback: CallbackQuery):
    up_id = int(callback.data.split(":", 1)[1])
    item = await db.get_user_phone_item(up_id)

    if item is None or item["user_id"] != callback.from_user.id:
        await callback.answer("Не найдено", show_alert=True)
        return

    delta = abs(item["price"] - item["base_price"])
    caption = format_phone_card(
        username=display_username(callback.from_user),
        model=item["model"],
        status=item["status"],
        rarity=item["rarity"],
        base_price=item["base_price"],
        delta=delta,
        obtained_ts=item["obtained_at"],
        added_note=False,
    )

    if item["photo_file_id"]:
        await callback.message.answer_photo(item["photo_file_id"], caption=caption)
    else:
        await callback.message.answer(caption + "\n\nФото телефона не было найдено.")
    await callback.answer()


async def main():
    await db.init_db()
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
