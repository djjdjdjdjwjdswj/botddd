import os
import threading
import logging

from flask import Flask

import psycopg2
import psycopg2.extras

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import BaseMiddleware

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

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
        raise RuntimeError("DATABASE_URL is not set. Add it in Render env.")
    dsn = (DATABASE_URL or "").strip()
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
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it in Render env.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

admin_reply_target: dict[int, int] = {}

class UserFlow(StatesGroup):
    choosing = State()
    chatting = State()

def topic_name(code: str) -> str:
    return {"ads": "Насчет рекламы", "bug": "Замечена ошибка", "other": "Другое"}.get(code, code)

def start_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="1) Насчет рекламы", callback_data="topic:ads")
    kb.button(text="2) Замечена ошибка", callback_data="topic:bug")
    kb.button(text="3) Другое", callback_data="topic:other")
    kb.adjust(1)
    return kb.as_markup()

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
    username = f"@{u.username}" if u and u.username else "(без username)"
    full_name = u.full_name if u else "unknown"
    uid = u.id if u else 0
    return f"{username} | id={uid} | {full_name}"

# ===================== LOG MIDDLEWARE (пишет в Render любой апдейт) =====================
class LogAllUpdatesMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, Message) and event.from_user:
                txt = event.text if event.text is not None else f"<{event.content_type}>"
                logging.info(f"IN MSG chat={event.chat.id} from={event.from_user.id} username={event.from_user.username} text={txt[:200]}")
            elif isinstance(event, CallbackQuery) and event.from_user:
                logging.info(f"IN CB from={event.from_user.id} username={event.from_user.username} data={event.data}")
        except Exception:
            pass
        return await handler(event, data)

dp.update.middleware(LogAllUpdatesMiddleware())

# ===================== DEBUG COMMANDS =====================
@dp.message(F.text == "/ping")
async def ping(m: Message):
    await m.answer("pong ✅")

@dp.message(F.text == "/whoami")
async def whoami(m: Message):
    if not m.from_user:
        return
    uid = m.from_user.id
    uname = f"@{m.from_user.username}" if m.from_user.username else "(без username)"
    is_admin = uid in ADMINS
    await m.answer(f"Ваш id: {uid}\nusername: {uname}\nadmin: {is_admin}")

@dp.message(F.text == "/testadmins")
async def testadmins(m: Message):
    if not m.from_user:
        return
    if m.from_user.id not in ADMINS:
        await m.answer("Нет доступа.")
        return

    ok = []
    bad = []
    for aid in ADMINS:
        try:
            await bot.send_message(aid, f"✅ Тест: бот может писать админу {aid}")
            ok.append(str(aid))
        except Exception as e:
            bad.append(f"{aid} ({type(e).__name__}: {e})")

    await m.answer("OK: " + (", ".join(ok) if ok else "-") + "\nFAIL: " + (", ".join(bad) if bad else "-"))

# ===================== SHARED FORWARD =====================
async def forward_to_admins(user_msg: Message, topic_code: str):
    uid = user_msg.from_user.id

    header = (
        "📩 **Новое сообщение**\n"
        f"Тема: **{topic_name(topic_code)}**\n"
        f"От: `{user_tag(user_msg)}`\n\n"
        "🛠 **Админ-панель:** кнопки ниже (Ответить / Бан)"
    )

    any_sent = False
    for admin_id in ADMINS:
        try:
            banned_flag = is_banned(uid)

            sent_header = await bot.send_message(
                admin_id,
                header,
                parse_mode="Markdown",
                reply_markup=admin_actions_kb(uid, banned_flag)
            )
            save_admin_map(admin_id, sent_header.message_id, uid, topic_code)

            copied = await user_msg.copy_to(admin_id, reply_to_message_id=sent_header.message_id)
            save_admin_map(admin_id, copied.message_id, uid, topic_code)

            any_sent = True
        except Exception as e:
            logging.exception(f"SEND_TO_ADMIN_FAILED admin={admin_id} err={e}")

    if any_sent:
        await user_msg.answer("✅ Принято. Жди ответа админа.")
    else:
        await user_msg.answer("⚠️ Не смог отправить админам. Пусть админы нажмут /start у бота в личке.")

