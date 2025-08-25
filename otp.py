import os
import asyncio
from datetime import datetime, timedelta, timezone

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from twilio.rest import Client
import stripe

# ------------------------- CONFIG -------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")  # e.g. https://otp-28gz.onrender.com

# Initialize services
stripe.api_key = STRIPE_SECRET_KEY
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Flask app
app = Flask(__name__)

# Telegram bot app
application = Application.builder().token(TELEGRAM_TOKEN).build()

# ------------------------- STATE -------------------------
paid_users = {}  # user_id -> expiry datetime
user_phone_numbers = {}  # user_id -> phone
user_last_message = {}  # user_id -> last message
user_waiting_phone = set()
user_waiting_message = set()

# ------------------------- HELPERS -------------------------
def is_paid(user_id: int) -> bool:
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def main_menu_keyboard(user_id: int):
    keyboard = []
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

# ------------------------- TELEGRAM HANDLERS -------------------------
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
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Bot Access (4 days)"},
                    "unit_amount": 2500,
                },
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
        user_waiting_phone.add(user_id)
        await query.message.reply_text(
            "📱 Send me your phone number in the format +1XXXXXXXXXX\n\n"
            "Or press '↩️ Back to Menu'.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
        )

    elif query.data == "make_call":
        if not is_paid(user_id):
            await query.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
            return
        phone = user_phone_numbers.get(user_id)
        if not phone:
            await query.message.reply_text("❌ No phone set. Please set a phone number first.", reply_markup=main_menu_keyboard(user_id))
            return
        user_waiting_message.add(user_id)
        await query.message.reply_text(
            "📞 Send me your custom message for the call.\nOr type /call to reuse your last message.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")],
                [InlineKeyboardButton("ℹ️ Info / Usage", callback_data="help")]
            ])
        )

    elif query.data == "help":
        await query.message.reply_text(
            "ℹ️ Usage Guide:\n"
            "1️⃣ Pay $25 to unlock features.\n"
            "2️⃣ Set a phone number.\n"
            "3️⃣ Make calls with custom messages.\n"
            "✅ OTPs will appear here in Telegram.",
            reply_markup=main_menu_keyboard(user_id)
        )

    elif query.data == "menu":
        await query.message.reply_text("Main Menu:", reply_markup=main_menu_keyboard(user_id))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id in user_waiting_phone:
        if text.startswith("+") and text[1:].isdigit():
            user_phone_numbers[user_id] = text
            user_waiting_phone.remove(user_id)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📞 Make Call", callback_data="make_call")],
                [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")],
                [InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")]
            ])
            await update.message.reply_text(f"✅ Phone number saved: {text}", reply_markup=keyboard)
        else:
            await update.message.reply_text("❌ Invalid format. Use +1XXXXXXXXXX.")

    elif user_id in user_waiting_message:
        user_last_message[user_id] = text
        user_waiting_message.remove(user_id)
        await update.message.reply_text("📞 Message saved. Use /call to place the call now.")

# Command /call
async def call_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_paid(user_id):
        await update.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
        return
    phone = user_phone_numbers.get(user_id)
    if not phone:
        await update.message.reply_text("❌ No phone set.")
        return
    message = user_last_message.get(user_id, "This is your call.")
    call = twilio_client.calls.create(
        to=phone,
        from_=TWILIO_PHONE_NUMBER,
        twiml=f"<Response><Say>{message}</Say></Response>"
    )
    await update.message.reply_text(f"📞 Call placed to {phone} (SID: {call.sid})")

# ------------------------- FLASK ROUTES -------------------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.get_event_loop().create_task(application.update_queue.put(update))
    return "ok", 200

@app.route("/setwebhook")
def set_webhook():
    webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}"
    application.bot.set_webhook(webhook_url)
    return f"Webhook set to {webhook_url}"

@app.route("/success")
def success():
    user_id = int(request.args.get("user_id"))
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return "✅ Payment successful. You now have 4 days access."

@app.route("/cancel")
def cancel():
    return "❌ Payment canceled."

# ------------------------- REGISTER TELEGRAM HANDLERS -------------------------
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("call", call_command))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

# ------------------------- RUN FLASK -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
