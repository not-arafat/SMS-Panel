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
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channel', 'https://t.me/your_channel')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('support', '@your_support')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('otp_group_link', 'https://t.me/your_otp_group')")
    conn.commit()
    conn.close()

init_sqlite()


def create_button(text: str, callback_data: str = None, url: str = None, copy_text: str = None, style: str = None) -> dict:
    btn = {"text": text}
    if callback_data:
        btn["callback_data"] = callback_data
    if url:
        btn["url"] = url
    if copy_text:
        btn["copy_text"] = {"text": copy_text}
    if style:
        btn["style"] = style
    return btn


def get_setting(key: str, default_val: str = "") -> str:
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            val = db.reference(f"settings/{key}").get()
            if val:
                return str(val)
        except Exception as e:
            logging.error(f"Error reading setting from Firebase: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logging.error(f"Error reading setting from SQLite: {e}")

    return default_val


def set_setting(key: str, value: str):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"settings/{key}").set(value)
        except Exception as e:
            logging.error(f"Error writing setting to Firebase: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error writing setting to SQLite: {e}")


def get_admin_services_summary():
    summary = {}
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        srv_ref = db.reference("services").get()
        if srv_ref and isinstance(srv_ref, dict):
            for srv in srv_ref.keys():
                summary[srv] = {}
                cnt_ref = db.reference(f"services/{srv}").get()
                if cnt_ref and isinstance(cnt_ref, dict):
                    for cnt in cnt_ref.keys():
                        num_ref = db.reference(f"numbers/{srv}/{cnt}").get()
                        avail_count = 0
                        if num_ref and isinstance(num_ref, dict):
                            for n_key, n_val in num_ref.items():
                                if isinstance(n_val, dict) and n_val.get("status") == "available":
                                    avail_count += 1
                        summary[srv][cnt] = avail_count
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_name, country_name FROM services")
        pairs = cursor.fetchall()
        for srv, cnt in pairs:
            if srv not in summary:
                summary[srv] = {}
            cursor.execute("SELECT COUNT(*) FROM numbers WHERE service = ? AND country = ? AND status = 'available'", (srv, cnt))
            cnt_val = cursor.fetchone()[0]
            summary[srv][cnt] = cnt_val
        conn.close()
    return summary


def delete_service_db(service: str):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"services/{service}").delete()
            db.reference(f"numbers/{service}").delete()
        except Exception as e:
            logging.error(f"Error deleting service from Firebase: {e}")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM services WHERE service_name = ?", (service,))
    cursor.execute("DELETE FROM numbers WHERE service = ?", (service,))
    conn.commit()
    conn.close()


def delete_country_db(service: str, country: str):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"services/{service}/{country}").delete()
            db.reference(f"numbers/{service}/{country}").delete()
        except Exception as e:
            logging.error(f"Error deleting country from Firebase: {e}")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM services WHERE service_name = ? AND country_name = ?", (service, country))
    cursor.execute("DELETE FROM numbers WHERE service = ? AND country = ?", (service, country))
    conn.commit()
    conn.close()


def build_admin_services_view():
    summary = get_admin_services_summary()
    if not summary:
        text = "📱 **SERVICES MANAGEMENT**\n\nবর্তমানে কোনো সার্ভিস যুক্ত করা নেই।"
        buttons = [[create_button("➕ Add New Service", callback_data="adm:srv:add", style="success")]]
        return text, InlineKeyboardMarkup(buttons)

    text = "📱 **SERVICES MANAGEMENT**\n\nনিচে আপনার সার্ভিসসমূহ এবং আনইউজড/এভেলেবল নম্বরের বিবরণ দেওয়া হলো:\n"
    buttons = []
    for srv, cnts in summary.items():
        total_avail = sum(cnts.values())
        text += f"\n🔹 **{srv}** (Total Available: `{total_avail}`)"
        for cnt, count in cnts.items():
            text += f"\n   └ {cnt}: `{count}` টি"
        buttons.append([create_button(f"⚙️ Manage {srv}", callback_data=f"adm:srv:view:{srv}", style="primary")])

    buttons.append([create_button("➕ Add New Service / Numbers", callback_data="adm:srv:add", style="success")])
    return text, InlineKeyboardMarkup(buttons)


