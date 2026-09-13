import asyncio
import os
import random
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

import config
import database as db

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


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


def roll_status_and_price(base_price: int):
    is_used = random.randint(1, 100) <= config.USED_STATUS_CHANCE
    adjust = random.uniform(config.PRICE_ADJUST_MIN, config.PRICE_ADJUST_MAX)

    if is_used:
        price = round(base_price * (1 - adjust))
        status = "Б/У"
    else:
        price = round(base_price * (1 + adjust))
        status = "Новый"

    return status, price


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

    status, price = roll_status_and_price(phone["base_price"])
    await db.set_last_pull_now(user_id)
    await db.record_pull(user_id, phone["id"], status, price)

    caption = (
        f"📱 Модель: {phone['model']}\n"
        f"⭐ Редкость: {phone['rarity']}\n"
        f"🔧 Статус: {status}\n"
        f"💰 Цена: {price}₽"
    )

    if phone["photo_file_id"]:
        await message.reply_photo(phone["photo_file_id"], caption=caption)
    else:
        await message.reply(caption + "\n\nФото телефона не было найдено.")


async def main():
    await db.init_db()
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
