import os
import logging
from threading import Thread
from flask import Flask

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

TOKEN = os.getenv("BOT_TOKEN")

ADMINS = {8243127223, 6334413055}

user_topics = {}
reply_mode = {}

# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Насчет рекламы", callback_data="ads")],
        [InlineKeyboardButton("Замечена ошибка", callback_data="error")],
        [InlineKeyboardButton("Другое", callback_data="other")]
    ]

    await update.message.reply_text(
        "Привет!\nВыберите тему обращения:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================= КНОПКИ =================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_topics[query.from_user.id] = query.data

    await query.edit_message_text(
        "Напишите сообщение.\nМожете отправить несколько сообщений.\nОжидайте ответа."
    )

# ================= СООБЩЕНИЯ =================

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    username = user.username or "без username"
    text = update.message.text

    # ===== ЕСЛИ АДМИН ОТВЕЧАЕТ =====
    if user_id in ADMINS and user_id in reply_mode:
        target_id = reply_mode[user_id]
        try:
            await context.bot.send_message(target_id, text)
            await update.message.reply_text("Ответ отправлен.")
        except:
            await update.message.reply_text("Ошибка отправки.")
        del reply_mode[user_id]
        return

    # ===== ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ =====
    topic = user_topics.get(user_id)
    if not topic:
        return

    topic_text = {
        "ads": "Насчет рекламы",
        "error": "Замечена ошибка",
        "other": "Другое"
    }.get(topic, "Не указано")

    keyboard = [
        [InlineKeyboardButton("Ответить", callback_data=f"reply_{user_id}")]
    ]

    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📩 Новое сообщение\n\n"
                f"Тема: {topic_text}\n"
                f"От: @{username}\n"
                f"ID: {user_id}\n\n"
                f"{text}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except:
            pass

# ================= ОТВЕТ АДМИНА =================

async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("reply_"):
        return

    target_id = int(query.data.split("_")[1])

    if query.from_user.id not in ADMINS:
        return

    reply_mode[query.from_user.id] = target_id

    await query.message.reply_text(
        f"Напишите ответ пользователю (ID {target_id})"
    )

# ================= MAIN =================

def main():
    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons, pattern="^(ads|error|other)$"))
    application.add_handler(CallbackQueryHandler(admin_reply, pattern="^reply_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    application.run_polling()

if __name__ == "__main__":
    main()
