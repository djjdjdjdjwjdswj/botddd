import os
import re
import logging
import threading
from datetime import datetime, timezone

import psycopg2
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== CONFIG ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required")

ADMINS = {7355737254, 8243127223, 8167127645}

TOPICS = {
    "ads": "Насчет рекламы",
    "bug": "Замечена ошибка",
    "other": "Другое",
}

# ================== DB ==================
def db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    topic_code TEXT NOT NULL,
                    topic_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    src_chat_id BIGINT,
                    src_message_id BIGINT,
                    admin_id BIGINT,
                    answered_at TIMESTAMPTZ,
                    answer_text TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bans (
                    user_id BIGINT PRIMARY KEY,
                    until_ts TIMESTAMPTZ NOT NULL,
                    reason TEXT NOT NULL,
                    banned_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
        conn.commit()
    log.info("DB initialized")

def is_banned(user_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT until_ts, reason FROM bans WHERE user_id=%s;", (user_id,))
            row = cur.fetchone()
            if not row:
                return False, None, None
            until_ts, reason = row
            now = datetime.now(timezone.utc)
            if until_ts > now:
                return True, reason, until_ts
            cur.execute("DELETE FROM bans WHERE user_id=%s;", (user_id,))
        conn.commit()
    return False, None, None

def unban_user(user_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bans WHERE user_id=%s;", (user_id,))
        conn.commit()

def create_ticket(user_id: int, topic_code: str, src_chat_id: int, src_message_id: int):
    topic_text = TOPICS[topic_code]
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets (user_id, topic_code, topic_text, src_chat_id, src_message_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (user_id, topic_code, topic_text, src_chat_id, src_message_id),
            )
            ticket_id = cur.fetchone()[0]
        conn.commit()
    return ticket_id, topic_text

def set_ticket_answer(ticket_id: int, admin_id: int, answer_text: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tickets
                SET status='answered', admin_id=%s, answered_at=NOW(), answer_text=%s
                WHERE id=%s;
                """,
                (admin_id, answer_text, ticket_id),
            )
        conn.commit()

def get_ticket(ticket_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, topic_code, topic_text, status, created_at, src_chat_id, src_message_id
                FROM tickets
                WHERE id=%s;
                """,
                (ticket_id,),
            )
            return cur.fetchone()

def add_seconds(dt: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(dt.timestamp() + seconds, tz=timezone.utc)

def ban_user(user_id: int, seconds: int, reason: str, banned_by: int | None):
    until_ts = add_seconds(datetime.now(timezone.utc), seconds)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bans (user_id, until_ts, reason, banned_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET until_ts=EXCLUDED.until_ts, reason=EXCLUDED.reason, banned_by=EXCLUDED.banned_by;
                """,
                (user_id, until_ts, reason, banned_by),
            )
        conn.commit()
    return until_ts

# ================== UTILS ==================
def user_label(u) -> str:
    if getattr(u, "username", None):
        return f"@{u.username}"
    return u.first_name or "без ника"

def parse_duration(s: str):
    s = (s or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhd])", s)
    if not m:
        return None, None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return n * 60, f"{n} мин"
    if unit == "h":
        return n * 3600, f"{n} ч"
    if unit == "d":
        return n * 86400, f"{n} д"
    return None, None

# ================== FLASK ==================
app = Flask(__name__)

@app.get("/")
def home():
    return "OK"

@app.get("/health")
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ================== BOT ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    banned, reason, until_ts = is_banned(u.id)
    if banned:
        until_str = until_ts.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        await update.message.reply_text(f"🚫 Ты в бане до {until_str}\nПричина: {reason}")
        return

    # сброс юзер-стейта
    context.user_data.pop("topic_code", None)
    context.user_data.pop("awaiting_one_message", None)
    context.user_data.pop("pending_ticket_id", None)

    kb = [
        [InlineKeyboardButton("1) Насчет рекламы", callback_data="topic:ads")],
        [InlineKeyboardButton("2) Замечена ошибка", callback_data="topic:bug")],
        [InlineKeyboardButton("3) Другое", callback_data="topic:other")],
    ]
    await update.message.reply_text(
        "Привет! Выбери, по какому вопросу пишешь 👇",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def pick_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user

    banned, reason, until_ts = is_banned(u.id)
    if banned:
        until_str = until_ts.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        await q.edit_message_text(f"🚫 Ты в бане до {until_str}\nПричина: {reason}")
        return

    _, code = q.data.split(":", 1)
    if code not in TOPICS:
        await q.edit_message_text("Ошибка темы. Нажми /start")
        return

    context.user_data["topic_code"] = code
    context.user_data["awaiting_one_message"] = True
    context.user_data["pending_ticket_id"] = None

    topic_text = TOPICS[code]
    await q.edit_message_text(
        f"Ок, тема: {topic_text}.\n\nНапиши ОДНО сообщение — я передам админам.\nПосле этого жди ответа."
    )

async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    u = update.effective_user

    banned, reason, until_ts = is_banned(u.id)
    if banned:
        until_str = until_ts.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        await msg.reply_text(f"🚫 Ты в бане до {until_str}\nПричина: {reason}")
        return

    # если уже отправил — не даём спамить
    if context.user_data.get("pending_ticket_id"):
        tid = context.user_data["pending_ticket_id"]
        await msg.reply_text(f"✅ Твоя заявка #{tid} уже отправлена. Жди ответа.")
        return

    topic_code = context.user_data.get("topic_code")
    if not context.user_data.get("awaiting_one_message") or not topic_code:
        await msg.reply_text("Нажми /start и выбери тему.")
        return

    ticket_id, topic_text = create_ticket(u.id, topic_code, msg.chat_id, msg.message_id)
    context.user_data["pending_ticket_id"] = ticket_id
    context.user_data["awaiting_one_message"] = False

    await msg.reply_text(f"✅ Заявка #{ticket_id} отправлена. Жди ответа.")

    header = (
        f"🔔 <b>Заявка #{ticket_id}</b>\n\n"
        f"👤 <b>{user_label(u)}</b> (ID: <code>{u.id}</code>)\n"
        f"📝 <b>Тема:</b> {topic_text}"
    )

    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Ответить", callback_data=f"admin:reply:{ticket_id}")],
            [InlineKeyboardButton("Бан", callback_data=f"admin:ban:{ticket_id}")],
            [InlineKeyboardButton("Разбан", callback_data=f"admin:unban:{u.id}")],
        ]
    )

    for admin_id in ADMINS:
        try:
            if msg.text:
                body = f"{header}\n\n<b>🧾 Текст:</b>\n{msg.text}"
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=body,
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons,
                )
            else:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=header,
                    parse_mode=ParseMode.HTML,
                )
                await context.bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    reply_markup=buttons,
                )
        except Exception as e:
            log.warning("Failed to notify admin %s: %s", admin_id, e)

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    admin_id = q.from_user.id

    if admin_id not in ADMINS:
        await q.answer("Нет доступа", show_alert=True)
        return

    _, action, arg = q.data.split(":", 2)

    if action == "reply":
        ticket_id = int(arg)
        t = get_ticket(ticket_id)
        if not t:
            await q.edit_message_text("Тикет не найден.")
            return
        context.user_data["admin_mode"] = "reply"
        context.user_data["admin_ticket_id"] = ticket_id
        await q.edit_message_text((q.message.text or "") + "\n\n✍️ Напиши ответ одним сообщением.")
        return

    if action == "ban":
        ticket_id = int(arg)
        t = get_ticket(ticket_id)
        if not t:
            await q.edit_message_text("Тикет не найден.")
            return
        context.user_data["admin_mode"] = "ban_duration"
        context.user_data["admin_ticket_id"] = ticket_id
        await q.edit_message_text((q.message.text or "") + "\n\n⏱ Время бана? (10m, 2h, 3d)")
        return

    if action == "unban":
        user_id = int(arg)
        try:
            unban_user(user_id)
            await q.edit_message_text((q.message.text or "") + "\n\n✅ Разбанено.")
            try:
                await context.bot.send_message(user_id, "✅ Тебя разбанили.")
            except:
                pass
        except Exception as e:
            await q.edit_message_text((q.message.text or "") + f"\n\n❌ Ошибка разбана: {e}")
        return

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ВАЖНО: теперь админ может быть обычным юзером.
    Этот хендлер обрабатывает ТОЛЬКО когда админ реально в режиме reply/ban.
    Иначе он НИЧЕГО не делает, и сообщение уйдет в user_message.
    """
    admin_id = update.effective_user.id
    if admin_id not in ADMINS:
        return

    mode = context.user_data.get("admin_mode")
    ticket_id = context.user_data.get("admin_ticket_id")
    if not mode or not ticket_id:
        return  # <-- ключевой фикс

    msg = update.effective_message
    t = get_ticket(int(ticket_id))
    if not t:
        await msg.reply_text("Тикет не найден.")
        context.user_data.pop("admin_mode", None)
        context.user_data.pop("admin_ticket_id", None)
        return

    _id, user_id, topic_code, topic_text, status, created_at, src_chat_id, src_message_id = t

    if mode == "reply":
        answer = msg.text or ""
        set_ticket_answer(_id, admin_id, answer)

        out = f"✅ <b>Ответ по заявке #{_id}</b>\n<b>Тема:</b> {topic_text}\n\n{answer}"
        try:
            await context.bot.send_message(user_id, out, parse_mode=ParseMode.HTML)
        except Exception as e:
            await msg.reply_text(f"❌ Не смог отправить пользователю: {e}")
            return

        await msg.reply_text("✅ Ответ отправлен.")
        context.user_data.pop("admin_mode", None)
        context.user_data.pop("admin_ticket_id", None)
        return

    if mode == "ban_duration":
        seconds, readable = parse_duration(msg.text or "")
        if seconds is None:
            await msg.reply_text("❌ Формат времени: 10m, 2h, 3d")
            return
        context.user_data["admin_mode"] = "ban_reason"
        context.user_data["ban_seconds"] = seconds
        context.user_data["ban_readable"] = readable
        await msg.reply_text("📝 Причина бана? (одним сообщением)")
        return

    if mode == "ban_reason":
        reason = msg.text or "без причины"
        seconds = int(context.user_data.get("ban_seconds", 0))
        readable = context.user_data.get("ban_readable", "")

        until_ts = ban_user(user_id, seconds, reason, admin_id)
        until_str = until_ts.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

        try:
            await context.bot.send_message(
                user_id,
                f"🚫 Тебя забанили на {readable}\nДо: {until_str}\nПричина: {reason}",
            )
        except:
            pass

        await msg.reply_text(f"✅ Забанено: ID {user_id} на {readable} (до {until_str})")

        context.user_data.pop("admin_mode", None)
        context.user_data.pop("admin_ticket_id", None)
        context.user_data.pop("ban_seconds", None)
        context.user_data.pop("ban_readable", None)
        return

async def testadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for a in ADMINS:
        try:
            await context.bot.send_message(a, f"✅ Тест: бот может писать админу {a}")
        except Exception as e:
            await update.message.reply_text(f"FAIL {a}: {e}")
            return
    await update.message.reply_text("OK")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)

def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(on_error)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("testadmins", testadmins))

    application.add_handler(CallbackQueryHandler(pick_topic, pattern=r"^topic:"))
    application.add_handler(CallbackQueryHandler(admin_buttons, pattern=r"^admin:"))

    # Админский текстовый — но он теперь не блокирует админа как юзера
    application.add_handler(
        MessageHandler(filters.Chat(list(ADMINS)) & filters.TEXT & ~filters.COMMAND, admin_text)
    )

    # Все сообщения (и админов тоже, если admin_text не в режиме) идут сюда
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, user_message)
    )

    return application

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    application = build_app()
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
