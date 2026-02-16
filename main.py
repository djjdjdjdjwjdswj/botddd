import os
import logging
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# твои админы (те же, что ты писал)
ADMINS = {7355737254, 8243127223, 8167127645}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is missing")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ===================== FLASK (Render ping) =====================
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

# ===================== DB =====================
def db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def _col_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
        LIMIT 1;
        """,
        (table, col),
    )
    return cur.fetchone() is not None

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            # users
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            # tickets
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    topic TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    status TEXT NOT NULL DEFAULT 'open',
                    src_chat_id BIGINT NOT NULL,
                    src_message_id BIGINT NOT NULL
                );
                """
            )

            # bans (с миграциями)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bans (
                    user_id BIGINT PRIMARY KEY
                );
                """
            )
            if not _col_exists(cur, "bans", "is_banned"):
                cur.execute("ALTER TABLE bans ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT TRUE;")
            if not _col_exists(cur, "bans", "reason"):
                cur.execute("ALTER TABLE bans ADD COLUMN reason TEXT;")
            if not _col_exists(cur, "bans", "banned_by"):
                cur.execute("ALTER TABLE bans ADD COLUMN banned_by BIGINT;")
            if not _col_exists(cur, "bans", "banned_at"):
                cur.execute("ALTER TABLE bans ADD COLUMN banned_at TIMESTAMPTZ DEFAULT now();")
            if not _col_exists(cur, "bans", "until_ts"):
                cur.execute("ALTER TABLE bans ADD COLUMN until_ts BIGINT;")  # unix timestamp; NULL = forever

        conn.commit()

def upsert_user(user_id: int, username: str | None, full_name: str | None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (user_id, username, full_name, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (user_id)
                DO UPDATE SET username=EXCLUDED.username, full_name=EXCLUDED.full_name, updated_at=now();
                """,
                (user_id, username, full_name),
            )
        conn.commit()

def is_banned(user_id: int):
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT is_banned, reason, until_ts FROM bans WHERE user_id=%s;", (user_id,))
            row = cur.fetchone()
            if not row:
                return False, None

            if not row["is_banned"]:
                return False, None

            until_ts = row.get("until_ts")
            if until_ts is not None and int(until_ts) > 0 and time.time() > int(until_ts):
                # бан истек — снимаем
                cur.execute("UPDATE bans SET is_banned=FALSE WHERE user_id=%s;", (user_id,))
                conn.commit()
                return False, None

            return True, row.get("reason") or "без причины"

def create_ticket(user_id: int, topic: str, src_chat_id: int, src_message_id: int) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets (user_id, topic, src_chat_id, src_message_id)
                VALUES (%s, %s, %s, %s)
                RETURNING ticket_id;
                """,
                (user_id, topic, src_chat_id, src_message_id),
            )
            ticket_id = cur.fetchone()[0]
        conn.commit()
    return int(ticket_id)

def get_ticket(ticket_id: int):
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tickets WHERE ticket_id=%s;", (ticket_id,))
            return cur.fetchone()

def set_ticket_status(ticket_id: int, status: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tickets SET status=%s WHERE ticket_id=%s;", (status, ticket_id))
        conn.commit()

def ban_user(target_id: int, admin_id: int, reason: str, until_ts: int | None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bans (user_id, is_banned, reason, banned_by, banned_at, until_ts)
                VALUES (%s, TRUE, %s, %s, now(), %s)
                ON CONFLICT (user_id)
                DO UPDATE SET is_banned=TRUE, reason=EXCLUDED.reason, banned_by=EXCLUDED.banned_by, banned_at=now(), until_ts=EXCLUDED.until_ts;
                """,
                (target_id, reason, admin_id, until_ts),
            )
        conn.commit()

def unban_user(target_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bans SET is_banned=FALSE WHERE user_id=%s;", (target_id,))
        conn.commit()

# ===================== BOT LOGIC =====================
TOPIC_MAP = {
    "topic_ads": "Насчет рекламы",
    "topic_bug": "Замечена ошибка",
    "topic_other": "Другое",
}

def topic_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1. Насчет рекламы", callback_data="topic_ads")],
            [InlineKeyboardButton("2. Замечена ошибка", callback_data="topic_bug")],
            [InlineKeyboardButton("3. Другое", callback_data="topic_other")],
        ]
    )

