import asyncio
import os
import random
import time
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import profile as profile_module

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

BOT_USERNAME = None  # заполняется при старте в main() через bot.get_me()

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


# ---------- АПГРЕЙД ТЕЛЕФОНОВ ----------

UPGRADE_COST_RUB = 25000
RANK_MULTIPLIERS = {
    "F": 0.80,   # -20%
    "B": 1.10,   # +10%
    "C": 1.20,   # +20%
    "A": 1.40,   # +40%
    "S": 1.50,   # +50%
    "SS": 2.50,  # +150%
    "SSS": 4.00, # +300%
}
RANKS_THAT_FIX_STATUS = {"SS", "SSS"}  # выше S — чинят Б/У -> Новый


def roll_upgrade_rank(rarity: str):
    """Чем выше редкость телефона, тем выше шанс на неудачу (F).
    SS/SSS — всегда очень маленький шанс, независимо от редкости."""
    order = config.RARITY_ORDER
    idx = order.index(rarity) if rarity in order else 0
    max_idx = len(order) - 1 if len(order) > 1 else 1

    f_chance = 5 + (idx / max_idx) * 55  # от 5% (Мусор) до 60% (Космический)
    remaining = 100 - f_chance

    success_shares = {"B": 0.40, "C": 0.30, "A": 0.15, "S": 0.10, "SS": 0.04, "SSS": 0.01}
    weights = {"F": f_chance}
    for rank, share in success_shares.items():
        weights[rank] = remaining * share

    ranks = list(weights.keys())
    probs = list(weights.values())
    chosen = random.choices(ranks, weights=probs, k=1)[0]
    return chosen, RANK_MULTIPLIERS[chosen]



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
    rank: str | None = None, current_price: int | None = None,
) -> str:
    rarity_emoji = RARITY_EMOJI.get(rarity, "❔")
    status_emoji = STATUS_EMOJI.get(status, "")
    sign = "-" if status == "Б/У" else "+"
    time_str = format_msk_time(obtained_ts)

    model_safe = html.escape(str(model))
    username_safe = html.escape(str(username))

    lines = [
        f"🎉 {username_safe}, получен новый телефон!" if added_note else "📱 Информация о телефоне",
        "",
        f"🕒 Дата получения: {time_str}",
        f"📱 Модель: {model_safe}",
        "",
        f"{status_emoji} Статус: {status}",
        f"{rarity_emoji} Редкость: {rarity}",
    ]

    if current_price is not None:
        lines.append(f"💰 Текущая цена: {current_price}₽")
    else:
        lines.append(f"💰 Стоимость: {base_price}₽ ({sign}{delta}₽)")

    if rank:
        lines.append(f"<b>РАНГ ТЕЛЕФОНА: {rank}</b>")

    if added_note:
        lines.append("")
        lines.append("✅ Новый телефон уже добавлен в Ваш инвентарь!")
        if total_count is not None:
            lines.append(f"📊 Всего в коллекции: {total_count} шт.")

    return "\n".join(lines)


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.ensure_user(user_id, display_username(message.from_user))

    if command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args.removeprefix("ref_"))
        except ValueError:
            referrer_id = None
        if referrer_id and referrer_id != user_id:
            await db.set_referrer_if_none(user_id, referrer_id)

    await message.answer(
        "📱 Бот-фарм телефонов!\n\n"
        "Напиши слово «тел» (и только его) в этот чат или в группу, "
        "чтобы получить случайный телефон случайной редкости.\n"
        f"Перезарядка: {format_seconds(config.COOLDOWN_SECONDS)}."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📱 Бот-фарм телефонов!\n\n"
        "Напиши слово «тел» (и только его) в этот чат или в группу, "
        "чтобы получить случайный телефон случайной редкости.\n"
        f"Перезарядка: {format_seconds(config.COOLDOWN_SECONDS)}."
    )


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    user_id = message.from_user.id
    await db.ensure_user(user_id, display_username(message.from_user))
    count = await db.get_referral_count(user_id)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    await message.answer(
        f"🔗 Ваша реферальная ссылка:\n{link}\n\n"
        f"👥 Приглашено друзей: {count}\n\n"
        "Когда друг перейдёт по ссылке и получит свой первый телефон — "
        "вы получите сброс кулдауна и буст удачи +50% на следующие 3 «тел»!"
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


@dp.message(Command("inventory"))
async def cmd_inventory(message: Message):
    await send_inventory_rarities(message, mode="sell")


@dp.message(Command("update"))
async def cmd_update(message: Message):
    await send_inventory_rarities(message, mode="upgrade")


@dp.message(Command("tel"))
async def cmd_tel(message: Message):
    await handle_tel_pull(message)


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    await show_profile(message)


async def show_profile(message: Message):
    user = message.from_user
    user_id = user.id
    await db.ensure_user(user_id, display_username(user))

    avatar_bytes = None
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            file = await bot.get_file(file_id)
            buf = await bot.download_file(file.file_path)
            avatar_bytes = buf.read()
    except Exception as e:
        logging.warning(f"Не удалось получить аватарку для профиля: {e}")

    header_png = profile_module.build_profile_header(avatar_bytes)

    user_row = await db.get_user(user_id)
    balance = user_row["balance"] if user_row else 0
    created_at = user_row["created_at"] if user_row and user_row["created_at"] else None
    reputation = user_row["reputation"] if user_row and user_row["reputation"] is not None else 0.0
    upgrades_successful = user_row["upgrades_successful"] if user_row else 0
    phones_sold = user_row["phones_sold"] if user_row else 0
    is_tester = user_row["is_tester"] if user_row else False
    tester_probation = user_row["tester_probation"] if user_row else False

    inventory_value = await db.get_inventory_value(user_id)
    max_rarity = await db.get_max_rarity(user_id)
    balance_rank = await db.get_balance_rank(user_id)
    inventory_rank = await db.get_inventory_rank(user_id)
    promo_used = await db.get_promo_used_count(user_id)

    reg_date = format_msk_time(created_at) if created_at else "в/р"
    if max_rarity:
        max_rarity_display = f"{RARITY_EMOJI.get(max_rarity, '')} {max_rarity}"
    else:
        max_rarity_display = "—"

    caption = (
        f"💰 Баланс: {balance}₽\n"
        f"📦 Цена инвентаря: {inventory_value}₽\n"
        f"🏆 Максимальная редкость: {max_rarity_display}\n"
        f"📅 Дата регистрации в боте: {reg_date}\n\n"
        f"🥇 Место в топе по балансу: {balance_rank}\n"
        f"🥇 Место в топе по цене инвентаря: {inventory_rank}\n\n"
        f"🎟 Промокодов использовано: {promo_used}\n"
        f"🔧 Телефонов улучшено: {upgrades_successful}\n"
        f"🏅 Достижений выполнено: в/р\n"
        f"💸 Телефонов продано: {phones_sold}\n\n"
        f"⭐ Репутация: {reputation:+.2f}"
    )

    if is_tester:
        tester_line = "❗Данный пользователь является официальным тестером бота"
        if tester_probation:
            tester_line += " (исп. срок)"
        caption += f"\n\n{tester_line}"

    photo = BufferedInputFile(header_png, filename="profile.png")
    await message.answer_photo(photo, caption=caption)


@dp.message(F.text)
async def handle_text_triggers(message: Message):
    text = message.text.strip().lower()

    # Команды (/addpromo, /promo и т.д.) обрабатываются своими отдельными
    # хендлерами. SkipHandler явно просит aiogram передать сообщение
    # дальше по списку обработчиков — иначе он считал бы, что раз F.text
    # совпал, сообщение уже обработано, и дальше не искал бы нужную команду
    if text.startswith("/"):
        raise SkipHandler

    # Реагируем только на голое слово, без другого текста, и не на ответ на сообщение
    if message.reply_to_message is not None:
        return

    if text == "тел":
        await handle_tel_pull(message)
    elif text == "инв":
        await send_inventory_rarities(message, mode="sell")
    elif text == "апдейт":
        await send_inventory_rarities(message, mode="upgrade")
    elif text == "мой акк":
        await show_profile(message)
    elif text == "ежедневка":
        await handle_daily(message)


async def handle_tel_pull(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    await db.ensure_user(user_id, username)

    remaining = await db.seconds_until_ready(user_id)
    if remaining > 0:
        await message.reply(
            f"⏳ Перезарядка ещё не прошла. Подожди {format_seconds(remaining)}."
        )
        return

    boost = await db.consume_luck_boost(user_id)

    # Ивент удачи действует на всех сразу, поверх личного буста игрока
    event = await db.get_active_event()
    if event and event["event_type"] == "luck":
        boost += event["percent"]

    phone = await db.pick_random_phone(boost_percent=boost)
    if phone is None:
        await message.reply("В базе пока нет ни одного телефона. Попроси админа добавить!")
        return

    status, price, delta = roll_status_and_price(phone["base_price"])
    await db.set_last_pull_now(user_id)
    await db.record_pull(user_id, phone["id"], status, price, phone["rarity"])
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
        await message.reply_photo(phone["photo_file_id"], caption=caption, parse_mode="HTML")
    else:
        await message.reply(caption + "\n\nФото телефона не было найдено.", parse_mode="HTML")

    # Метеорит — шанс 50% при выпадении редкости Хроматический и выше
    chromatic_idx = config.RARITY_ORDER.index("Хроматический")
    phone_idx = config.RARITY_ORDER.index(phone["rarity"]) if phone["rarity"] in config.RARITY_ORDER else -1
    if phone_idx >= chromatic_idx and random.randint(1, 100) <= 50:
        await db.add_meteorites(user_id, 1)
        await message.answer("☄️ Бонус! Вам выпал метеорит за высокую редкость телефона.")

    # Награда за реферала — раньше завязана на "это первый телефон вообще",
    # что могло не сработать, если игрок получил телефон из другого
    # источника (промокод/ежедневка) раньше своего первого "тел". Теперь
    # используем отдельный флаг именно для факта "первый тел когда-либо".
    is_first_tel_ever = not await db.get_first_tel_done(user_id)
    await db.mark_first_tel_done(user_id)

    if is_first_tel_ever:
        referrer_id = await db.get_referrer(user_id)
        if referrer_id and not await db.is_referral_rewarded(user_id):
            await db.reset_cooldown(referrer_id)
            await db.set_luck_boost(referrer_id, percent=50, pulls=3)
            await db.add_balance(user_id, 150)
            await db.set_luck_boost(user_id, percent=50, pulls=3)
            await db.mark_referral_rewarded(user_id)

            await message.answer(
                "🎁 Вам начислено 150₽ и буст удачи +50% на следующие 3 «тел» "
                "за переход по реферальной ссылке!"
            )
            try:
                await bot.send_message(
                    referrer_id,
                    "🎉 Ваш друг присоединился и получил первый телефон!\n"
                    "Вам начислено: сброс кулдауна + буст удачи +50% на следующие 3 «тел».",
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить пригласившего {referrer_id}: {e}")


DAILY_REWARDS = {
    1: {"type": "rub", "amount": 5000},
    2: {"type": "rub", "amount": 6000},
    3: {"type": "rub", "amount": 7500},
    4: {"type": "rub", "amount": 9000},
    5: {"type": "rub", "amount": 11000},
    6: {"type": "rub", "amount": 13500},
    7: {"type": "rub", "amount": 16000},
    8: {"type": "rub", "amount": 20000},
    9: {"type": "rub", "amount": 25000},
    10: {"type": "rub", "amount": 30000},
    11: {"type": "meteor", "amount": 1},
    12: {"type": "rub", "amount": 40000},
    13: {"type": "meteor", "amount": 2},
    14: {"type": "phone_rarity", "rarity": "Космический"},
}


@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    await handle_daily(message)


async def handle_daily(message: Message):
    user_id = message.from_user.id
    await db.ensure_user(user_id, display_username(message.from_user))

    current_day, last_claim = await db.get_daily_status(user_id)
    now = time.time()

    if last_claim:
        elapsed = now - last_claim
        if elapsed < 24 * 3600:
            remaining = 24 * 3600 - elapsed
            await message.answer(
                f"⏳ Сегодняшняя ежедневка уже забрана. Приходи через {format_seconds(int(remaining))}."
            )
            return
        elif elapsed < 48 * 3600:
            next_day = current_day + 1 if current_day < 14 else 1
        else:
            next_day = 1  # не успел забрать вовремя — цикл сбрасывается
    else:
        next_day = 1

    reward = DAILY_REWARDS[next_day]
    await db.set_daily_progress(user_id, next_day, now)

    header = f"📅 Ежедневный бонус — день {next_day}/14!"

    if reward["type"] == "rub":
        await db.add_balance(user_id, reward["amount"])
        await message.answer(f"{header}\n\n💰 Получено: {reward['amount']}₽")

    elif reward["type"] == "meteor":
        await db.add_meteorites(user_id, reward["amount"])
        await message.answer(f"{header}\n\n☄️ Получено: {reward['amount']} метеорит(ов)")

    elif reward["type"] == "phone_rarity":
        candidates = await db.get_phones_by_rarity(reward["rarity"])
        if not candidates:
            # в каталоге пока нет телефонов такой редкости — не оставляем
            # игрока ни с чем, компенсируем крупной суммой
            await db.add_balance(user_id, 100000)
            await message.answer(
                f"{header}\n\n"
                "В каталоге пока нет телефонов редкости Космический — "
                "начислено 100000₽ в качестве компенсации."
            )
            return

        phone = random.choice(candidates)
        status, price, delta = roll_status_and_price(phone["base_price"])
        await db.record_pull(user_id, phone["id"], status, price, phone["rarity"])
        total_count = await db.get_user_phone_count(user_id)

        caption = format_phone_card(
            username=display_username(message.from_user),
            model=phone["model"],
            status=status,
            rarity=phone["rarity"],
            base_price=phone["base_price"],
            delta=delta,
            obtained_ts=now,
            added_note=True,
            total_count=total_count,
        )

        await message.answer(f"{header}\n\n🎉 Финальный день — топовый телефон!")
        if phone["photo_file_id"]:
            await message.answer_photo(phone["photo_file_id"], caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption + "\n\nФото телефона не было найдено.", parse_mode="HTML")


async def send_inventory_rarities(message: Message, mode: str = "sell"):
    rarities = await db.get_user_owned_rarities(message.from_user.id)
    if not rarities:
        await message.answer(
            "В вашем инвентаре пока пусто. Напишите «тел», чтобы получить первый телефон!"
        )
        return
    title = "📦 Выберите редкость:" if mode == "sell" else "🔧 Выберите редкость (апгрейд):"
    await message.answer(title, reply_markup=build_rarity_keyboard(rarities, mode))


def build_rarity_keyboard(rarities: dict, mode: str):
    kb = InlineKeyboardBuilder()
    for rarity in config.RARITY_ORDER:
        if rarity in rarities:
            emoji = RARITY_EMOJI.get(rarity, "")
            kb.button(
                text=f"{emoji} {rarity} ({rarities[rarity]})",
                callback_data=f"inv_rarity:{mode}:{rarity}",
            )
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("inv_rarity:"))
async def cb_inventory_rarity(callback: CallbackQuery):
    _, mode, rarity = callback.data.split(":", 2)
    items = await db.get_user_phones_by_rarity(callback.from_user.id, rarity)
    if not items:
        await callback.answer("Тут пока пусто", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for item in items:
        time_str = format_msk_time(item["obtained_at"], short=True)
        rank_tag = f" [{item['rank']}]" if item.get("rank") else ""
        kb.button(
            text=f"{item['model']} ({time_str}){rank_tag}",
            callback_data=f"inv_item:{mode}:{item['up_id']}",
        )
    kb.button(text="⬅️ Назад", callback_data=f"inv_back:{mode}")
    kb.adjust(1)

    emoji = RARITY_EMOJI.get(rarity, "")
    await callback.message.edit_text(
        f"{emoji} {rarity} — ваши телефоны:", reply_markup=kb.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("inv_back:"))
async def cb_inventory_back(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    rarities = await db.get_user_owned_rarities(callback.from_user.id)
    title = "📦 Выберите редкость:" if mode == "sell" else "🔧 Выберите редкость (апгрейд):"
    await callback.message.edit_text(title, reply_markup=build_rarity_keyboard(rarities, mode))
    await callback.answer()


@dp.callback_query(F.data.startswith("inv_item:"))
async def cb_inventory_item(callback: CallbackQuery):
    _, mode, up_id_str = callback.data.split(":", 2)
    up_id = int(up_id_str)
    item = await db.get_user_phone_item(up_id)

    if item is None or item["user_id"] != callback.from_user.id:
        await callback.answer("Не найдено", show_alert=True)
        return

    caption = format_phone_card(
        username=display_username(callback.from_user),
        model=item["model"],
        status=item["status"],
        rarity=item["rarity"],
        base_price=item["base_price"],
        delta=0,
        current_price=item["price"],
        rank=item["rank"],
        obtained_ts=item["obtained_at"],
        added_note=False,
    )

    if mode == "sell":
        kb = InlineKeyboardBuilder()
        kb.button(text="💰 Продать телефон", callback_data=f"sell_item:{up_id}")
        markup = kb.as_markup()
    else:  # upgrade
        kb = InlineKeyboardBuilder()
        kb.button(text="🔧 Обновить", callback_data=f"upgrade_item:{up_id}")
        markup = kb.as_markup()

    if item["photo_file_id"]:
        await callback.message.answer_photo(
            item["photo_file_id"], caption=caption, parse_mode="HTML", reply_markup=markup
        )
    else:
        await callback.message.answer(
            caption + "\n\nФото телефона не было найдено.",
            parse_mode="HTML", reply_markup=markup,
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("sell_item:"))
async def cb_sell_item(callback: CallbackQuery):
    """Первый клик — только просим подтверждение, ничего не продаём сразу."""
    up_id = int(callback.data.split(":", 1)[1])
    item = await db.get_user_phone_item(up_id)

    if item is None or item["user_id"] != callback.from_user.id:
        await callback.answer("Телефон не найден", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, продать", callback_data=f"sell_confirm:{up_id}")
    kb.button(text="❌ Отмена", callback_data=f"sell_cancel:{up_id}")
    kb.adjust(2)
    await callback.message.answer(
        f"Точно продать {item['model']} за {item['price']}₽?",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("sell_confirm:"))
async def cb_sell_confirm(callback: CallbackQuery):
    up_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id
    item = await db.get_user_phone_item(up_id)

    if item is None or item["user_id"] != user_id:
        await callback.answer("Телефон не найден", show_alert=True)
        return

    sell_price = item["price"]
    event = await db.get_active_event()
    bonus_note = ""
    if event and event["event_type"] == "sell_bonus":
        sell_price = round(sell_price * (1 + event["percent"] / 100))
        bonus_note = f" (учтён ивент +{event['percent']:.0f}%)"

    await db.delete_user_phone(up_id)
    await db.add_balance(user_id, sell_price)
    await db.increment_phones_sold(user_id)

    sold_text = f"💰 Телефон продан за {sell_price}₽{bonus_note}. Деньги начислены на баланс."
    await callback.message.edit_text(sold_text)
    await callback.answer()


@dp.callback_query(F.data.startswith("sell_cancel:"))
async def cb_sell_cancel(callback: CallbackQuery):
    await callback.message.edit_text("Продажа отменена.")
    await callback.answer()


@dp.callback_query(F.data.startswith("upgrade_item:"))
async def cb_upgrade_prompt(callback: CallbackQuery):
    up_id = int(callback.data.split(":", 1)[1])
    item = await db.get_user_phone_item(up_id)
    if item is None or item["user_id"] != callback.from_user.id:
        await callback.answer("Телефон не найден", show_alert=True)
        return

    meteorites = await db.get_meteorites(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💰 Заплатить {UPGRADE_COST_RUB}₽", callback_data=f"upgrade_pay:rub:{up_id}")
    if meteorites > 0:
        kb.button(
            text=f"☄️ Заплатить 1 метеоритом (у вас {meteorites})",
            callback_data=f"upgrade_pay:meteor:{up_id}",
        )
    kb.adjust(1)
    await callback.message.answer("Выберите способ оплаты апгрейда:", reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("upgrade_pay:"))
async def cb_upgrade_pay(callback: CallbackQuery):
    _, method, up_id_str = callback.data.split(":")
    up_id = int(up_id_str)
    user_id = callback.from_user.id

    item = await db.get_user_phone_item(up_id)
    if item is None or item["user_id"] != user_id:
        await callback.answer("Телефон не найден", show_alert=True)
        return

    if method == "rub":
        balance = await db.get_balance(user_id)
        if balance < UPGRADE_COST_RUB:
            await callback.answer("Недостаточно рублей на балансе.", show_alert=True)
            return
        await db.add_balance(user_id, -UPGRADE_COST_RUB)
    else:
        meteorites = await db.get_meteorites(user_id)
        if meteorites < 1:
            await callback.answer("У вас нет метеоритов.", show_alert=True)
            return
        await db.add_meteorites(user_id, -1)

    rank, multiplier = roll_upgrade_rank(item["rarity"])
    new_price = round(item["price"] * multiplier)
    new_status = item["status"]
    if rank in RANKS_THAT_FIX_STATUS and item["status"] == "Б/У":
        new_status = "Новый"

    await db.apply_upgrade(up_id, new_price, new_status, rank)
    if rank != "F":
        await db.increment_upgrades_successful(user_id)

    result_lines = [
        f"🎲 Результат апгрейда: <b>РАНГ {rank}</b>",
        f"💰 Новая цена: {new_price}₽",
    ]
    if new_status != item["status"]:
        result_lines.append(f"✨ Статус обновлён до: {new_status}")

    await callback.message.answer("\n".join(result_lines), parse_mode="HTML")
    await callback.answer()


# ---------- ПРОМОКОДЫ ----------

@dp.message(Command("addtester"))
async def cmd_add_tester(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) not in (2, 3) or not parts[1].isdigit():
        await message.answer(
            "Использование:\n"
            "/addtester USER_ID — сделать тестером (испытательный срок уже пройден)\n"
            "/addtester USER_ID исп — сделать тестером с пометкой «испытательный срок»"
        )
        return

    target_id = int(parts[1])
    probation = len(parts) == 3 and parts[2].lower() == "исп"

    await db.ensure_user(target_id, None)
    await db.set_tester(target_id, probation)

    label = " (испытательный срок)" if probation else ""
    await message.answer(f"✅ Пользователь {target_id} теперь тестер{label}.")


@dp.message(Command("tester"))
async def cmd_tester_update(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2].lower() != "update":
        await message.answer("Использование: /tester USER_ID update — убирает пометку «испытательный срок».")
        return

    target_id = int(parts[1])
    ok = await db.update_tester_probation(target_id, False)
    if not ok:
        await message.answer("Этот пользователь не является тестером.")
        return

    await message.answer(f"✅ У пользователя {target_id} убрана пометка «испытательный срок».")


@dp.message(Command("addpromo"))
async def cmd_add_promo(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) != 4:
        await message.answer(
            "Использование:\n"
            "/addpromo КОД НАГРАДА КОЛ-ВО_АКТИВАЦИЙ\n\n"
            "Варианты награды:\n"
            "• rub:500 — рубли на баланс\n"
            "• phone:12 — конкретный телефон (ID из /listphones)\n"
            "• resetcd — сброс кулдауна на «тел»\n"
            "• luck:40 — буст удачи +40% на следующий «тел» (10-70)\n"
            "• statusfix — починка статуса Б/У → Новый (только в личке)\n"
            "• meteor:1 — метеориты (валюта для апгрейда)\n\n"
            "Пример: /addpromo u0MWmHTUhSp1H rub:500 100"
        )
        return

    _, code, reward_raw, activations_raw = parts

    if not activations_raw.isdigit() or int(activations_raw) <= 0:
        await message.answer("Количество активаций должно быть положительным числом.")
        return
    max_activations = int(activations_raw)

    reward_type, _, reward_value = reward_raw.partition(":")
    reward_type = reward_type.lower()

    if reward_type == "rub":
        if not reward_value.isdigit():
            await message.answer("Для rub укажите сумму, например rub:500")
            return
    elif reward_type == "phone":
        if not reward_value.isdigit():
            await message.answer("Для phone укажите ID телефона, например phone:12")
            return
        phone = await db.get_phone(int(reward_value))
        if phone is None:
            await message.answer(f"Телефон с ID {reward_value} не найден. Проверьте /listphones")
            return
    elif reward_type == "resetcd":
        reward_value = ""
    elif reward_type == "luck":
        if not reward_value.isdigit() or not (10 <= int(reward_value) <= 70):
            await message.answer("Для luck укажите число от 10 до 70, например luck:40")
            return
    elif reward_type == "statusfix":
        reward_value = ""
    elif reward_type == "meteor":
        if not reward_value.isdigit() or int(reward_value) <= 0:
            await message.answer("Для meteor укажите положительное число, например meteor:1")
            return
    else:
        await message.answer(
            "Неизвестный тип награды. Доступные: rub, phone, resetcd, luck, statusfix, meteor"
        )
        return

    existing = await db.get_promo_by_code(code)
    if existing is not None:
        await message.answer("Промокод с таким названием уже существует.")
        return

    await db.add_promo(code, reward_type, reward_value, max_activations)
    await message.answer(
        f"✅ Промокод «{code}» создан ({reward_raw}), активаций: {max_activations}"
    )


@dp.message(Command("promo"))
async def cmd_promo(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /promo КОД")
        return
    code = parts[1].strip()

    promo = await db.get_promo_by_code(code)
    if promo is None:
        await message.answer("❌ Промокод не найден.")
        return

    user_id = message.from_user.id
    if await db.has_user_activated(promo["id"], user_id):
        await message.answer("❌ Вы уже активировали этот промокод.")
        return

    used = await db.get_promo_activation_count(promo["id"])
    if used >= promo["max_activations"]:
        await message.answer("❌ Лимит активаций этого промокода исчерпан.")
        return

    reward_type = promo["reward_type"]
    reward_value = promo["reward_value"]

    await db.ensure_user(user_id, display_username(message.from_user))

    if reward_type == "resetcd":
        remaining = await db.seconds_until_ready(user_id)
        if remaining <= 0:
            await message.answer(
                "❌ У вас сейчас нет активной перезарядки — нечего сбрасывать. "
                "Сначала напишите «тел», а этот промокод сохраните на потом."
            )
            return

    if reward_type == "statusfix":
        if message.chat.type != "private":
            await message.answer(
                "❌ Этот промокод можно активировать только в личных сообщениях боту."
            )
            return
        used_phones = await db.get_user_used_phones(user_id)
        if not used_phones:
            await message.answer("У вас нет телефонов со статусом Б/У, которые можно починить.")
            return

        kb = InlineKeyboardBuilder()
        for item in used_phones:
            time_str = format_msk_time(item["obtained_at"], short=True)
            kb.button(
                text=f"{item['model']} ({time_str})",
                callback_data=f"promofix:{promo['id']}:{item['up_id']}",
            )
        kb.adjust(1)
        await message.answer("Выберите телефон для починки статуса:", reply_markup=kb.as_markup())
        return

    await _grant_promo_reward(message, reward_type, reward_value)
    await db.record_promo_activation(promo["id"], user_id)


async def _grant_promo_reward(message: Message, reward_type: str, reward_value: str):
    user_id = message.from_user.id

    if reward_type == "rub":
        amount = int(reward_value)
        await db.add_balance(user_id, amount)
        await message.answer(f"💰 Получено: {amount}₽ на баланс")

    elif reward_type == "phone":
        phone = await db.get_phone(int(reward_value))
        if phone is None:
            await message.answer("❌ Телефон из этого промокода больше недоступен.")
            return
        status, price, delta = roll_status_and_price(phone["base_price"])
        await db.record_pull(user_id, phone["id"], status, price, phone["rarity"])
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
            await message.answer_photo(phone["photo_file_id"], caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption + "\n\nФото телефона не было найдено.", parse_mode="HTML")

    elif reward_type == "resetcd":
        await db.reset_cooldown(user_id)
        await message.answer("⏳ Перезарядка на «тел» сброшена!")

    elif reward_type == "luck":
        percent = int(reward_value)
        await db.set_luck_boost(user_id, percent)
        await message.answer(f"🍀 Получен буст удачи +{percent}% на следующий «тел»!")

    elif reward_type == "meteor":
        amount = int(reward_value) if reward_value else 1
        await db.add_meteorites(user_id, amount)
        word = "метеорит" if amount == 1 else "метеорита(ов)"
        await message.answer(f"☄️ Получено: {amount} {word}!")


@dp.callback_query(F.data.startswith("promofix:"))
async def cb_promo_fix(callback: CallbackQuery):
    _, promo_id_str, up_id_str = callback.data.split(":")
    promo_id = int(promo_id_str)
    up_id = int(up_id_str)
    user_id = callback.from_user.id

    if await db.has_user_activated(promo_id, user_id):
        await callback.answer("Вы уже активировали этот промокод.", show_alert=True)
        return

    promo = await db.get_promo(promo_id)
    if promo is None:
        await callback.answer("Промокод не найден.", show_alert=True)
        return

    used = await db.get_promo_activation_count(promo_id)
    if used >= promo["max_activations"]:
        await callback.answer("Лимит активаций исчерпан.", show_alert=True)
        return

    item = await db.get_user_phone_item(up_id)
    if item is None or item["user_id"] != user_id:
        await callback.answer("Телефон не найден.", show_alert=True)
        return
    if item["status"] != "Б/У":
        await callback.answer("Этот телефон уже не Б/У.", show_alert=True)
        return

    new_price = item["base_price"]
    await db.fix_phone_status(up_id, new_price)
    await db.record_promo_activation(promo_id, user_id)

    await callback.message.edit_text(
        f"🔧 Статус обновлён: {item['model']} теперь Новый (цена {new_price}₽ (+0₽))"
    )
    await callback.answer()


PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Помощь и информация о боте"),
    BotCommand(command="tel", description="Получить случайный телефон (аналог слова «тел»)"),
    BotCommand(command="inventory", description="Инвентарь телефонов (аналог слова «инв»)"),
    BotCommand(command="update", description="Апгрейд телефона (аналог слова «апдейт»)"),
    BotCommand(command="daily", description="Ежедневный бонус (аналог слова «ежедневка»)"),
    BotCommand(command="profile", description="Профиль (аналог фразы «мой акк»)"),
    BotCommand(command="promo", description="Активировать промокод"),
    BotCommand(command="ref", description="Получить реферальную ссылку"),
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand(command="addphone", description="Добавить телефон в каталог"),
    BotCommand(command="delphone", description="Удалить телефон из каталога"),
    BotCommand(command="listphones", description="Список всех телефонов с ID"),
    BotCommand(command="addpromo", description="Создать промокод"),
    BotCommand(command="addevent", description="Запустить глобальный ивент"),
]


async def setup_commands():
    await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logging.warning(f"Не удалось задать меню команд для админа {admin_id}: {e}")


# ---------- ИВЕНТЫ ----------

EVENT_LABELS = {
    "luck": ("🍀 НАЧАЛСЯ ГЛОБАЛЬНЫЙ ИВЕНТ УДАЧИ!", 'Любые ваши карточки с помощью "тел" теперь имеют буст к удаче +{percent:.0f}%'),
    "sell_bonus": ("💰 НАЧАЛСЯ ГЛОБАЛЬНЫЙ ИВЕНТ ПРОДАЖ!", "Продажа телефонов теперь приносит +{percent:.0f}% к итоговой цене!"),
}


async def start_event(event_type: str, percent: float, duration_seconds: int, created_by: str):
    await db.create_event(event_type, percent, duration_seconds, created_by)
    ends_at = time.time() + duration_seconds
    title, body_template = EVENT_LABELS.get(event_type, (f"НАЧАЛСЯ ИВЕНТ: {event_type}", ""))
    text = (
        f"{title}\n\n"
        f"{body_template.format(percent=percent)}\n\n"
        f"Ивент заканчивается в: {format_msk_time(ends_at)}\n"
        f"Создано {created_by}."
    )
    await broadcast_message(text)


async def broadcast_message(text: str):
    user_ids = await db.get_all_user_ids()
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
        except Exception as e:
            logging.warning(f"Не удалось отправить рассылку пользователю {uid}: {e}")
        await asyncio.sleep(0.05)  # не превысить лимиты Telegram на частоту сообщений


@dp.message(Command("addevent"))
async def cmd_add_event(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) != 3 or parts[1] not in ("luck", "sell_bonus") or not parts[2].isdigit():
        await message.answer(
            "Использование:\n"
            "/addevent luck МИНУТЫ — ивент удачи (+50% к «тел»)\n"
            "/addevent sell_bonus МИНУТЫ — ивент продаж (+30% к цене продажи)\n\n"
            "Пример: /addevent luck 180"
        )
        return

    event_type = parts[1]
    duration_minutes = int(parts[2])
    percent = 50 if event_type == "luck" else 30

    await start_event(event_type, percent, duration_minutes * 60, created_by="Админ")
    await message.answer(f"✅ Ивент «{event_type}» запущен на {duration_minutes} минут.")


async def event_scheduler():
    """Фоновая задача — раз в случайное время сама запускает глобальный
    ивент, если сейчас никакой не идёт."""
    while True:
        active = await db.get_active_event()
        if active:
            wait_seconds = max(10, active["ends_at"] - time.time())
            await asyncio.sleep(wait_seconds)
            continue

        # Пауза между ивентами — случайная, от 2 до 12 часов
        gap_seconds = random.uniform(2 * 3600, 12 * 3600)
        await asyncio.sleep(gap_seconds)

        event_type = random.choice(["luck", "sell_bonus"])
        percent = 50 if event_type == "luck" else 30
        # Длительность 1-9 часов, шаг 10 минут
        duration_minutes = random.choice(range(60, 541, 10))

        await start_event(event_type, percent, duration_minutes * 60, created_by="Бот")


async def main():
    global BOT_USERNAME
    await db.init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await setup_commands()
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot),
        event_scheduler(),
    )


if __name__ == "__main__":
    asyncio.run(main())
