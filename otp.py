import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from twilio.rest import Client

# -------------------------
# Environment / credentials
# -------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 5000))

TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER")

twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

# -------------------------
# Flask app
# -------------------------
flask_app = Flask(__name__)

# -------------------------
# Telegram bot setup
# -------------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

app_bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# -------------------------
# Telegram handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is live ✅")

app_bot.add_handler(CommandHandler("start", start))

# -------------------------
# Webhook route
# -------------------------
@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, app_bot.bot)
    loop.create_task(app_bot.process_update(update))
    return "OK", 200

# -------------------------
# Example OTP sending via Twilio
# -------------------------
@flask_app.route("/send_otp/<phone>/<otp>", methods=["GET"])
def send_otp(phone, otp):
    try:
        twilio_client.messages.create(
            body=f"Your OTP is: {otp}",
            from_=TWILIO_NUMBER,
            to=phone
        )
        return f"OTP sent to {phone}", 200
    except Exception as e:
        return str(e), 500

# -------------------------
# Run bot + Flask
# -------------------------
if __name__ == "__main__":
    # Initialize bot
    loop.create_task(app_bot.initialize())
    loop.create_task(app_bot.start())
    print(f"Bot running on port {PORT} with webhook /{TELEGRAM_TOKEN}")
    # Run Flask
    flask_app.run(host="0.0.0.0", port=PORT)