# ===================== USER FLOW =====================
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
        f"Ок, тема: **{topic_name(code)}**.\n\n"
        "Напиши сообщение — я передам админам.\n"
        "После этого жди ответа.",
        parse_mode="Markdown"
    )
    await cb.answer()

@dp.message(UserFlow.chatting, F.any())
async def user_message_chatting(m: Message, state: FSMContext):
    if not m.from_user:
        return
    if is_banned(m.from_user.id):
        return
    if m.from_user.id in ADMINS:
        # админские сообщения не форвардим (чтобы не было петель)
        return

    topic = get_topic(m.from_user.id) or "unknown"
    await forward_to_admins(m, topic)

# FALLBACK: если FSM слетел — всё равно шлём, если тема есть в БД
@dp.message(F.any())
async def user_message_fallback(m: Message, state: FSMContext):
    if not m.from_user:
        return

    uid = m.from_user.id
    if uid in ADMINS:
        return
    if is_banned(uid):
        return

    cur_state = await state.get_state()
    if cur_state == UserFlow.chatting.state:
        return  # уже обработал user_message_chatting

    topic = get_topic(uid)
    if not topic:
        await m.answer("Нажми /start и выбери тему, потом напиши сообщение.")
        return

    await forward_to_admins(m, topic)

# ===================== ADMIN CALLBACKS =====================
@dp.callback_query(F.data.startswith("admin:"))
async def admin_cb(cb: CallbackQuery):
    if not cb.from_user:
        return
    if cb.from_user.id not in ADMINS:
        await cb.answer("Нет доступа.", show_alert=True)
        return

    try:
        _, action, uid_str = cb.data.split(":", 2)
        target_uid = int(uid_str)
    except Exception:
        await cb.answer("Ошибка данных кнопки.", show_alert=True)
        return

    if action == "ban":
        username = None
        try:
            chat = await bot.get_chat(target_uid)
            if getattr(chat, "username", None):
                username = f"@{chat.username}"
        except Exception:
            pass

        upsert_ban(target_uid, username, "banned by admin button")
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
        await cb.answer("Ок. Следующее сообщение отправлю пользователю.", show_alert=True)

    else:
        await cb.answer("Неизвестное действие.", show_alert=True)

# ===================== ADMIN REPLY ROUTING =====================
@dp.message(F.reply_to_message)
async def admin_reply_by_reply(m: Message):
    if not m.from_user:
        return
    if m.from_user.id not in ADMINS:
        return
    if not m.reply_to_message:
        return

    user_id = get_mapped_user(m.chat.id, m.reply_to_message.message_id)
    if not user_id:
        return

    if is_banned(user_id):
        await m.answer("Пользователь забанен — не отправил.")
        return

    try:
        await m.copy_to(user_id)
        await m.answer("✅ Отправлено пользователю.")
    except Exception as e:
        await m.answer(f"❌ Не смог отправить (возможно, пользователь заблокировал бота). {type(e).__name__}")

@dp.message(F.any())
async def admin_reply_by_mode(m: Message):
    if not m.from_user:
        return
    if m.from_user.id not in ADMINS:
        return

    target_uid = admin_reply_target.get(m.from_user.id)
    if not target_uid:
        return

    if is_banned(target_uid):
        admin_reply_target.pop(m.from_user.id, None)
        await m.answer("Пользователь забанен — не отправил.")
        return

    try:
        await m.copy_to(target_uid)
        await m.answer("✅ Отправлено пользователю.")
    except Exception as e:
        await m.answer(f"❌ Не смог отправить (возможно, пользователь заблокировал бота). {type(e).__name__}")
    finally:
        admin_reply_target.pop(m.from_user.id, None)

# ===================== RUN =====================
async def main():
    init_db()

    # Убираем webhook (на всякий, чтобы polling не конфликтовал)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.info(f"delete_webhook failed: {e}")

    threading.Thread(target=run_flask, daemon=True).start()

    logging.info("Start polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
