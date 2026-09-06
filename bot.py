import asyncio
import logging
import os
import re
import json
import base64
import sqlite3
import threading
import httpx
from dotenv import load_dotenv

try:
    import firebase_admin
    from firebase_admin import credentials, db
    HAS_FIREBASE_LIB = True
except ImportError:
    HAS_FIREBASE_LIB = False

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

load_dotenv()

# ---------------- CONFIGURATION ----------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN পরিবেশক ভ্যারিয়েবল পাওয়া যায়নি! .env ফাইল বা এনভায়রনমেন্ট চেক করুন।")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
OTP_GROUP_ID = os.environ.get("OTP_GROUP_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
API_URL = os.environ.get("API_URL")

FIREBASE_JSON_PATH = "temp_firebase.json"
CURRENT_DB_MODE = "SQLite (Local)"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)


def get_db_connection():
    return sqlite3.connect("bot_database.db", timeout=10)


def init_sqlite():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS services (
            service_name TEXT,
            country_name TEXT,
            PRIMARY KEY (service_name, country_name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT,
            country TEXT,
            number TEXT,
            status TEXT DEFAULT 'available',
            user_id INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS allocations (
            number TEXT PRIMARY KEY,
            user_id INTEGER,
            service TEXT,
            country TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS seen_otps (
            msg_id TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_sqlite()


def init_firebase_system(run_migration=False):
    global CURRENT_DB_MODE
    if not HAS_FIREBASE_LIB:
        return False

    if firebase_admin._apps:
        CURRENT_DB_MODE = "Firebase (Cloud)"
        if run_migration:
            migrate_sqlite_to_firebase()
        return True

    cred_dict = None
    firebase_b64 = os.environ.get("FIREBASE_BASE64")
    firebase_json_env = os.environ.get("FIREBASE_CONFIG_JSON")

    try:
        if firebase_b64:
            decoded_json = base64.b64decode(firebase_b64).decode('utf-8')
            cred_dict = json.loads(decoded_json)
        elif firebase_json_env:
            cred_dict = json.loads(firebase_json_env)
            if "private_key" in cred_dict:
                cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
        elif os.path.exists(FIREBASE_JSON_PATH):
            with open(FIREBASE_JSON_PATH, "r") as f:
                cred_dict = json.load(f)

        if cred_dict:
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
            CURRENT_DB_MODE = "Firebase (Cloud)"
            if run_migration:
                migrate_sqlite_to_firebase()
            logging.info("Firebase connected successfully!")
            return True
    except Exception as e:
        logging.error(f"Firebase Init Error: {e}")

    CURRENT_DB_MODE = "SQLite (Local)"
    return False


def migrate_sqlite_to_firebase():
    """Migrate SQLite data to Firebase Realtime DB without creating duplicate items"""
    if not firebase_admin._apps:
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT service, country, number, status, user_id FROM numbers")
    rows = cursor.fetchall()
    for row in rows:
        srv, cnt, num, st, uid = row
        db.reference(f"numbers/{srv}/{cnt}/{num}").set({"number": num, "status": st, "user_id": uid})
        db.reference(f"services/{srv}/{cnt}").set(True)

    cursor.execute("SELECT number, user_id, service, country FROM allocations")
    rows = cursor.fetchall()
    for row in rows:
        num, uid, srv, cnt = row
        db.reference(f"allocations/{num}").set({"user_id": uid, "service": srv, "country": cnt})

    conn.close()


init_firebase_system(run_migration=False)

app = Flask(__name__)

@app.route('/')
def home():
    return f"Bot running! Current DB Mode: {CURRENT_DB_MODE}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

ADD_SERVICE, ADD_COUNTRY, ADD_NUMBERS, WAIT_FIREBASE_FILE = range(4)


def get_main_keyboard(user_id: int):
    keyboard = [
        ["Get number"],
        ["Channel", "Support"]
    ]
    if user_id == ADMIN_ID:
        keyboard.append(["Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    msg = f"Welcome!\nSelect an option from menu: **{CURRENT_DB_MODE}**"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")


async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "Get number":
        services = []
        if CURRENT_DB_MODE == "Firebase (Cloud)":
            services_ref = db.reference("services").get()
            if services_ref and isinstance(services_ref, dict):
                services = list(services_ref.keys())
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT service_name FROM services")
            services = [row[0] for row in cursor.fetchall()]
            conn.close()

        if not services:
            await update.message.reply_text("বর্তমানে কোনো সার্ভিস এভেলেবল নেই।")
            return

        buttons = [[InlineKeyboardButton(srv, callback_data=f"srv_{srv}")] for srv in services]
        await update.message.reply_text("একটি সার্ভিস সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))

    elif text == "Channel":
        await update.message.reply_text("আমাদের অফিশিয়াল চ্যানেল: https://t.me/your_channel")

    elif text == "Support":
        await update.message.reply_text("যেকোনো সাহায্যের জন্য যোগাযোগ করুন: @your_support")

    elif text == "Admin Panel" and user_id == ADMIN_ID:
        buttons = [
            [InlineKeyboardButton("➕ Add Service & Numbers", callback_data="admin_add_service")],
            [InlineKeyboardButton("📤 Upload Firebase JSON", callback_data="admin_upload_firebase")]
        ]
        await update.message.reply_text(f"**ADMIN PANEL**\n\nবর্তমান ডাটাবেস: **{CURRENT_DB_MODE}**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


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
        valid_count = 0
        if CURRENT_DB_MODE == "Firebase (Cloud)":
            ref = db.reference(f"numbers/{service}/{country}")
            for num in numbers:
                clean_num = re.sub(r'\D', '', num)
                if clean_num:
                    ref.child(clean_num).set({"number": clean_num, "status": "available", "user_id": 0})
                    valid_count += 1
            db.reference(f"services/{service}/{country}").set(True)
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO services (service_name, country_name) VALUES (?, ?)", (service, country))
            for num in numbers:
                clean_num = re.sub(r'\D', '', num)
                if clean_num:
                    cursor.execute("INSERT INTO numbers (service, country, number, status) VALUES (?, ?, ?, 'available')", (service, country, clean_num))
                    valid_count += 1
            conn.commit()
            conn.close()

        await update.message.reply_text(f"সফলভাবে {valid_count} টি নম্বর যোগ করা হয়েছে!", reply_markup=get_main_keyboard(ADMIN_ID))
    else:
        await update.message.reply_text("তথ্য অসম্পূর্ণ ছিল, আবার চেষ্টা করুন।", reply_markup=get_main_keyboard(ADMIN_ID))

    context.user_data.clear()
    return ConversationHandler.END

async def admin_upload_firebase_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    await query.message.reply_text("দয়া করে ফায়ারবেসের `.json` ফাইলটি সেন্ড করুন:")
    return WAIT_FIREBASE_FILE

async def receive_firebase_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document or not update.message.document.file_name.endswith('.json'):
        await update.message.reply_text("ভুল ফাইল! শুধুমাত্র `.json` সার্ভিস একাউন্ট ফাইল আপলোড দিন।")
        return ConversationHandler.END

    file = await context.bot.get_file(update.message.document.file_id)
    await file.download_to_drive(FIREBASE_JSON_PATH)

    success = init_firebase_system(run_migration=True)
    if success:
        await update.message.reply_text("ফায়ারবেস ফাইল রিসিভড! ডাটাবেস সফলভাবে Firebase-এ সুইচেবল ও মাইগ্রেট হয়েছে। 🚀", reply_markup=get_main_keyboard(ADMIN_ID))
    else:
        await update.message.reply_text("ফাইল সেভ হয়েছে কিন্তু ফায়ারবেসে কানেক্ট হতে পারেনি। JSON চেক করুন।", reply_markup=get_main_keyboard(ADMIN_ID))

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("অপারেশন বাতিল করা হয়েছে।", reply_markup=get_main_keyboard(update.effective_user.id))
    return ConversationHandler.END


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    await query.answer()

    if data.startswith("srv_"):
        service = data.split("_", 1)[1]
        countries = []

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            countries_ref = db.reference(f"services/{service}").get()
            if countries_ref and isinstance(countries_ref, dict):
                countries = list(countries_ref.keys())
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT country_name FROM services WHERE service_name = ?", (service,))
            countries = [row[0] for row in cursor.fetchall()]
            conn.close()

        if not countries:
            await query.edit_message_text("এই সার্ভিসে কোনো দেশ পাওয়া যায়নি।")
            return

        buttons = [[InlineKeyboardButton(cnt, callback_data=f"cnt_{service}_{cnt}")] for cnt in countries]
        await query.edit_message_text(f"{service} এর জন্য দেশ নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("cnt_"):
        parts = data.split("_", 2)
        if len(parts) < 3:
            await query.edit_message_text("অবৈধ কমান্ড।")
            return
        
        service, country = parts[1], parts[2]
        assigned_num = None

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            numbers_ref = db.reference(f"numbers/{service}/{country}").get()
            if numbers_ref and isinstance(numbers_ref, dict):
                for key, val in numbers_ref.items():
                    if isinstance(val, dict) and val.get("status") == "available":
                        assigned_num = val.get("number")
                        db.reference(f"numbers/{service}/{country}/{key}").update({"status": "allocated", "user_id": user_id})
                        db.reference(f"allocations/{assigned_num}").set({"user_id": user_id, "service": service, "country": country})
                        break
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            # SQLite Transaction for Race Condition Prevention
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT id, number FROM numbers WHERE service = ? AND country = ? AND status = 'available' LIMIT 1", (service, country))
            row = cursor.fetchone()
            if row:
                num_id, assigned_num = row
                cursor.execute("UPDATE numbers SET status = 'allocated', user_id = ? WHERE id = ?", (user_id, num_id))
                cursor.execute("INSERT OR REPLACE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)", (assigned_num, user_id, service, country))
                conn.commit()
            else:
                conn.rollback()
            conn.close()

        if not assigned_num:
            await query.edit_message_text("দুঃখিত, এই ক্যাটাগরিতে কোনো নম্বর খালি নেই।")
            return

        await query.edit_message_text(f"আপনার নম্বর: `{assigned_num}`\n\nওটিপি আসার সাথে সাথে জানিয়ে দেওয়া হবে।", parse_mode="Markdown")


async def otp_poller(application: Application):
    processed_ids = set()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        seen_ref = db.reference("seen_otp_ids").get()
        if seen_ref and isinstance(seen_ref, dict):
            processed_ids = set(seen_ref.keys())
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT msg_id FROM seen_otps")
        processed_ids = {row[0] for row in cursor.fetchall()}
        conn.close()

    # Async HTTP client to prevent blocking event loop
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                if API_URL:
                    res = await client.get(API_URL)
                    if res.status_code == 200:
                        docs = res.json().get("data", {}).get("docs", [])

                        for item in docs:
                            msg_id = item.get("_id")
                            num = item.get("number")
                            msg = item.get("message")

                            if msg_id and msg_id not in processed_ids:
                                processed_ids.add(msg_id)

                                # Prevent infinite memory growth (Keep last 1000 items)
                                if len(processed_ids) > 2000:
                                    processed_ids = set(list(processed_ids)[-1000:])

                                if CURRENT_DB_MODE == "Firebase (Cloud)":
                                    db.reference(f"seen_otp_ids/{msg_id}").set(True)
                                else:
                                    conn = get_db_connection()
                                    cursor = conn.cursor()
                                    cursor.execute("INSERT OR IGNORE INTO seen_otps (msg_id) VALUES (?)", (msg_id,))
                                    conn.commit()
                                    conn.close()

                                if OTP_GROUP_ID:
                                    try:
                                        await application.bot.send_message(
                                            chat_id=OTP_GROUP_ID,
                                            text=f"📩 **New OTP Received**\n📱 **Number:** `{num}`\n💬 **Message:**\n`{msg}`",
                                            parse_mode="Markdown"
                                        )
                                    except Exception as e:
                                        logging.error(f"Group Forward Error: {e}")

                                allocated_user = None
                                if CURRENT_DB_MODE == "Firebase (Cloud)":
                                    alloc_ref = db.reference(f"allocations/{num}").get()
                                    if alloc_ref and isinstance(alloc_ref, dict):
                                        allocated_user = alloc_ref.get("user_id")
                                else:
                                    conn = get_db_connection()
                                    cursor = conn.cursor()
                                    cursor.execute("SELECT user_id FROM allocations WHERE number = ?", (num,))
                                    row = cursor.fetchone()
                                    if row:
                                        allocated_user = row[0]
                                    conn.close()

                                if allocated_user:
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


def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_add_service_start, pattern="^admin_add_service$"),
            CallbackQueryHandler(admin_upload_firebase_start, pattern="^admin_upload_firebase$")
        ],
        states={
            ADD_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_service_name)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_country_name)],
            ADD_NUMBERS: [MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, receive_numbers)],
            WAIT_FIREBASE_FILE: [MessageHandler(filters.Document.ALL, receive_firebase_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(admin_conv)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    application.add_handler(CallbackQueryHandler(handle_callback))

    async def post_init(app: Application):
        asyncio.create_task(otp_poller(app))

    application.post_init = post_init
    application.run_polling()

if __name__ == "__main__":
    main()