def admin_ticket_kb(ticket_id: int, user_id: int):
    # callback_data короткие
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ Ответить", callback_data=f"reply:{ticket_id}")],
            [InlineKeyboardButton("🚫 Бан", callback_data=f"banmenu:{ticket_id}:{user_id}")],
            [InlineKeyboardButton("✅ Разбан", callback_data=f"unban:{user_id}")],
        ]
    )

def ban_menu_kb(ticket_id: int, user_id: int):
    # длительности (сек)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1ч", callback_data=f"ban:{ticket_id}:{user_id}:3600"),
                InlineKeyboardButton("1д", callback_data=f"ban:{ticket_id}:{user_id}:86400"),
                InlineKeyboardButton("7д", callback_data=f"ban:{ticket_id}:{user_id}:604800"),
            ],
            [
                InlineKeyboardButton("Навсегда", callback_data=f"ban:{ticket_id}:{user_id}:0"),
                InlineKeyboardButton("Отмена", callback_data="ban_cancel"),
            ],
        ]
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username, u.full_name)

    banned, reason = is_banned(u.id)
    if banned:
        await update.message.reply_text(f"🚫 Вы заблокированы.\nПричина: {reason}")
        return

    context.user_data.pop("topic", None)
    context.user_data.pop("awaiting_ticket", None)

    await update.message.reply_text(
        "Привет!\n\nВыберите тему обращения:",
        reply_markup=topic_kb(),
    )

async def on_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    code = q.data
    if code not in TOPIC_MAP:
        return

    u = q.from_user
    upsert_user(u.id, u.username, u.full_name)

    banned, reason = is_banned(u.id)
    if banned:
        await q.edit_message_text(f"🚫 Вы заблокированы.\nПричина: {reason}")
        return

    context.user_data["topic"] = TOPIC_MAP[code]
    context.user_data["awaiting_ticket"] = True

    # убираем “чтобы снова выбрать тему” — как ты просил
    await q.edit_message_text(
        f"✅ Тема: {context.user_data['topic']}\n\nНапиши сообщение (только 1 сообщение на заявку)."
    )

