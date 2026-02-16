import os
import asyncio
import threading
import logging

from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = {8243127223, 6334413055}

logging.basicConfig(level=logging.INFO)

# ================= FLASK =================
app = Flask(__name__)

@app.get("/")
def index():
    return "ok", 200

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= BOT =================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# хранит кому админ отвечает
admin_reply_mode = {}

# ================= КНОПКИ =================

def start_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="1) Насчет рекламы", callback_data="topic:ads")
    kb.button(text="2) Замечена ошибка", callback_data="topic:bug")
    kb.button(text="3) Другое", callback_data="topic:other")
    kb.adjust(1)
    return kb.as_markup()

def admin_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Ответить", callback_data=f"reply:{user_id}")
    kb.button(text="🚫 Бан", callback_data=f"ban:{user_id}")
    kb.adjust(2)
    return kb.as_markup()

# ================= СТАРТ =================

@dp.message(F.text == "/start")
async def start(m: Message):
    await m.answer("Выбери тему 👇", reply_markup=start_kb())

# ================= ВЫБОР ТЕМЫ =================

@dp.callback_query(F.data.startswith("topic:"))
async def choose_topic(cb: CallbackQuery):
    await cb.message.answer("Напиши сообщение — я передам админам.")
    await cb.answer()

# ================= ОТПРАВКА ЖАЛОБ =================

@dp.message()
async def forward_to_admins(m: Message):

    if not m.from_user:
        return

    # если пишет админ — это ответ пользователю
    if m.from_user.id in ADMINS:
        target = admin_reply_mode.get(m.from_user.id)
        if target:
            try:
                await bot.send_message(target, m.text)
                await m.answer("✅ Отправлено пользователю")
            except:
                await m.answer("❌ Не удалось отправить")
            admin_reply_mode.pop(m.from_user.id, None)
        return

    # обычный пользователь
    user = m.from_user
    username = f"@{user.username}" if user.username else "без username"

    header = (
        "📩 Новое сообщение\n\n"
        f"👤 {username}\n"
        f"🆔 {user.id}"
    )

    for admin in ADMINS:
        try:
            await bot.send_message(
                admin,
                header,
                reply_markup=admin_kb(user.id)
            )

            await m.copy_to(admin)

        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")

    await m.answer("✅ Отправлено админам")

# ================= КНОПКИ АДМИНА =================

@dp.callback_query(F.data.startswith("reply:"))
async def admin_reply(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return

    user_id = int(cb.data.split(":")[1])
    admin_reply_mode[cb.from_user.id] = user_id

    await cb.answer("Напиши сообщение — отправлю пользователю", show_alert=True)

@dp.callback_query(F.data.startswith("ban:"))
async def admin_ban(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return

    user_id = int(cb.data.split(":")[1])

    try:
        await bot.send_message(user_id, "🚫 Вы были заблокированы администратором.")
    except:
        pass

    await cb.answer("Пользователь заблокирован", show_alert=True)

# ================= RUN =================

async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
