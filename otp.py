import os
import asyncio
import threading

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Load env variables
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", 10000))

# =========================
# Flask app
# =========================
app = Flask(__name__)

# =========================
# Telegram bot application
# =========================
application = Application.builder().token(TELEGRAM_TOKEN).build()


# ---- Bot Commands ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot is online!\nSend me a message and I'll echo it back."
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"You said: {update.message.text}")


application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))


# ---- Webhook Route ----
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = Update.de_json(request.json, application.bot)  # FIXED JSON
        application.update_queue.put_nowait(update)
    except Exception as e:
        print("Webhook error:", e)
        return "error", 500
    return "ok", 200


# ---- Health Check ----
@app.route("/", methods=["GET"])
def index():
    return "Bot is running ✅", 200


# ---- Webhook Setter ----
@app.route("/setwebhook", methods=["GET"])
def set_webhook():
    webhook_url = f"{os.getenv('RENDER_URL')}/{TELEGRAM_TOKEN}"
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(application.bot.set_webhook(webhook_url))
        return f"Webhook set to {webhook_url}", 200
    except Exception as e:
        print("Webhook error:", e)
        return "error", 500


# =========================
# Run Flask + Bot
# =========================
def run_flask():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    # Start Flask in a thread
    threading.Thread(target=run_flask).start()

    # Start Telegram application in polling-safe mode
    asyncio.run(application.initialize())
    asyncio.run(application.start())
    asyncio.get_event_loop().run_forever()


