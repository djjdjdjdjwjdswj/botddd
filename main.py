import os
import logging
from threading import Thread
from flask import Flask

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# айди админов как у тебя
ADMINS = {8243127223, 6334413055}

# user_id -> topic_code (пока не отправил 1 сообщение)
user_topics: dict[int, str] = {}

# admin_id -> dict(user_id, ticket_id, topic_code)
reply_mode: dict[int, dict] = {}

# ================= Flask =================
app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= DB =================
def db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def _col_exists(cur, table: str, col: str) -> bool:
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
        LIMIT 1;
    """, (table, col))
    return cur.fetchone() is not None

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    banned_by BIGINT,
                    banned_at TIMESTAMPTZ DEFAULT now()
                );
            """)

            # миграции (если таблица уже была создана старым кодом)
            if not _col_exists(cur, "bans", "is_banned"):
                cur.execute("ALTER TABLE bans ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT TRUE;")
            if not _col_exists(cur, "bans", "banned_by"):
                cur.execute("ALTER TABLE bans ADD COLUMN banned_by BIGINT;")
            if not _col_exists(cur, "bans", "banned_at"):
                cur.execute("ALTER TABLE bans ADD COLUMN banned_at TIMESTAMPTZ DEFAULT now();")
            if not _col_exists(cur, "bans", "reason"):
                cur.execute("ALTER TABLE bans ADD COLUMN reason TEXT;")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    topic_code TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    message_text TEXT,
                    src_chat_id BIGINT NOT NULL,
                    src_message_id BIGINT NOT NULL
                );
            """)

        conn.commit()


            cur.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    banned_by BIGINT,
                    banned_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            if not _col_exists(cur, "bans", "is_banned"):
                cur.execute("ALTER TABLE bans ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT TRUE;")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    topic_code TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    message_text TEXT,
                    src_chat_id BIGINT NOT NULL,
                    src_message_id BIGINT NOT NULL
                );
            """)
        conn.commit()

def upsert_user(user_id: int, username: str | None, full_name: str | None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, full_name, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (user_id)
                DO UPDATE SET username = EXCLUDED.username,
                              full_name = EXCLUDED.full_name,
                              updated_at = now();
            """, (user_id, username, full_name))
        conn.commit()

def is_banned(user_id: int) -> tuple[bool, str | None]:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT is_banned, reason FROM bans WHERE user_id=%s;", (user_id,))
            row = cur.fetchone()
            if not row:
                return False, None
            return bool(row.get("is_banned", True)), row.get("reason")

def ban_user(user_id: int, banned_by: int, reason: str | None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bans (user_id, is_banned, reason, banned_by, banned_at)
                VALUES (%s, TRUE, %s, %s, now())
                ON CONFLICT (user_id)
                DO UPDATE SET is_banned=TRUE, reason=EXCLUDED.reason, banned_by=EXCLUDED.banned_by, banned_at=now();
            """, (user_id, reason, banned_by))
        conn.commit()

def unban_user(user_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bans (user_id, is_banned, reason, banned_by, banned_at)
                VALUES (%s, FALSE, NULL, NULL, now())
                ON CONFLICT (user_id)
                DO UPDATE SET is_banned=FALSE, reason=NULL, banned_by=NULL, banned_at=now();
            """, (user_id,))
        conn.commit()

def resolve_user_id(value: str) -> int | None:
    v = value.strip()
    if v.startswith("@"):
        uname = v[1:].lower()
        with db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT user_id FROM users WHERE lower(username)=%s LIMIT 1;", (uname,))
                row = cur.fetchone()
                return int(row["user_id"]) if row else None
    try:
        return int(v)
    except:
        return None

def create_ticket(user_id: int, topic_code: str, message_text: str | None, src_chat_id: int, src_message_id: int) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tickets (user_id, topic_code, message_text, src_chat_id, src_message_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING ticket_id;
            """, (user_id, topic_code, message_text, src_chat_id, src_message_id))
            tid = cur.fetchone()[0]
        conn.commit()
        return int(tid)

def get_ticket(ticket_id: int) -> dict | None:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM tickets WHERE ticket_id=%s;", (ticket_id,))
            row = cur.fetchone()
            return dict(row) if row else None

# ================= UI helpers =================
def topic_text(code: str) -> str:
    return {
        "ads": "Насчет рекламы",
        "error": "Замечена ошибка",
        "other": "Другое",
    }.get(code, "Другое")

def topic_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1) Насчет рекламы", callback_data="topic:ads")],
        [InlineKeyboardButton("2) Замечена ошибка", callback_data="topic:error")],
        [InlineKeyboardButton("3) Другое", callback_data="topic:other")],
    ])

def admin_kb(ticket_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ответить", callback_data=f"reply:{ticket_id}")],
        [InlineKeyboardButton("Бан", callback_data=f"ban:{ticket_id}")],
    ])

# ================= Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = update.message.from_user
    upsert_user(u.id, u.username, u.full_name)

    banned, reason = is_banned(u.id)
    if banned:
        await update.message.reply_text(f"🚫 Вы заблокированы.\nПричина: {reason or 'не указана'}")
        return

    await update.message.reply_text(
        "Привет! Выбери, по какому вопросу пишешь 👇",
        reply_markup=topic_kb()
    )