async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # принимает ЛЮБОЙ контент (кроме команд), но только если выбрана тема
    msg = update.effective_message
    u = update.effective_user
    upsert_user(u.id, u.username, u.full_name)

    banned, reason = is_banned(u.id)
    if banned:
        await msg.reply_text(f"🚫 Вы заблокированы.\nПричина: {reason}")
        return

    topic = context.user_data.get("topic")
    awaiting = context.user_data.get("awaiting_ticket")

    if not topic or not awaiting:
        # игнор, если человек не выбрал тему
        return

    # создаем заявку и запрещаем второе сообщение
    ticket_id = create_ticket(u.id, topic, msg.chat_id, msg.message_id)
    context.user_data["awaiting_ticket"] = False  # только 1 сообщение
    context.user_data.pop("topic", None)

    # пользователю подтверждение
    await msg.reply_text(f"✅ Заявка #{ticket_id} отправлена. Ожидай ответ.")

    # админам — заголовок + копия сообщения
    header = (
        f"🔔 Заявка #{ticket_id}\n\n"
        f"👤 @{u.username if u.username else u.full_name} (ID: {u.id})\n"
        f"📝 Тема: {topic}\n"
    )

    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=header,
                reply_markup=admin_ticket_kb(ticket_id, u.id),
            )
            # копируем оригинальное сообщение (медиа/голос/фото/текст — всё ок)
            await context.bot.copy_message(
                chat_id=admin_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
        except Exception as e:
            log.warning("Failed to notify admin %s: %s", admin_id, e)

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    admin_id = q.from_user.id
    if admin_id not in ADMINS:
        await q.answer("Нет прав", show_alert=True)
        return

    data = q.data or ""

    # открыть меню бана
    if data.startswith("banmenu:"):
        _, ticket_id_s, user_id_s = data.split(":")
        ticket_id = int(ticket_id_s)
        user_id = int(user_id_s)
        await q.edit_message_reply_markup(reply_markup=ban_menu_kb(ticket_id, user_id))
        return

    if data == "ban_cancel":
        # ничего не делаем, просто уберём меню
        await q.edit_message_reply_markup(reply_markup=None)
        return

    # начать ответ
    if data.startswith("reply:"):
        _, ticket_id_s = data.split(":")
        ticket_id = int(ticket_id_s)

        t = get_ticket(ticket_id)
        if not t:
            await q.answer("Заявка не найдена", show_alert=True)
            return

        # сохраняем состояние "админ сейчас пишет ответ"
        context.user_data["reply_ticket_id"] = ticket_id
        context.user_data["reply_user_id"] = int(t["user_id"])
        context.user_data["reply_topic"] = str(t["topic"])

        await q.message.reply_text(
            f"✍️ Напиши ответ для заявки #{ticket_id}\nТема: {t['topic']}\n\n(Отправь ОДНО сообщение текстом)"
        )
        return

    # бан: ban:ticket:user:seconds
    if data.startswith("ban:"):
        _, ticket_id_s, user_id_s, seconds_s = data.split(":")
        ticket_id = int(ticket_id_s)
        user_id = int(user_id_s)
        seconds = int(seconds_s)

        context.user_data["ban_ticket_id"] = ticket_id
        context.user_data["ban_user_id"] = user_id
        context.user_data["ban_seconds"] = seconds

        dur_txt = "навсегда" if seconds == 0 else f"{seconds} сек"
        await q.message.reply_text(f"🚫 Бан по заявке #{ticket_id} ({dur_txt}).\nНапиши причину:")
        return

    # разбан
    if data.startswith("unban:"):
        _, user_id_s = data.split(":")
        user_id = int(user_id_s)
        unban_user(user_id)
        await q.message.reply_text(f"✅ Разбан: {user_id}")
        return

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # админ пишет либо ответ, либо причину бана
    msg = update.effective_message
    admin_id = update.effective_user.id

    if admin_id not in ADMINS:
        return

    # 1) ответ пользователю
    if "reply_ticket_id" in context.user_data:
        ticket_id = int(context.user_data["reply_ticket_id"])
        user_id = int(context.user_data["reply_user_id"])
        topic = str(context.user_data["reply_topic"])

        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("❌ Ответ должен быть текстом.")
            return

        # отправляем пользователю одним сообщением как ты просил
        out = f"✅ Ответ по заявке #{ticket_id}\nТема: {topic}\n\n{text}"
        try:
            await context.bot.send_message(chat_id=user_id, text=out)
        except Exception as e:
            await msg.reply_text(f"❌ Не смог отправить пользователю: {e}")
        else:
            await msg.reply_text(f"✅ Отправлено пользователю (ID: {user_id})")
            set_ticket_status(ticket_id, "answered")

        context.user_data.pop("reply_ticket_id", None)
        context.user_data.pop("reply_user_id", None)
        context.user_data.pop("reply_topic", None)
        return

    # 2) причина бана
    if "ban_user_id" in context.user_data:
        ticket_id = int(context.user_data["ban_ticket_id"])
        user_id = int(context.user_data["ban_user_id"])
        seconds = int(context.user_data["ban_seconds"])

        reason = (msg.text or "").strip()
        if not reason:
            await msg.reply_text("❌ Нужна причина текстом.")
            return

        until_ts = None
        if seconds > 0:
            until_ts = int(time.time() + seconds)

        ban_user(user_id, admin_id, reason, until_ts)

        # уведомим пользователя
        try:
            if until_ts is None:
                await context.bot.send_message(chat_id=user_id, text=f"🚫 Вы заблокированы.\nПричина: {reason}")
            else:
                dt = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
                await context.bot.send_message(chat_id=user_id, text=f"🚫 Вы заблокированы до {dt}\nПричина: {reason}")
        except Exception:
            pass

        await msg.reply_text(f"✅ Забанен пользователь {user_id} (заявка #{ticket_id})")
        set_ticket_status(ticket_id, "closed")

        context.user_data.pop("ban_ticket_id", None)
        context.user_data.pop("ban_user_id", None)
        context.user_data.pop("ban_seconds", None)
        return

def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    # темы
    application.add_handler(CallbackQueryHandler(on_topic, pattern=r"^topic_"))
    # админ-кнопки
    application.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^(reply:|banmenu:|ban:|unban:|ban_cancel)"))

    # сообщения от пользователей (любой контент, кроме команд)
    application.add_handler(MessageHandler(~filters.COMMAND, user_message))
    # текст от админов (ответ/причина)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))

    return application

def main():
    init_db()

    # Flask в отдельном потоке (чтобы Render видел порт)
    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    application = build_app()

    # ВАЖНО: только один запуск polling, без start()/stop() вручную
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
