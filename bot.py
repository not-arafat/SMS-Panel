import asyncio
import logging
import os
import re
import json
import base64
import threading
import requests
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Load environment variables
load_dotenv()

# ---------------- CONFIGURATION ----------------
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
OTP_GROUP_ID = os.environ.get("OTP_GROUP_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
API_URL = os.environ.get("API_URL")

# ---------------- FIREBASE INITIALIZATION ----------------
if not firebase_admin._apps:
    firebase_b64 = os.environ.get("FIREBASE_BASE64")
    firebase_json_env = os.environ.get("FIREBASE_CONFIG_JSON")

    try:
        if firebase_b64:
            # 1. Base64 Method (Recommended)
            decoded_json = base64.b64decode(firebase_b64).decode('utf-8')
            cred_dict = json.loads(decoded_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
            logging.info("Firebase connected via Base64 ENV!")
        elif firebase_json_env:
            # 2. Raw JSON String Method
            cred_dict = json.loads(firebase_json_env)
            if "private_key" in cred_dict:
                cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
            logging.info("Firebase connected via JSON String ENV!")
        else:
            # 3. File Fallback
            cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "serviceAccountKey.json")
            if os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
                logging.info("Firebase connected via Local File!")
            else:
                logging.critical("CRITICAL: No valid Firebase credentials found!")
    except Exception as e:
        logging.critical(f"CRITICAL: Firebase Initialization Failed: {e}")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ---------------- FLASK SERVER FOR RENDER ----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running perfectly!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Conversation States
ADD_SERVICE, ADD_COUNTRY, ADD_NUMBERS = range(3)

# Main Keyboard
def get_main_keyboard(user_id: int):
    keyboard = [
        ["Get number"],
        ["Channel", "Support"]
    ]
    if user_id == ADMIN_ID:
        keyboard.append(["Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------------- TELEGRAM BOT HANDLERS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()  # Reset any stuck conversation state
    user_id = update.effective_user.id
    await update.message.reply_text("স্বাগতম! নিচের মেনু থেকে অপশন নির্বাচন করুন:", reply_markup=get_main_keyboard(user_id))

async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "Get number":
        try:
            services_ref = db.reference("services").get()
            if not services_ref:
                await update.message.reply_text("বর্তমানে কোনো সার্ভিস এভেলেবল নেই।")
                return
            
            buttons = []
            for service_name in services_ref.keys():
                buttons.append([InlineKeyboardButton(service_name, callback_data=f"srv_{service_name}")])
            
            await update.message.reply_text("একটি সার্ভিস সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logging.error(f"Error getting services: {e}")
            await update.message.reply_text("ডাটাবেস কানেকশনে সমস্যা হচ্ছে। একটু পরে চেষ্টা করুন।")

    elif text == "Channel":
        await update.message.reply_text("আমাদের অফিশিয়াল চ্যানেল: https://t.me/your_channel")

    elif text == "Support":
        await update.message.reply_text("যেকোনো সাহায্যের জন্য যোগাযোগ করুন: @your_support")

    elif text == "Admin Panel" and user_id == ADMIN_ID:
        buttons = [
            [InlineKeyboardButton("➕ Add Service & Numbers", callback_data="admin_add_service")]
        ]
        await update.message.reply_text("এডমিন প্যানেল:", reply_markup=InlineKeyboardMarkup(buttons))

# ---------------- ADMIN CONVERSATION FLOW ----------------

async def admin_add_service_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    await query.message.reply_text("সার্ভিসের নাম লিখুন (যেমন: TikTok, Facebook):")
    return ADD_SERVICE

async def receive_service_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['service_name'] = update.message.text.strip()
    await update.message.reply_text("দেশের নাম লিখুন (যেমন: Bangladesh, Nepal):")
    return ADD_COUNTRY

async def receive_country_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['country_name'] = update.message.text.strip()
    await update.message.reply_text("নম্বরগুলো পাঠাও (টেক্সট ফাইল অথবা প্রতি লাইনে একটি করে নম্বর):")
    return ADD_NUMBERS

async def receive_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    numbers = []
    if update.message.document:
        file = await context.bot.get_file(update.message.document.file_id)
        content = (await file.download_as_bytearray()).decode('utf-8')
        numbers = [line.strip() for line in content.splitlines() if line.strip()]
    elif update.message.text:
        numbers = [line.strip() for line in update.message.text.splitlines() if line.strip()]

    service = context.user_data.get('service_name')
    country = context.user_data.get('country_name')

    if service and country and numbers:
        ref = db.reference(f"numbers/{service}/{country}")
        for num in numbers:
            clean_num = re.sub(r'\D', '', num)
            if clean_num:
                ref.push({"number": clean_num, "status": "available"})

        db.reference(f"services/{service}/{country}").set(True)
        await update.message.reply_text(f"সফলভাবে {len(numbers)} টি নম্বর যোগ করা হয়েছে!", reply_markup=get_main_keyboard(ADMIN_ID))
    else:
        await update.message.reply_text("তথ্য অসম্পূর্ণ ছিল, আবার চেষ্টা করুন।", reply_markup=get_main_keyboard(ADMIN_ID))

    context.user_data.clear()  # Clear state after completion
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("অপারেশন বাতিল করা হয়েছে।", reply_markup=get_main_keyboard(update.effective_user.id))
    return ConversationHandler.END

# ---------------- INLINE KEYBOARD CALLBACKS ----------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    await query.answer()

    if data.startswith("srv_"):
        service = data.split("_")[1]
        countries_ref = db.reference(f"services/{service}").get()
        if not countries_ref:
            await query.edit_message_text("এই সার্ভিসে কোনো দেশ পাওয়া যায়নি।")
            return
        
        buttons = []
        for country in countries_ref.keys():
            buttons.append([InlineKeyboardButton(country, callback_data=f"cnt_{service}_{country}")])
        
        await query.edit_message_text(f"{service} এর জন্য দেশ নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("cnt_"):
        _, service, country = data.split("_")
        numbers_ref = db.reference(f"numbers/{service}/{country}").get()
        
        assigned_num = None
        assigned_key = None

        if numbers_ref:
            for key, val in numbers_ref.items():
                if val.get("status") == "available":
                    assigned_num = val.get("number")
                    assigned_key = key
                    break

        if not assigned_num:
            await query.edit_message_text("দুঃখিত, এই ক্যাটাগরিতে কোনো নম্বর খালি নেই।")
            return

        db.reference(f"numbers/{service}/{country}/{assigned_key}").update({"status": "allocated", "user_id": user_id})
        db.reference(f"allocations/{assigned_num}").set({"user_id": user_id, "service": service, "country": country})

        await query.edit_message_text(f"আপনার নম্বর: `{assigned_num}`\n\nওটিপি আসার সাথে সাথে জানিয়ে দেওয়া হবে।", parse_mode="Markdown")

# ---------------- OTP POLLING WORKER ----------------

async def otp_poller(application: Application):
    processed_ids = set()
    seen_ref = db.reference("seen_otp_ids").get()
    if seen_ref:
        processed_ids = set(seen_ref.keys())

    while True:
        try:
            res = requests.get(API_URL, timeout=10).json()
            docs = res.get("data", {}).get("docs", [])

            for item in docs:
                msg_id = item.get("_id")
                num = item.get("number")
                msg = item.get("message")

                if msg_id and msg_id not in processed_ids:
                    processed_ids.add(msg_id)
                    db.reference(f"seen_otp_ids/{msg_id}").set(True)

                    # Forward to group
                    if OTP_GROUP_ID:
                        try:
                            await application.bot.send_message(
                                chat_id=OTP_GROUP_ID, 
                                text=f"📩 **New OTP Received**\n📱 **Number:** `{num}`\n💬 **Message:**\n`{msg}`", 
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logging.error(f"Group Forward Error: {e}")

                    # Forward to assigned user
                    alloc_ref = db.reference(f"allocations/{num}").get()
                    if alloc_ref:
                        allocated_user = alloc_ref.get("user_id")
                        try:
                            await application.bot.send_message(
                                chat_id=allocated_user, 
                                text=f"🎉 **আপনার OTP কোড এসেছে!**\n📱 **নম্বর:** `{num}`\n💬 **মেসেজ:**\n`{msg}`", 
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logging.error(f"User Forward Error: {e}")

        except Exception as e:
            logging.error(f"Polling Exception: {e}")

        await asyncio.sleep(5)

# ---------------- MAIN APPLICATION ----------------

def main():
    # Flask Background Thread
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    # Admin Conversation Handler
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_service_start, pattern="^admin_add_service$")],
        states={
            ADD_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_service_name)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_country_name)],
            ADD_NUMBERS: [MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, receive_numbers)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    # Handlers Registration
    application.add_handler(CommandHandler("start", start))
    application.add_handler(admin_conv)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Post Init Task for OTP Polling
    async def post_init(app: Application):
        asyncio.create_task(otp_poller(app))

    application.post_init = post_init
    application.run_polling()

if __name__ == "__main__":
    main()