async def on_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    u = q.from_user
    upsert_user(u.id, u.username, u.full_name)

    banned, reason = is_banned(u.id)
    if banned:
        return

    code = q.data.split(":", 1)[1]
    user_topics[u.id] = code

    await q.edit_message_text(
        f"Ок, тема: {topic_text(code)}.\n\n"
        "Напиши ОДНО сообщение — я отправлю админам.\n"
        "Потом жди ответа."
    )

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    admin_id = q.from_user.id
    if admin_id not in ADMINS:
        return

    data = q.data

    if data.startswith("reply:"):
        ticket_id = int(data.split(":", 1)[1])
        t = get_ticket(ticket_id)
        if not t:
            await q.message.reply_text("⚠️ Заявка не найдена.")
            return

        reply_mode[admin_id] = {
            "user_id": int(t["user_id"]),
            "ticket_id": ticket_id,
            "topic_code": t["topic_code"],
        }
        await q.message.reply_text(f"✍️ Напиши ответ по заявке #{ticket_id}:")
        return

    if data.startswith("ban:"):
        ticket_id = int(data.split(":", 1)[1])
        t = get_ticket(ticket_id)
        if not t:
            await q.message.reply_text("⚠️ Заявка не найдена.")
            return

        target_id = int(t["user_id"])
        reason = f"бан через заявку #{ticket_id}"
        ban_user(target_id, admin_id, reason)

        try:
            await context.bot.send_message(target_id, f"🚫 Вы заблокированы администратором.\nПричина: {reason}")
        except:
            pass

        await q.message.reply_text(f"✅ Пользователь (ID {target_id}) заблокирован.")
        return

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    admin_id = update.message.from_user.id
    if admin_id not in ADMINS:
        return

    if not context.args:
        await update.message.reply_text("Использование: /ban <id|@username> [причина]")
        return

    target_id = resolve_user_id(context.args[0])
    if not target_id:
        await update.message.reply_text("Не нашёл пользователя. По @username работает только если он уже писал боту.")
        return

    reason = " ".join(context.args[1:]).strip() or "не указана"
    ban_user(target_id, admin_id, reason)
    await update.message.reply_text(f"✅ Забанен: {target_id}\nПричина: {reason}")

    try:
        await context.bot.send_message(target_id, f"🚫 Вы заблокированы.\nПричина: {reason}")
    except:
        pass

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    admin_id = update.message.from_user.id
    if admin_id not in ADMINS:
        return

    if not context.args:
        await update.message.reply_text("Использование: /unban <id|@username>")
        return

    target_id = resolve_user_id(context.args[0])
    if not target_id:
        await update.message.reply_text("Не нашёл пользователя. По @username работает только если он уже писал боту.")
        return

    unban_user(target_id)
    await update.message.reply_text(f"✅ Разбанен: {target_id}")

    try:
        await context.bot.send_message(target_id, "✅ Вас разбанили.")
    except:
        pass

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    u = msg.from_user
    upsert_user(u.id, u.username, u.full_name)

    banned, reason = is_banned(u.id)
    if banned:
        await msg.reply_text(f"🚫 Вы заблокированы.\nПричина: {reason or 'не указана'}")
        return

    # ===== ADMIN REPLY MODE (ответ одним сообщением) =====
    if u.id in ADMINS and u.id in reply_mode:
        info = reply_mode.pop(u.id)
        target_id = info["user_id"]
        ticket_id = info["ticket_id"]
        tcode = info["topic_code"]

        answer_text = (msg.text or "").strip()
        if not answer_text:
            await msg.reply_text("❌ Ответ должен быть текстом.")
            return

        out = (
            f"✅ Ответ по заявке #{ticket_id}\n"
            f"Тема: {topic_text(tcode)}\n\n"
            f"{answer_text}"
        )
        try:
            await context.bot.send_message(target_id, out)
            await msg.reply_text("✅ Ответ отправлен.")
        except:
            await msg.reply_text("❌ Не смог отправить (возможно пользователь заблокировал бота).")
        return

    # ===== USER SENDS TICKET (1 сообщение) =====
    if u.id not in user_topics:
        await msg.reply_text("Нажми /start и выбери тему.")
        return

    topic_code = user_topics.pop(u.id)  # только 1 сообщение за раз

    ticket_id = create_ticket(
        user_id=u.id,
        topic_code=topic_code,
        message_text=msg.text if msg.text else None,
        src_chat_id=msg.chat_id,
        src_message_id=msg.message_id
    )

    username = f"@{u.username}" if u.username else u.full_name

    # ВОТ ТУТ: текст заявки в ЭТОМ ЖЕ сообщении
    ticket_text = ""
    if msg.text:
        ticket_text = msg.text
    elif msg.caption:
        ticket_text = msg.caption
    else:
        ticket_text = "(медиа)"

    admin_text = (
        f"🔔 Заявка #{ticket_id}\n\n"
        f"👤 {username} (ID: {u.id})\n"
        f"📝 Тема: {topic_text(topic_code)}\n\n"
        f"{ticket_text}"
    )

    for admin_id in ADMINS:
        try:
            await context.bot.send_message(admin_id, admin_text, reply_markup=admin_kb(ticket_id))
        except Exception as e:
            log.error(f"send to admin {admin_id} failed: {e}")

    await msg.reply_text(f"✅ Заявка #{ticket_id} отправлена. Жди ответа.")

# ================= Main =================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    init_db()

    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ban", ban_cmd))
    application.add_handler(CommandHandler("unban", unban_cmd))

    application.add_handler(CallbackQueryHandler(on_topic, pattern=r"^topic:(ads|error|other)$"))
    application.add_handler(CallbackQueryHandler(admin_buttons, pattern=r"^(reply|ban):\d+$"))

    # ловим любые сообщения (только текст нужен для ответа админа, заявка может быть текст/медиа)
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_any_message))

    # ВАЖНО: run_polling сам управляет старт/стоп, иначе и вылезает "already running"
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