def build_service_manage_view(service: str):
    summary = get_admin_services_summary()
    cnts = summary.get(service, {})
    total_avail = sum(cnts.values())

    text = f"⚙️ **SERVICE DETAILS: {service}**\n\n"
    text += f"📊 মোট এভেলেবল নম্বর: `{total_avail}` টি\n\n"
    text += "🏳️ **দেশ এবং আনইউজড নম্বর:**\n"
    if cnts:
        for cnt, count in cnts.items():
            text += f"• **{cnt}**: `{count}` টি এভেলেবল\n"
    else:
        text += "কোনো দেশ যুক্ত নেই।\n"

    buttons = [
        [create_button("➕ Add Country / Numbers", callback_data=f"adm:srv:add:{service}", style="success")],
        [create_button("🗑️ Delete Service", callback_data=f"adm:srv:del:{service}", style="danger")],
    ]
    if cnts:
        buttons.append([create_button("❌ Delete Country", callback_data=f"adm:cnt:delli:{service}", style="danger")])
    buttons.append([create_button("⬅️ Back to Services", callback_data="adm:srv:list", style="danger")])

    return text, InlineKeyboardMarkup(buttons)


def build_global_settings_view():
    ch_val = get_setting("channel", "https://t.me/your_channel")
    sp_val = get_setting("support", "@your_support")
    otp_link = get_setting("otp_group_link", "https://t.me/your_otp_group")
    text = (
        f"⚙️ **GLOBAL SETTINGS**\n\n"
        f"📢 **Channel:** {ch_val}\n"
        f"🎧 **Support:** {sp_val}\n"
        f"🔗 **OTP Group Link:** {otp_link}\n\n"
        f"পরিবর্তন করতে নিচের বাটনে ক্লিক করুন:"
    )
    buttons = [
        [
            create_button("📢 Edit Channel", callback_data="adm:set:channel", style="primary"),
            create_button("🎧 Edit Support", callback_data="adm:set:support", style="primary")
        ],
        [
            create_button("🔗 Edit OTP Group Link", callback_data="adm:set:otplink", style="primary")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_allocation_keyboard(service: str, country: str, number: str):
    otp_group_link = get_setting("otp_group_link", "https://t.me/your_otp_group")
    buttons = [
        [create_button(f"{number}", copy_text=number, style="success")],
        [
            create_button("Change", callback_data=f"change_{service}_{country}_{number}", style="primary"),
            create_button("OTP Group", url=otp_group_link, style="primary")
        ],
        [create_button("Back", callback_data=f"srv_{service}", style="danger")]
    ]
    return InlineKeyboardMarkup(buttons)


def get_services_keyboard():
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
        return None, "বর্তমানে কোনো সার্ভিস এভেলেবল নেই।"

    buttons = [[create_button(srv, callback_data=f"srv_{srv}", style="primary")] for srv in services]
    return InlineKeyboardMarkup(buttons), "একটি সার্ভিস সিলেক্ট করুন:"


def init_firebase_system(run_migration=False, force_reinit=False):
    global CURRENT_DB_MODE
    if not HAS_FIREBASE_LIB:
        CURRENT_DB_MODE = "SQLite (Local)"
        return False

    if force_reinit and firebase_admin._apps:
        try:
            for app_name in list(firebase_admin._apps.keys()):
                firebase_admin.delete_app(firebase_admin._apps[app_name])
        except Exception as e:
            logging.error(f"Error deleting previous firebase instance: {e}")

    if firebase_admin._apps:
        CURRENT_DB_MODE = "Firebase (Cloud)"
        if run_migration:
            migrate_sqlite_to_firebase()
        return True

    cred_dict = None
    firebase_b64 = os.environ.get("FIREBASE_BASE64")
    firebase_json_env = os.environ.get("FIREBASE_CONFIG_JSON")

    try:
        if os.path.exists(FIREBASE_JSON_PATH):
            with open(FIREBASE_JSON_PATH, "r") as f:
                cred_dict = json.load(f)
        elif firebase_b64:
            decoded_json = base64.b64decode(firebase_b64).decode('utf-8')
            cred_dict = json.loads(decoded_json)
        elif firebase_json_env:
            cred_dict = json.loads(firebase_json_env)
            if "private_key" in cred_dict:
                cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")

        if cred_dict:
            cred = credentials.Certificate(cred_dict)
            options = {}
            if DATABASE_URL:
                options['databaseURL'] = DATABASE_URL
            firebase_admin.initialize_app(cred, options if options else None)
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

    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    for row in rows:
        k, v = row
        db.reference(f"settings/{k}").set(v)

    conn.close()


init_firebase_system(run_migration=False)

app = Flask(__name__)

@app.route('/')
def home():
    return f"Bot running! Current DB Mode: {CURRENT_DB_MODE}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# States for Admin Conversations
(
    ADD_SERVICE,
    ADD_COUNTRY,
    ADD_NUMBERS,
    WAIT_FIREBASE_FILE,
    WAIT_CHANNEL,
    WAIT_SUPPORT,
    WAIT_OTP_LINK,
) = range(7)


# ---------------- KEYBOARDS ----------------
def get_main_keyboard(user_id: int):
    keyboard = [
        ["Get number"],
        ["Channel", "Support"]
    ]
    if user_id == ADMIN_ID:
        keyboard.append(["Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_admin_keyboard():
    keyboard = [
        ["Services", "Upload Firebase"],
        ["Global Settings"],
        ["Back"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ---------------- BOT HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'main'
    user_id = update.effective_user.id
    msg = f"Welcome!\nSelect an option from menu: **{CURRENT_DB_MODE}**"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")


async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "Get number":
        kbd, msg = get_services_keyboard()
        if not kbd:
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=kbd)

    elif text == "Channel":
        ch_link = get_setting("channel", "https://t.me/your_channel")
        await update.message.reply_text(f"📢 আমাদের অফিশিয়াল চ্যানেল: {ch_link}")

    elif text == "Support":
        sp_link = get_setting("support", "@your_support")
        await update.message.reply_text(f"🎧 যেকোনো সাহায্যের জন্য যোগাযোগ করুন: {sp_link}")

    elif text == "Admin Panel" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        await update.message.reply_text(
            f"**ADMIN PANEL**\n\nবর্তমান ডাটাবেস: **{CURRENT_DB_MODE}**",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )

    elif text == "Services" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_services_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text == "Global Settings" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_global_settings_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text == "Back":
        context.user_data['current_menu'] = 'main'
        await update.message.reply_text("প্রধান মেনু:", reply_markup=get_main_keyboard(user_id))


# ---------------- CONVERSATION HANDLERS (ADMIN) ----------------
async def admin_add_service_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    await query.message.reply_text("সার্ভিসের নাম লিখুন (যেমন: TikTok, Facebook):")
    return ADD_SERVICE

async def admin_add_service_with_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    service = query.data.split(":", 3)[3]
    context.user_data['service_name'] = service
    await query.message.reply_text(f"সার্ভিস **{service}** সিলেক্ট করা হয়েছে।\n\nদেশের নাম লিখুন (যেমন: Bangladesh, Nepal):", parse_mode="Markdown")
    return ADD_COUNTRY

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

        await update.message.reply_text(f"সফলভাবে {valid_count} টি নম্বর যোগ করা হয়েছে!", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("তথ্য অসম্পূর্ণ ছিল, আবার চেষ্টা করুন।", reply_markup=get_admin_keyboard())

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'admin'
    return ConversationHandler.END

async def admin_upload_firebase_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("দয়া করে ফায়ারবেসের `.json` ফাইলটি সেন্ড করুন:")
    else:
        await update.message.reply_text("দয়া করে ফায়ারবেসের `.json` ফাইলটি সেন্ড করুন:")
    return WAIT_FIREBASE_FILE

async def receive_firebase_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document or not update.message.document.file_name.endswith('.json'):
        await update.message.reply_text("ভুল ফাইল! শুধুমাত্র `.json` সার্ভিস একাউন্ট ফাইল আপলোড দিন।", reply_markup=get_admin_keyboard())
        return ConversationHandler.END

    file = await context.bot.get_file(update.message.document.file_id)
    await file.download_to_drive(FIREBASE_JSON_PATH)

    success = init_firebase_system(run_migration=True, force_reinit=True)
    if success:
        await update.message.reply_text("ফায়ারবেস ফাইল রিসিভড! ডাটাবেস সফলভাবে Firebase-এ সুইচেবল ও মাইগ্রেট হয়েছে। 🚀", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("ফাইল সেভ হয়েছে কিন্তু ফায়ারবেসে কানেক্ট হতে পারেনি। JSON চেক করুন।", reply_markup=get_admin_keyboard())

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'admin'
    return ConversationHandler.END

# Global Settings Handlers
async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        if query.from_user.id != ADMIN_ID:
            return ConversationHandler.END
        await query.message.reply_text("নতুন চ্যানেল লিঙ্কটি লিখুন (যেমন: https://t.me/your_channel):")
    return WAIT_CHANNEL

async def receive_channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    set_setting("channel", new_link)
    await update.message.reply_text(f"✅ সফলভাবে চ্যানেল লিঙ্ক আপডেট করা হয়েছে!\nবর্তমান লিঙ্ক: {new_link}")
    text_msg, kbd = build_global_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

async def set_support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        if query.from_user.id != ADMIN_ID:
            return ConversationHandler.END
        await query.message.reply_text("নতুন সাপোর্ট ইউজারনেম/লিঙ্ক লিখুন (যেমন: @your_support):")
    return WAIT_SUPPORT

async def receive_support_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    set_setting("support", new_link)
    await update.message.reply_text(f"✅ সফলভাবে সাপোর্ট ইউজারনেম/লিঙ্ক আপডেট করা হয়েছে!\nবর্তমান সাপোর্ট: {new_link}")
    text_msg, kbd = build_global_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

async def set_otplink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        if query.from_user.id != ADMIN_ID:
            return ConversationHandler.END
        await query.message.reply_text("নতুন OTP Group লিঙ্ক লিখুন (যেমন: https://t.me/your_otp_group):")
    return WAIT_OTP_LINK

async def receive_otp_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    set_setting("otp_group_link", new_link)
    await update.message.reply_text(f"✅ সফলভাবে OTP Group লিঙ্ক আপডেট করা হয়েছে!\nবর্তমান লিঙ্ক: {new_link}")
    text_msg, kbd = build_global_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)

    text = update.message.text if update.message else ""

    if text == "Global Settings" and user_id == ADMIN_ID:
        text_msg, kbd = build_global_settings_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    elif text == "Admin Panel" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        await update.message.reply_text(
            f"**ADMIN PANEL**\n\nবর্তমান ডাটাবেস: **{CURRENT_DB_MODE}**",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )
    elif text == "Services" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_services_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    else:
        current_menu = context.user_data.get('current_menu', 'main')
        if current_menu == 'admin':
            reply_kbd = get_admin_keyboard()
        else:
            reply_kbd = get_main_keyboard(user_id)

        await update.message.reply_text("অপারেশন বাতিল করা হয়েছে।", reply_markup=reply_kbd)

    return ConversationHandler.END


# ---------------- INLINE CALLBACK HANDLER ----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "back_to_services":
        await query.answer()
        kbd, msg = get_services_keyboard()
        if not kbd:
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text(msg, reply_markup=kbd)
        return

    # Admin Management Actions
    if data == "adm:srv:list":
        await query.answer()
        if user_id != ADMIN_ID:
            return
        text, kbd = build_admin_services_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:srv:view:"):
        await query.answer()
        if user_id != ADMIN_ID:
            return
        service = data.split(":", 3)[3]
        text, kbd = build_service_manage_view(service)
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:srv:del:"):
        if user_id != ADMIN_ID:
            await query.answer()
            return
        service = data.split(":", 3)[3]
        delete_service_db(service)
        await query.answer(f"{service} সার্ভিসটি সফলভাবে ডিলিট করা হয়েছে!", show_alert=True)
        text, kbd = build_admin_services_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:cnt:delli:"):
        await query.answer()
        if user_id != ADMIN_ID:
            return
        service = data.split(":", 3)[3]
        summary = get_admin_services_summary()
        cnts = summary.get(service, {})
        buttons = []
        for cnt in cnts.keys():
            buttons.append([create_button(f"❌ Delete {cnt}", callback_data=f"adm:cnt:del:{service}:{cnt}", style="danger")])
        buttons.append([create_button("⬅️ Back", callback_data=f"adm:srv:view:{service}", style="danger")])
        await query.edit_message_text(f"**{service}** থেকে কোন দেশটি ডিলিট করতে চান নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("adm:cnt:del:"):
        if user_id != ADMIN_ID:
            await query.answer()
            return
        parts = data.split(":", 4)
        if len(parts) >= 5:
            service, country = parts[3], parts[4]
            delete_country_db(service, country)
            await query.answer(f"{service} থেকে {country} ডিলিট করা হয়েছে!", show_alert=True)
            text, kbd = build_service_manage_view(service)
            await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    # User Get Number Flow
    elif data.startswith("srv_"):
        await query.answer()
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

        buttons = [[create_button(cnt, callback_data=f"cnt_{service}_{cnt}", style="primary")] for cnt in countries]
        buttons.append([create_button("⬅️ Back", callback_data="back_to_services", style="danger")])
        await query.edit_message_text(f"{service} এর জন্য দেশ নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("cnt_"):
        await query.answer()
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

        alloc_msg = (
            "━━━━━━━━━━━━━━━\n"
            "Number Allocated \n"
            "— — — — — — — — — —\n"
            f"📘 {service} ➜ {country}\n"
            "━━━━━━━━━━━━━━━"
        )
        kbd = build_allocation_keyboard(service, country, assigned_num)
        await query.edit_message_text(alloc_msg, reply_markup=kbd)

    elif data.startswith("change_"):
        parts = data.split("_", 3)
        if len(parts) < 4:
            await query.answer("অবৈধ অনুরোধ!", show_alert=True)
            return

        service, country, old_number = parts[1], parts[2], parts[3]
        new_num = None

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            db.reference(f"numbers/{service}/{country}/{old_number}").update({"status": "available", "user_id": 0})
            db.reference(f"allocations/{old_number}").delete()

            numbers_ref = db.reference(f"numbers/{service}/{country}").get()
            if numbers_ref and isinstance(numbers_ref, dict):
                for key, val in numbers_ref.items():
                    if isinstance(val, dict) and val.get("status") == "available" and val.get("number") != old_number:
                        new_num = val.get("number")
                        db.reference(f"numbers/{service}/{country}/{key}").update({"status": "allocated", "user_id": user_id})
                        db.reference(f"allocations/{new_num}").set({"user_id": user_id, "service": service, "country": country})
                        break

            if not new_num:
                db.reference(f"numbers/{service}/{country}/{old_number}").update({"status": "allocated", "user_id": user_id})
                db.reference(f"allocations/{old_number}").set({"user_id": user_id, "service": service, "country": country})
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("UPDATE numbers SET status = 'available', user_id = 0 WHERE number = ?", (old_number,))
            cursor.execute("DELETE FROM allocations WHERE number = ?", (old_number,))

            cursor.execute(
                "SELECT id, number FROM numbers WHERE service = ? AND country = ? AND status = 'available' AND number != ? LIMIT 1",
                (service, country, old_number)
            )
            row = cursor.fetchone()
            if row:
                num_id, new_num = row
                cursor.execute("UPDATE numbers SET status = 'allocated', user_id = ? WHERE id = ?", (user_id, num_id))
                cursor.execute("INSERT OR REPLACE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)", (new_num, user_id, service, country))
                conn.commit()
            else:
                cursor.execute("UPDATE numbers SET status = 'allocated', user_id = ? WHERE number = ?", (user_id, old_number))
                cursor.execute("INSERT OR REPLACE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)", (old_number, user_id, service, country))
                conn.commit()
            conn.close()

        if new_num:
            await query.answer("successfully changed", show_alert=False)
            alloc_msg = (
                "━━━━━━━━━━━━━━━\n"
                "Number Allocated \n"
                "— — — — — — — — — —\n"
                f"📘 {service} ➜ {country}\n"
                "━━━━━━━━━━━━━━━"
            )
            kbd = build_allocation_keyboard(service, country, new_num)
            await query.edit_message_text(alloc_msg, reply_markup=kbd)
        else:
            await query.answer("দুঃখিত, কোনো নতুন নম্বর খালি নেই!", show_alert=True)


# ---------------- OTP POLLING SERVICE ----------------
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


# ---------------- MAIN FUNCTION ----------------
def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_add_service_start, pattern="^adm:srv:add$"),
            CallbackQueryHandler(admin_add_service_with_name, pattern="^adm:srv:add:"),
            CallbackQueryHandler(admin_upload_firebase_start, pattern="^admin_upload_firebase$"),
            CallbackQueryHandler(set_channel_start, pattern="^adm:set:channel$"),
            CallbackQueryHandler(set_support_start, pattern="^adm:set:support$"),
            CallbackQueryHandler(set_otplink_start, pattern="^adm:set:otplink$"),
            MessageHandler(filters.Regex("^Upload Firebase$") & filters.User(user_id=ADMIN_ID), admin_upload_firebase_start),
        ],
        states={
            ADD_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_service_name)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_country_name)],
            ADD_NUMBERS: [MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, receive_numbers)],
            WAIT_FIREBASE_FILE: [MessageHandler(filters.Document.ALL, receive_firebase_file)],
            WAIT_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_link)],
            WAIT_SUPPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_support_link)],
            WAIT_OTP_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_otp_link)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Back|Cancel|Admin Panel|Global Settings|Services|Get number)$"), cancel)
        ],
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
