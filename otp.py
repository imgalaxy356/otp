import os
from datetime import datetime, timedelta, timezone

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from twilio.rest import Client
import stripe
import asyncio

# -------------------------
# CONFIG
# -------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]

stripe.api_key = STRIPE_SECRET_KEY
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Flask app
app = Flask(__name__)

# Telegram app
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Paid users
paid_users = {}
user_phone_numbers = {}
user_last_message = {}

# -------------------------
# Helpers
# -------------------------
def is_paid(user_id: int) -> bool:
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def get_main_menu(user_id: int) -> InlineKeyboardMarkup:
    if is_paid(user_id):
        keyboard = [
            [InlineKeyboardButton("📱 Set Phone", callback_data="set_phone")],
            [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
            [InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("💳 Pay $25 (4 days)", callback_data="buy")],
            [InlineKeyboardButton("📱 Set Phone", callback_data="set_phone")],
            [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
            [InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")],
        ]
    return InlineKeyboardMarkup(keyboard)

# -------------------------
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Choose an option:", reply_markup=get_main_menu(update.effective_user.id))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "buy":
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {"currency": "usd", "product_data": {"name": "Bot Access (4 days)"}, "unit_amount": 2500},
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{RENDER_EXTERNAL_URL}/success?user_id={user_id}",
            cancel_url=f"{RENDER_EXTERNAL_URL}/cancel",
        )
        await query.message.reply_text(f"💳 Complete payment here:\n{session.url}")

    elif query.data == "set_phone":
        if not is_paid(user_id):
            await query.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
        await query.message.reply_text("📱 Send me your phone number (with country code).", reply_markup=keyboard)
        context.user_data["awaiting_phone"] = True

    elif query.data == "make_call":
        if not is_paid(user_id):
            await query.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
            return
        phone = user_phone_numbers.get(user_id)
        if not phone:
            await query.message.reply_text("❌ No phone set. Please set a phone number first.")
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
        await query.message.reply_text("Send me your custom message or type /call to reuse last message.", reply_markup=keyboard)
        context.user_data["awaiting_message"] = True

    elif query.data == "help":
        await query.message.reply_text(
            "ℹ️ Usage Guide:\n\n"
            "1️⃣ Pay $25 to unlock features.\n"
            "2️⃣ Set a phone number.\n"
            "3️⃣ Make calls with custom messages.\n"
            "✅ OTPs will appear here in Telegram.",
            reply_markup=get_main_menu(user_id)
        )

    elif query.data == "menu":
        await query.message.reply_text("Main Menu:", reply_markup=get_main_menu(user_id))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if context.user_data.get("awaiting_phone"):
        user_phone_numbers[user_id] = text
        context.user_data["awaiting_phone"] = False
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
            [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
        ])
        await update.message.reply_text(f"✅ Phone number saved: {text}", reply_markup=keyboard)
        return

    if context.user_data.get("awaiting_message"):
        user_last_message[user_id] = text
        context.user_data["awaiting_message"] = False
        await update.message.reply_text("Message saved! Use /call to place the call.")

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
    twilio_client.calls.create(to=phone, from_=TWILIO_PHONE_NUMBER, twiml=f"<Response><Say>{message}</Say></Response>")
    await update.message.reply_text(f"📞 Call placed to {phone}!")

# -------------------------
# Flask routes
# -------------------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.update_queue.put(update))
    return "ok"

@app.route("/setwebhook", methods=["GET"])
def set_webhook():
    application.bot.set_webhook(f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}")
    return f"Webhook set!"

@app.route("/success", methods=["GET"])
def success():
    user_id = int(request.args.get("user_id"))
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return "✅ Payment successful."

@app.route("/cancel", methods=["GET"])
def cancel():
    return "❌ Payment canceled."

# -------------------------
# Register handlers
# -------------------------
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("call", call_command))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# -------------------------
# Run Flask
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
