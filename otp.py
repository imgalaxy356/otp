import os
import logging
from datetime import datetime, timedelta, timezone
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from twilio.rest import Client
import stripe
import asyncio

# -------------------------
# CONFIG (use Render env vars)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# -------------------------
# Init services
# -------------------------
stripe.api_key = STRIPE_SECRET_KEY
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = Flask(__name__)
application = Application.builder().token(TELEGRAM_TOKEN).build()

# -------------------------
# State storage
# -------------------------
paid_users = {}  # user_id -> expiry datetime
user_phone_numbers = {}  # user_id -> phone
user_last_message = {}  # user_id -> last message

# -------------------------
# Helpers
# -------------------------
def is_paid(user_id: int) -> bool:
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def main_menu_keyboard(user_id: int):
    if is_paid(user_id):
        keyboard = [
            [InlineKeyboardButton("📱 Set Phone", callback_data="set_phone")],
            [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("💳 Pay $25 (4 days)", callback_data="buy")],
            [InlineKeyboardButton("📱 Set Phone", callback_data="set_phone")],
            [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
        ]
    return InlineKeyboardMarkup(keyboard)

# -------------------------
# Telegram Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Choose an option:", reply_markup=main_menu_keyboard(update.effective_user.id))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "buy":
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {"currency":"usd","product_data":{"name":"Bot Access (4 days)"},"unit_amount":2500},
                "quantity":1
            }],
            mode="payment",
            success_url=f"{RENDER_EXTERNAL_URL}/success?user_id={user_id}",
            cancel_url=f"{RENDER_EXTERNAL_URL}/cancel"
        )
        await query.message.reply_text(f"💳 Complete payment here:\n{session.url}")

    elif query.data == "set_phone":
        if not is_paid(user_id):
            await query.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
            return
        await query.message.reply_text("📱 Send me your phone number (with country code, e.g. +1...)")

    elif query.data == "make_call":
        if not is_paid(user_id):
            await query.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
            return
        phone = user_phone_numbers.get(user_id)
        if not phone:
            await query.message.reply_text("❌ No phone set. Please set a phone number first.")
            return
        await query.message.reply_text(
            "Send me your custom message for the call.\nOr type /call to reuse your last message."
        )

    elif query.data == "help":
        await query.message.reply_text(
            "ℹ️ Usage Guide:\n1️⃣ Pay $25 to unlock features.\n2️⃣ Set a phone number.\n3️⃣ Make calls with custom messages.\n✅ OTPs will appear here in Telegram."
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text.startswith("+") and text[1:].isdigit():
        user_phone_numbers[user_id] = text
        keyboard = [
            [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
        ]
        await update.message.reply_text(f"✅ Phone number saved: {text}", reply_markup=InlineKeyboardMarkup(keyboard))
    elif user_id in user_phone_numbers:
        user_last_message[user_id] = text
        await update.message.reply_text("📞 Use /call to place the call now.")

async def call_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_paid(user_id):
        await update.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
        return
    phone = user_phone_numbers.get(user_id)
    if not phone:
        await update.message.reply_text("❌ No phone number set.")
        return
    message = user_last_message.get(user_id, "This is your call.")
    twilio_client.calls.create(
        to=phone,
        from_=TWILIO_PHONE_NUMBER,
        twiml=f"<Response><Say>{message}</Say></Response>"
    )
    await update.message.reply_text(f"📞 Call placed to {phone}.")

# -------------------------
# Flask routes
# -------------------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "ok", 200

@app.route("/success", methods=["GET"])
def payment_success():
    user_id = int(request.args.get("user_id"))
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return "✅ Payment successful. You now have 4 days of access."

@app.route("/cancel", methods=["GET"])
def payment_cancel():
    return "❌ Payment canceled."

# -------------------------
# Register Handlers
# -------------------------
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("call", call_command))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# -------------------------
# Async webhook setup at startup
# -------------------------
async def set_webhook():
    url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}"
    await application.bot.set_webhook(url)
    print(f"Webhook set to {url}")

# -------------------------
# Run Flask (Render)
# -------------------------
if __name__ == "__main__":
    # Set webhook before starting Flask
    asyncio.run(set_webhook())
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
