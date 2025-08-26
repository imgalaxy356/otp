import os
import asyncio
from flask import Flask, request, Response
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 5000))

# -------------------------
# Telegram bot setup
# -------------------------
app_bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is live ✅")

app_bot.add_handler(CommandHandler("start", start))

# -------------------------
# Flask app for webhook
# -------------------------
flask_app = Flask(__name__)

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, app_bot.bot)
    # Schedule Telegram update to be processed in bot's loop
    asyncio.create_task(app_bot.process_update(update))
    return "OK", 200

if __name__ == "__main__":
    print(f"Bot running on port {PORT} with webhook endpoint /{TELEGRAM_TOKEN}")
    flask_app.run(host="0.0.0.0", port=PORT)
