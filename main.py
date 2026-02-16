import os
import re
import threading
import logging
from datetime import datetime

from flask import Flask

import psycopg2
import psycopg2.extras

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8588762559:AAFWqAeXCFZzexak4-Shey_yXVtSaGdqoos")
DATABASE_URL = os.getenv("DATABASE_URL")  # Supabase Postgres connection string

ADMINS = {6334413055, 8243127223}

logging.basicConfig(level=logging.INFO)

# ===================== FLASK (Render healthcheck) =====================
app = Flask(__name__)

@app.get("/")
def index():
    return "ok", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ===================== DB (Supabase Postgres) =====================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Add it in Render env (Supabase Postgres URI).")

    # Render env иногда сохраняет переносы/пробелы в конце — чистим
    dsn = (DATABASE_URL or "").strip()

    # На всякий: если в конце случайно есть пробелы/переносы после dbname
    # и если dbname в url сломан — зададим явно
    # psycopg2 умеет принимать и URL, и "key=value" параметры
    return psycopg2.connect(dsn, sslmode="require", dbname="postgres")

def init_db():
    with db_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    reason TEXT,
                    banned_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_map (
                    admin_chat_id BIGINT NOT NULL,
                    admin_msg_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    topic TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (admin_chat_id, admin_msg_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_topic (
                    user_id BIGINT PRIMARY KEY,
                    topic TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
            """)

def is_banned(user_id: int) -> bool:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM bans WHERE user_id=%s", (user_id,))
        return cur.fetchone() is not None

def upsert_ban(user_id: int, username: str | None, reason: str | None):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO bans(user_id, username, reason, banned_at)
            VALUES(%s,%s,%s,now())
            ON CONFLICT (user_id) DO UPDATE SET
                username=EXCLUDED.username,
                reason=EXCLUDED.reason,
                banned_at=now()
        """, (user_id, username, reason))

def remove_ban(user_id: int):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM bans WHERE user_id=%s", (user_id,))

def set_topic(user_id: int, topic: str):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_topic(user_id, topic, updated_at)
            VALUES(%s,%s,now())
            ON CONFLICT (user_id) DO UPDATE SET
                topic=EXCLUDED.topic,
                updated_at=now()
        """, (user_id, topic))

def get_topic(user_id: int) -> str | None:
    with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT topic FROM user_topic WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        return row["topic"] if row else None

def save_admin_map(admin_chat_id: int, admin_msg_id: int, user_id: int, topic: str | None):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO admin_map(admin_chat_id, admin_msg_id, user_id, topic, created_at)
            VALUES(%s,%s,%s,%s,now())
            ON CONFLICT (admin_chat_id, admin_msg_id) DO UPDATE SET
                user_id=EXCLUDED.user_id,
                topic=EXCLUDED.topic,
                created_at=now()
        """, (admin_chat_id, admin_msg_id, user_id, topic))

def get_mapped_user(admin_chat_id: int, admin_msg_id: int) -> int | None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT user_id FROM admin_map
            WHERE admin_chat_id=%s AND admin_msg_id=%s
        """, (admin_chat_id, admin_msg_id))
        row = cur.fetchone()
        return int(row[0]) if row else None

# ===================== BOT =====================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# админ нажал "Ответить" -> сюда кладём, кому следующую месагу отправлять (в памяти)
admin_reply_target: dict[int, int] = {}

class UserFlow(StatesGroup):
    choosing = State()
    chatting = State()

def start_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="1) Насчет рекламы", callback_data="topic:ads")
    kb.button(text="2) Замечена ошибка", callback_data="topic:bug")
    kb.button(text="3) Другое", callback_data="topic:other")
    kb.adjust(1)
    return kb.as_markup()

def topic_name(code: str) -> str:
    return {"ads": "Насчет рекламы", "bug": "Замечена ошибка", "other": "Другое"}.get(code, code)

def admin_only(user_id: int) -> bool:
    return user_id in ADMINS

def admin_actions_kb(user_id: int, banned: bool):
    kb = InlineKeyboardBuilder()
    if banned:
        kb.button(text="✅ Разбан", callback_data=f"admin:unban:{user_id}")
    else:
        kb.button(text="🚫 Бан", callback_data=f"admin:ban:{user_id}")
    kb.button(text="✍️ Ответить", callback_data=f"admin:reply:{user_id}")
    kb.adjust(2)
    return kb.as_markup()

def user_tag(m: Message) -> str:
    u = m.from_user
    username = f"@{u.username}" if u.username else "(без username)"
    return f"{username} | id={u.id} | {u.full_name}"

@dp.message(CommandStart())
async def on_start(m: Message, state: FSMContext):
    if not m.from_user:
        return
    if is_banned(m.from_user.id):
        return
    await state.set_state(UserFlow.choosing)
    await m.answer("Привет! Выбери, по какому вопросу пишешь 👇", reply_markup=start_kb())

@dp.callback_query(F.data.startswith("topic:"))
async def on_topic(cb: CallbackQuery, state: FSMContext):
    if not cb.from_user:
        return
    if is_banned(cb.from_user.id):
        await cb.answer("Вы заблокированы.", show_alert=True)
        return

    code = cb.data.split(":", 1)[1].strip()
    set_topic(cb.from_user.id, code)
    await state.set_state(UserFlow.chatting)
    await cb.message.answer(
        f"Ок, тема: **{topic_name(code)}**.\n\nПиши сообщение(я) — я отправлю админам.\nЧтобы снова выбрать тему: /start",
        parse_mode="Markdown"
    )
    await cb.answer()

@dp.message(UserFlow.chatting, F.any())
async def user_message(m: Message, state: FSMContext):
    if not m.from_user:
        return
    if is_banned(m.from_user.id):
        return

    topic = get_topic(m.from_user.id) or "unknown"

    header = (
        "📩 **Новое сообщение**\n"
        f"Тема: **{topic_name(topic)}**\n"
        f"От: `{user_tag(m)}`\n\n"
        "— — —\n"
        "🛠 **Админ-панель:** кнопки ниже (Ответить / Бан)"
    )

    for admin_id in ADMINS:
        try:
            banned_flag = is_banned(m.from_user.id)

            sent_header = await bot.send_message(
                admin_id,
                header,
                parse_mode="Markdown",
                reply_markup=admin_actions_kb(m.from_user.id, banned_flag)
            )
            save_admin_map(admin_id, sent_header.message_id, m.from_user.id, topic)

            # Копируем сообщение юзера ОТВЕТОМ на заголовок с кнопками
            copied = await m.copy_to(admin_id, reply_to_message_id=sent_header.message_id)
            save_admin_map(admin_id, copied.message_id, m.from_user.id, topic)

        except Exception as e:
            logging.exception(f"Failed to send to admin {admin_id}: {e}")

    await m.answer("✅ Принято. Жди ответа админа.")

# ======= ADMIN CALLBACKS (inline buttons) =======
@dp.callback_query(F.data.startswith("admin:"))
async def admin_cb(cb: CallbackQuery):
    if not cb.from_user:
        return
    if not admin_only(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return

    try:
        _, action, uid_str = cb.data.split(":", 2)
        target_uid = int(uid_str)
    except Exception:
        await cb.answer("Ошибка данных кнопки.", show_alert=True)
        return

    if action == "ban":
        # username попробуем взять из чата
        username = None
        try:
            chat = await bot.get_chat(target_uid)
            if getattr(chat, "username", None):
                username = f"@{chat.username}"
        except Exception:
            pass

        upsert_ban(target_uid, username, "banned by admin button")
        # обновим кнопки в сообщении, если возможно
        try:
            await cb.message.edit_reply_markup(reply_markup=admin_actions_kb(target_uid, True))
        except Exception:
            pass
        await cb.answer("🚫 Забанен")

    elif action == "unban":
        remove_ban(target_uid)
        try:
            await cb.message.edit_reply_markup(reply_markup=admin_actions_kb(target_uid, False))
        except Exception:
            pass
        await cb.answer("✅ Разбанен")

    elif action == "reply":
        admin_reply_target[cb.from_user.id] = target_uid
        await cb.answer("Ок. Напиши следующее сообщение — я отправлю пользователю.", show_alert=True)

    else:
        await cb.answer("Неизвестное действие.", show_alert=True)

# ======= ADMIN: reply-by-reply (как раньше) =======
@dp.message(F.reply_to_message)
async def admin_reply_router(m: Message):
    if not m.from_user:
        return
    if not admin_only(m.from_user.id):
        return
    if not m.reply_to_message:
        return

    user_id = get_mapped_user(m.chat.id, m.reply_to_message.message_id)
    if not user_id:
        return

    if is_banned(user_id):
        await m.answer("Этот пользователь забанен — ответ не отправлен.")
        return

    try:
        await m.copy_to(user_id)
        await m.answer("✅ Ответ отправлен пользователю.")
    except Exception:
        await m.answer("❌ Не смог отправить (возможно, пользователь заблокировал бота).")

# ======= ADMIN: "Ответить" кнопкой (без reply) =======
@dp.message(F.any())
async def admin_send_without_reply(m: Message):
    if not m.from_user:
        return
    if not admin_only(m.from_user.id):
        return

    target_uid = admin_reply_target.get(m.from_user.id)
    if not target_uid:
        return  # не в режиме "ответить кнопкой"

    if is_banned(target_uid):
        admin_reply_target.pop(m.from_user.id, None)
        await m.answer("Пользователь забанен — не отправил.")
        return

    try:
        await m.copy_to(target_uid)
        await m.answer("✅ Отправил пользователю. (Режим ответа выключен)")
    except Exception:
        await m.answer("❌ Не смог отправить (возможно, пользователь заблокировал бота).")
    finally:
        admin_reply_target.pop(m.from_user.id, None)

async def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
