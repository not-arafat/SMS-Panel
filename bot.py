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
    raise ValueError("BOT_TOKEN environment variable not found! Check your .env file or environment settings.")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
OTP_GROUP_ID = os.environ.get("OTP_GROUP_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
API_URL = os.environ.get("API_URL")

FIREBASE_JSON_PATH = "temp_firebase.json"
CURRENT_DB_MODE = "SQLite (Local)"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

MENU_FILTER = filters.Regex("^(Get Number|Profile|Wallet|Channel|Support|Admin Panel|Services|Upload Firebase|Global Settings|Edit Links|Edit API|Number Quantity|Admin Control|Broadcast|Back)$")


def escape_md(text: str) -> str:
    """Markdown special characters escape logic"""
    if not text:
        return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def clean_tg_link(val: str) -> str:
    """Converts usernames (@user), t.me links, or standard URLs into valid HTTPS Telegram links."""
    if not val:
        return "https://t.me"
    val = val.strip()
    if val.startswith("http://") or val.startswith("https://"):
        return val
    if val.startswith("t.me/"):
        return f"https://{val}"
    if val.startswith("@"):
        return f"https://t.me/{val[1:]}"
    return f"https://t.me/{val}"


def get_db_connection():
    return sqlite3.connect("bot_database.db", timeout=10)


def init_sqlite():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY
        )
    ''')
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
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('number_quantity', '2')")
    conn.commit()
    conn.close()

init_sqlite()


def save_user(user_id: int):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"users/{user_id}").set(True)
        except Exception as e:
            logging.error(f"Error saving user to Firebase: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error saving user to SQLite: {e}")


def get_all_users() -> list:
    users = []
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_users = db.reference("users").get()
            if fb_users and isinstance(fb_users, dict):
                users = [int(uid) for uid in fb_users.keys() if uid.isdigit()]
        except Exception as e:
            logging.error(f"Error fetching users from Firebase: {e}")

    if not users:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            users = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logging.error(f"Error fetching users from SQLite: {e}")

    return list(set(users))


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
            if val is not None:
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


def sync_firebase_to_sqlite():
    if not HAS_FIREBASE_LIB or not firebase_admin._apps:
        return
    try:
        fb_settings = db.reference("settings").get()
        if fb_settings and isinstance(fb_settings, dict):
            conn = get_db_connection()
            cursor = conn.cursor()
            for k, v in fb_settings.items():
                if v:
                    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (str(k), str(v)))
            conn.commit()
            conn.close()

        fb_users = db.reference("users").get()
        if fb_users and isinstance(fb_users, dict):
            conn = get_db_connection()
            cursor = conn.cursor()
            for uid in fb_users.keys():
                if uid.isdigit():
                    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (int(uid),))
            conn.commit()
            conn.close()
    except Exception as e:
        logging.error(f"Error syncing Firebase data to SQLite: {e}")


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
        text = "📱 **SERVICES MANAGEMENT**\n\nNo services added yet."
        buttons = [[create_button("➕ Add New Service", callback_data="adm:srv:add", style="success")]]
        return text, InlineKeyboardMarkup(buttons)

    text = "📱 **SERVICES MANAGEMENT**\n\nBelow is the summary of your added services and available numbers:\n"
    buttons = []
    for srv, cnts in summary.items():
        total_avail = sum(cnts.values())
        text += f"\n🔹 **{escape_md(srv)}** (Total Available: `{total_avail}`)"
        for cnt, count in cnts.items():
            text += f"\n   └ {escape_md(cnt)}: `{count}`"
        buttons.append([create_button(f"⚙️ Manage {srv}", callback_data=f"adm:srv:view:{srv}", style="primary")])

    buttons.append([create_button("➕ Add New Service / Numbers", callback_data="adm:srv:add", style="success")])
    return text, InlineKeyboardMarkup(buttons)


def build_service_manage_view(service: str):
    summary = get_admin_services_summary()
    cnts = summary.get(service, {})
    total_avail = sum(cnts.values())

    text = f"⚙️ **SERVICE DETAILS: {escape_md(service)}**\n\n"
    text += f"📊 Total Available Numbers: `{total_avail}`\n\n"
    text += "🏳️ **Countries & Available Quantities:**\n"
    if cnts:
        for cnt, count in cnts.items():
            text += f"• **{escape_md(cnt)}**: `{count}` available\n"
    else:
        text += "No countries configured.\n"

    buttons = [
        [create_button("➕ Add Country / Numbers", callback_data=f"adm:srv:add:{service}", style="success")],
        [create_button("🗑️ Delete Service", callback_data=f"adm:srv:del:{service}", style="danger")],
    ]
    if cnts:
        buttons.append([create_button("❌ Delete Country", callback_data=f"adm:cnt:delli:{service}", style="danger")])
    buttons.append([create_button("Back to Services", callback_data="adm:srv:list", style="danger")])

    return text, InlineKeyboardMarkup(buttons)


def build_edit_links_view():
    ch_val = get_setting("channel", "https://t.me/your_channel")
    sp_val = get_setting("support", "@your_support")
    otp_link = get_setting("otp_group_link", "https://t.me/your_otp_group")

    text = (
        f"🔗 **EDIT LINKS SETTINGS**\n\n"
        f"📢 **Channel:** {escape_md(ch_val)}\n"
        f"🎧 **Support:** {escape_md(sp_val)}\n"
        f"🔗 **OTP Group Link:** {escape_md(otp_link)}\n\n"
        f"Click below to modify links:"
    )
    buttons = [
        [
            create_button("📢 Edit Channel", callback_data="adm:set:channel", style="primary"),
            create_button("🎧 Edit Support", callback_data="adm:set:support", style="primary")
        ],
        [
            create_button("🔗 Edit Group Link", callback_data="adm:set:otplink", style="primary")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_number_quantity_view():
    current_qty = get_setting("number_quantity", "2")
    text = f"🔢 **NUMBER QUANTITY SETTINGS**\n\nSelect how many numbers a user receives per request.\nCurrent setting: `{current_qty}`"
    buttons = [
        [
            create_button("1", callback_data="adm:setqty:1", style="primary" if current_qty != "1" else "success"),
            create_button("2", callback_data="adm:setqty:2", style="primary" if current_qty != "2" else "success"),
            create_button("3", callback_data="adm:setqty:3", style="primary" if current_qty != "3" else "success")
        ],
        [
            create_button("4", callback_data="adm:setqty:4", style="primary" if current_qty != "4" else "success"),
            create_button("5", callback_data="adm:setqty:5", style="primary" if current_qty != "5" else "success"),
            create_button("6", callback_data="adm:setqty:6", style="primary" if current_qty != "6" else "success")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_allocation_keyboard(service: str, country: str, numbers: list):
    otp_group_link = clean_tg_link(get_setting("otp_group_link", "https://t.me/your_otp_group"))
    buttons = []
    for num in numbers:
        buttons.append([create_button(f"{num}", copy_text=str(num), style="success")])

    buttons.append([
        create_button("Change All", callback_data=f"chg:{service}:{country}", style="primary"),
        create_button("OTP Group", url=otp_group_link, style="primary")
    ])
    buttons.append([create_button("Back", callback_data=f"srv_{service}", style="danger")])
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
        return None, "No services currently available."

    buttons = []
    for i in range(0, len(services), 2):
        row = []
        row.append(create_button(services[i], callback_data=f"srv_{services[i]}", style="primary"))
        if i + 1 < len(services):
            row.append(create_button(services[i+1], callback_data=f"srv_{services[i+1]}", style="primary"))
        buttons.append(row)

    return InlineKeyboardMarkup(buttons), "📍 Please select a service:"


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
        sync_firebase_to_sqlite()
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
            
            sync_firebase_to_sqlite()
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

    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    for u in users:
        db.reference(f"users/{u[0]}").set(True)

    cursor.execute("SELECT service, country, number, status, user_id FROM numbers")
    rows = cursor.fetchall()
    for row in rows:
        srv, cnt, num, st, uid = row
        db.reference(f"numbers/{srv}/{cnt}/{num}").set({"number": str(num), "status": st, "user_id": uid})
        db.reference(f"services/{srv}/{cnt}").set(True)

    cursor.execute("SELECT number, user_id, service, country FROM allocations")
    rows = cursor.fetchall()
    for row in rows:
        num, uid, srv, cnt = row
        db.reference(f"allocations/{num}").set({"user_id": uid, "service": srv, "country": cnt})

    existing_fb_settings = db.reference("settings").get() or {}
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    for row in rows:
        k, v = row
        if k not in existing_fb_settings or not existing_fb_settings[k]:
            db.reference(f"settings/{k}").set(v)
        else:
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, existing_fb_settings[k]))

    conn.commit()
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
    WAIT_BROADCAST_MSG,
) = range(8)


# ---------------- KEYBOARDS ----------------
def get_main_keyboard(user_id: int):
    keyboard_layout = [
        [
            {"text": "Get Number", "style": "success"}
        ],
        [
            {"text": "Profile", "style": "primary"},
            {"text": "Wallet", "style": "primary"}
        ],
        [
            {"text": "Channel", "style": "danger"},
            {"text": "Support", "style": "danger"}
        ]
    ]
    if user_id == ADMIN_ID:
        keyboard_layout.append([{"text": "Admin Panel", "style": "danger"}])
        
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


def get_admin_keyboard():
    keyboard_layout = [
        [
            {"text": "Services", "style": "primary"},
            {"text": "Upload Firebase", "style": "primary"}
        ],
        [
            {"text": "Global Settings", "style": "primary"},
            {"text": "Broadcast", "style": "success"}
        ],
        [
            {"text": "Back", "style": "danger"}
        ]
    ]
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


def get_global_settings_keyboard():
    keyboard_layout = [
        [
            {"text": "Edit Links", "style": "primary"},
            {"text": "Edit API", "style": "primary"}
        ],
        [
            {"text": "Number Quantity", "style": "primary"},
            {"text": "Admin Control", "style": "primary"}
        ],
        [
            {"text": "Back", "style": "danger"}
        ]
    ]
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


# ---------------- BOT HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    save_user(user_id)

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'main'
    first_name = escape_md(update.effective_user.first_name or "User")
    msg = f"Welcome, {first_name}!\nPlease select an option from the menu:"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")


async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    save_user(user_id)

    if text in ["Get Number", "Get number"]:
        kbd, msg = get_services_keyboard()
        if not kbd:
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=kbd)

    elif text == "Profile":
        first_name = escape_md(update.effective_user.first_name or "User")
        bot_username = context.bot.username or "bot"
        refer_link = f"https://t.me/{bot_username}?start={user_id}"
        
        profile_text = (
            f"👤 **USER PROFILE**\n\n"
            f"📝 **Name:** {first_name}\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"💰 **Balance:** `0.00 ৳`"
        )
        kbd = InlineKeyboardMarkup([
            [create_button("Referral Link", copy_text=refer_link, style="success")]
        ])
        await update.message.reply_text(profile_text, reply_markup=kbd, parse_mode="Markdown")

    elif text == "Wallet":
        wallet_text = (
            f"👛 **YOUR WALLET**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Balance:** `0.00 ৳`"
        )
        await update.message.reply_text(wallet_text, parse_mode="Markdown")

    elif text == "Channel":
        ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))
        kbd = InlineKeyboardMarkup([
            [create_button("Join Channel", url=ch_link, style="primary")]
        ])
        await update.message.reply_text("Click below to join our official channel:", reply_markup=kbd)

    elif text == "Support":
        sp_link = clean_tg_link(get_setting("support", "@your_support"))
        ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))
        
        kbd = InlineKeyboardMarkup([
            [
                create_button("Support", url=sp_link, style="primary"),
                create_button("Channel", url=ch_link, style="primary")
            ]
        ])
        await update.message.reply_text("Click below to contact support or join our channel:", reply_markup=kbd)

    elif text == "Admin Panel" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        total_users = len(get_all_users())
        await update.message.reply_text(
            f"**ADMIN PANEL**\n\n"
            f"⚙️ DB Mode: **{CURRENT_DB_MODE}**\n"
            f"👥 Total Registered Users: `{total_users}`",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )

    elif text == "Services" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_services_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text == "Global Settings" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text(
            "⚙️ **GLOBAL SETTINGS MENU**\n\nSelect an option from below keyboard:",
            reply_markup=get_global_settings_keyboard(),
            parse_mode="Markdown"
        )

    elif text == "Edit Links" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_edit_links_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text == "Edit API" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text("⚠️ **Edit API feature is currently unavailable.**", parse_mode="Markdown")

    elif text == "Number Quantity" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_number_quantity_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text == "Admin Control" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text("🛠 **ADMIN CONTROL**\n\nSystem control settings panel.", parse_mode="Markdown")

    elif text == "Back":
        curr_menu = context.user_data.get('current_menu', 'main')
        if curr_menu == 'global_settings' and user_id == ADMIN_ID:
            context.user_data['current_menu'] = 'admin'
            total_users = len(get_all_users())
            await update.message.reply_text(
                f"**ADMIN PANEL**\n\n"
                f"⚙️ DB Mode: **{CURRENT_DB_MODE}**\n"
                f"👥 Total Registered Users: `{total_users}`",
                reply_markup=get_admin_keyboard(),
                parse_mode="Markdown"
            )
        else:
            context.user_data['current_menu'] = 'main'
            await update.message.reply_text("Main Menu", reply_markup=get_main_keyboard(user_id))


# ---------------- CONVERSATION HANDLERS (ADMIN) ----------------
async def admin_add_service_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    await query.message.reply_text("Enter the service name (e.g., TikTok, Facebook):")
    return ADD_SERVICE

async def admin_add_service_with_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    service = query.data.split(":", 3)[3]
    context.user_data['service_name'] = service
    await query.message.reply_text(f"Service **{escape_md(service)}** selected.\n\nEnter country name (e.g., Bangladesh, Nepal):", parse_mode="Markdown")
    return ADD_COUNTRY

async def receive_service_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['service_name'] = update.message.text.strip()
    await update.message.reply_text("Enter country name (e.g., Bangladesh, Nepal):")
    return ADD_COUNTRY

async def receive_country_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['country_name'] = update.message.text.strip()
    await update.message.reply_text("Send the numbers (as a text file or one number per line):")
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

        await update.message.reply_text(f"Successfully added {valid_count} numbers!", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("Incomplete data provided. Please try again.", reply_markup=get_admin_keyboard())

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
        await update.callback_query.message.reply_text("Please send the Firebase `.json` service account file:")
    else:
        await update.message.reply_text("Please send the Firebase `.json` service account file:")
    return WAIT_FIREBASE_FILE

async def receive_firebase_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document or not update.message.document.file_name.endswith('.json'):
        await update.message.reply_text("Invalid file! Please upload only `.json` service account files.", reply_markup=get_admin_keyboard())
        return ConversationHandler.END

    file = await context.bot.get_file(update.message.document.file_id)
    await file.download_to_drive(FIREBASE_JSON_PATH)

    success = init_firebase_system(run_migration=True, force_reinit=True)
    if success:
        await update.message.reply_text("Firebase file received! Database successfully connected and migrated to Firebase. 🚀", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("File saved, but failed to connect to Firebase. Please check the JSON content.", reply_markup=get_admin_keyboard())

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
        await query.message.reply_text("Enter the new channel link (e.g., https://t.me/your_channel or @your_channel):")
    return WAIT_CHANNEL

async def receive_channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    if not (new_link.startswith("http://") or new_link.startswith("https://") or new_link.startswith("t.me/") or new_link.startswith("@")):
        await update.message.reply_text("❌ Invalid link! Please enter a valid URL or Telegram username.\nType /cancel to abort.")
        return WAIT_CHANNEL

    set_setting("channel", new_link)
    await update.message.reply_text(f"✅ Channel link updated successfully!\nCurrent link: {escape_md(new_link)}", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

async def set_support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        if query.from_user.id != ADMIN_ID:
            return ConversationHandler.END
        await query.message.reply_text("Enter the new support username/link (e.g., @your_support or https://t.me/your_support):")
    return WAIT_SUPPORT

async def receive_support_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    if not (new_link.startswith("http://") or new_link.startswith("https://") or new_link.startswith("t.me/") or new_link.startswith("@")):
        await update.message.reply_text("❌ Invalid username/link! Please enter a valid URL or Telegram username.\nType /cancel to abort.")
        return WAIT_SUPPORT

    set_setting("support", new_link)
    await update.message.reply_text(f"✅ Support username/link updated successfully!\nCurrent support: {escape_md(new_link)}", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

async def set_otplink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        if query.from_user.id != ADMIN_ID:
            return ConversationHandler.END
        await query.message.reply_text("Enter the new OTP Group link (e.g., https://t.me/your_otp_group):")
    return WAIT_OTP_LINK

async def receive_otp_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    if not (new_link.startswith("http://") or new_link.startswith("https://") or new_link.startswith("t.me/")):
        await update.message.reply_text("❌ Invalid link! Please enter a valid group link.\nType /cancel to abort.")
        return WAIT_OTP_LINK

    set_setting("otp_group_link", new_link)
    await update.message.reply_text(f"✅ OTP Group link updated successfully!\nCurrent link: {escape_md(new_link)}", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

# Broadcast Handlers
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END

    all_users = get_all_users()
    await update.message.reply_text(
        f"📢 **BROADCAST SYSTEM**\n\n"
        f"Target Audience: `{len(all_users)}` users\n\n"
        f"Please send or forward the message (text, photo, video, document, etc.) you want to broadcast to all users.\n"
        f"Type /cancel to abort.",
        parse_mode="Markdown"
    )
    return WAIT_BROADCAST_MSG

async def receive_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END

    all_users = get_all_users()
    if not all_users:
        await update.message.reply_text("No users found in database to broadcast.", reply_markup=get_admin_keyboard())
        return ConversationHandler.END

    status_msg = await update.message.reply_text(f"⏳ Broadcasting message to `{len(all_users)}` users...", parse_mode="Markdown")

    success_count = 0
    failed_count = 0

    for target_id in all_users:
        try:
            await context.bot.copy_message(
                chat_id=target_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"Failed to send broadcast to {target_id}: {e}")
            failed_count += 1

    report = (
        f"📢 **BROADCAST COMPLETED**\n\n"
        f"✅ **Successfully Sent:** `{success_count}`\n"
        f"❌ **Failed / Blocked:** `{failed_count}`\n"
        f"📊 **Total Target Users:** `{len(all_users)}`"
    )
    await status_msg.edit_text(report, parse_mode="Markdown")
    await update.message.reply_text("Select an option from Admin Menu:", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)

    if update.message and update.message.text:
        await handle_text_menu(update, context)

    return ConversationHandler.END


# ---------------- INLINE CALLBACK HANDLER ----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    save_user(user_id)

    if data == "back_to_services":
        await query.answer()
        kbd, msg = get_services_keyboard()
        if not kbd:
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text(msg, reply_markup=kbd)
        return

    # Admin Settings Quantity Handlers
    elif data.startswith("adm:setqty:"):
        await query.answer()
        if user_id != ADMIN_ID:
            return
        qty_val = data.split(":", 2)[2]
        set_setting("number_quantity", qty_val)
        await query.answer(f"Number quantity set to {qty_val}!", show_alert=True)
        text_msg, kbd = build_number_quantity_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

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
        await query.answer(f"Service {service} deleted successfully!", show_alert=True)
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
        buttons.append([create_button("Back", callback_data=f"adm:srv:view:{service}", style="danger")])
        await query.edit_message_text(f"Select country to delete from **{escape_md(service)}**:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("adm:cnt:del:"):
        if user_id != ADMIN_ID:
            await query.answer()
            return
        parts = data.split(":", 4)
        if len(parts) >= 5:
            service, country = parts[3], parts[4]
            delete_country_db(service, country)
            await query.answer(f"Deleted {country} from {service}!", show_alert=True)
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
            await query.edit_message_text("No countries available for this service.")
            return

        buttons = []
        for i in range(0, len(countries), 2):
            row = []
            row.append(create_button(countries[i], callback_data=f"cnt_{service}_{countries[i]}", style="primary"))
            if i + 1 < len(countries):
                row.append(create_button(countries[i+1], callback_data=f"cnt_{service}_{countries[i+1]}", style="primary"))
            buttons.append(row)

        buttons.append([create_button("Back", callback_data="back_to_services", style="danger")])
        await query.edit_message_text(f"Select country for {escape_md(service)}:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("cnt_"):
        await query.answer()
        parts = data.split("_", 2)
        if len(parts) < 3:
            await query.edit_message_text("Invalid command.")
            return
        
        service, country = parts[1], parts[2]
        target_qty = int(get_setting("number_quantity", "2"))
        assigned_numbers = []

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            numbers_ref = db.reference(f"numbers/{service}/{country}").get()
            if numbers_ref and isinstance(numbers_ref, dict):
                for key, val in numbers_ref.items():
                    if len(assigned_numbers) >= target_qty:
                        break
                    if isinstance(val, dict) and val.get("status") == "available":
                        num_val = str(val.get("number"))
                        assigned_numbers.append(num_val)
                        db.reference(f"numbers/{service}/{country}/{key}").update({"status": "allocated", "user_id": user_id})
                        db.reference(f"allocations/{num_val}").set({"user_id": user_id, "service": service, "country": country})
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id, number FROM numbers WHERE service = ? AND country = ? AND status = 'available' LIMIT ?",
                (service, country, target_qty)
            )
            rows = cursor.fetchall()
            if rows and len(rows) == target_qty:
                for num_id, assigned_num in rows:
                    num_str = str(assigned_num)
                    assigned_numbers.append(num_str)
                    cursor.execute("UPDATE numbers SET status = 'allocated', user_id = ? WHERE id = ?", (user_id, num_id))
                    cursor.execute("INSERT OR REPLACE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)", (num_str, user_id, service, country))
                conn.commit()
            else:
                conn.rollback()
            conn.close()

        if len(assigned_numbers) < target_qty:
            if CURRENT_DB_MODE == "Firebase (Cloud)":
                for num_val in assigned_numbers:
                    db.reference(f"numbers/{service}/{country}/{num_val}").update({"status": "available", "user_id": 0})
                    db.reference(f"allocations/{num_val}").delete()
            await query.edit_message_text(f"Sorry, not enough ({target_qty}) numbers available in this category.")
            return

        nums_formatted = "\n".join([f"📱 `{n}`" for n in assigned_numbers])
        alloc_msg = (
            "━━━━━━━━━━━━━━━\n"
            "Numbers Allocated \n"
            "— — — — — — — — — —\n"
            f"📘 {escape_md(service)} ➜ {escape_md(country)}\n"
            f"{nums_formatted}\n"
            "━━━━━━━━━━━━━━━"
        )
        kbd = build_allocation_keyboard(service, country, assigned_numbers)
        await query.edit_message_text(alloc_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("chg:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            await query.answer("Invalid request!", show_alert=True)
            return

        service, country = parts[1], parts[2]
        target_qty = int(get_setting("number_quantity", "2"))
        old_numbers = []

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            alloc_ref = db.reference("allocations").get()
            if alloc_ref and isinstance(alloc_ref, dict):
                for num_k, num_v in alloc_ref.items():
                    if isinstance(num_v, dict) and num_v.get("user_id") == user_id and num_v.get("service") == service and num_v.get("country") == country:
                        old_numbers.append(str(num_k))
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT number FROM allocations WHERE user_id = ? AND service = ? AND country = ?", (user_id, service, country))
            old_numbers = [str(r[0]) for r in cursor.fetchall()]
            conn.close()

        new_numbers = []

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            for old_num in old_numbers:
                db.reference(f"numbers/{service}/{country}/{old_num}").update({"status": "available", "user_id": 0})
                db.reference(f"allocations/{old_num}").delete()

            numbers_ref = db.reference(f"numbers/{service}/{country}").get()
            if numbers_ref and isinstance(numbers_ref, dict):
                for key, val in numbers_ref.items():
                    if len(new_numbers) >= target_qty:
                        break
                    if isinstance(val, dict) and val.get("status") == "available" and str(val.get("number")) not in old_numbers:
                        new_num = str(val.get("number"))
                        new_numbers.append(new_num)
                        db.reference(f"numbers/{service}/{country}/{key}").update({"status": "allocated", "user_id": user_id})
                        db.reference(f"allocations/{new_num}").set({"user_id": user_id, "service": service, "country": country})

            if len(new_numbers) < target_qty:
                for n in new_numbers:
                    db.reference(f"numbers/{service}/{country}/{n}").update({"status": "available", "user_id": 0})
                    db.reference(f"allocations/{n}").delete()

                for old_num in old_numbers:
                    db.reference(f"numbers/{service}/{country}/{old_num}").update({"status": "allocated", "user_id": user_id})
                    db.reference(f"allocations/{old_num}").set({"user_id": user_id, "service": service, "country": country})
                new_numbers = []
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            
            for old_num in old_numbers:
                cursor.execute("UPDATE numbers SET status = 'available', user_id = 0 WHERE number = ?", (old_num,))
                cursor.execute("DELETE FROM allocations WHERE number = ?", (old_num,))

            if old_numbers:
                placeholders = ','.join(['?'] * len(old_numbers))
                query_sql = f"SELECT id, number FROM numbers WHERE service = ? AND country = ? AND status = 'available' AND number NOT IN ({placeholders}) LIMIT ?"
                params = [service, country] + old_numbers + [target_qty]
            else:
                query_sql = "SELECT id, number FROM numbers WHERE service = ? AND country = ? AND status = 'available' LIMIT ?"
                params = [service, country, target_qty]

            cursor.execute(query_sql, params)
            rows = cursor.fetchall()

            if rows and len(rows) == target_qty:
                for num_id, new_num in rows:
                    new_num_str = str(new_num)
                    new_numbers.append(new_num_str)
                    cursor.execute("UPDATE numbers SET status = 'allocated', user_id = ? WHERE id = ?", (user_id, num_id))
                    cursor.execute("INSERT OR REPLACE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)", (new_num_str, user_id, service, country))
                conn.commit()
            else:
                conn.rollback()
                for old_num in old_numbers:
                    cursor.execute("UPDATE numbers SET status = 'allocated', user_id = ? WHERE number = ?", (old_num, user_id))
                    cursor.execute("INSERT OR REPLACE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)", (old_num, user_id, service, country))
                conn.commit()
            conn.close()

        if len(new_numbers) == target_qty:
            await query.answer("Successfully changed all numbers!", show_alert=False)
            nums_formatted = "\n".join([f"📱 `{n}`" for n in new_numbers])
            alloc_msg = (
                "━━━━━━━━━━━━━━━\n"
                "Numbers Allocated \n"
                "— — — — — — — — — —\n"
                f"📘 {escape_md(service)} ➜ {escape_md(country)}\n"
                f"{nums_formatted}\n"
                "━━━━━━━━━━━━━━━"
            )
            kbd = build_allocation_keyboard(service, country, new_numbers)
            await query.edit_message_text(alloc_msg, reply_markup=kbd, parse_mode="Markdown")
        else:
            await query.answer(f"Sorry, not enough ({target_qty}) new numbers available to change!", show_alert=True)


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
                                            text=f"📩 **New OTP Received**\n📱 **Number:** `{num}`\n💬 **Message:**\n`{escape_md(msg)}`",
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
                                            text=f"🎉 **Your OTP Code Has Arrived!**\n📱 **Number:** `{num}`\n💬 **Message:**\n`{escape_md(msg)}`",
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
            MessageHandler(filters.Regex("^Broadcast$") & filters.User(user_id=ADMIN_ID), broadcast_start),
        ],
        states={
            ADD_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_service_name)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_country_name)],
            ADD_NUMBERS: [MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND & ~MENU_FILTER, receive_numbers)],
            WAIT_FIREBASE_FILE: [MessageHandler(filters.Document.ALL & ~MENU_FILTER, receive_firebase_file)],
            WAIT_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_channel_link)],
            WAIT_SUPPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_support_link)],
            WAIT_OTP_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_otp_link)],
            WAIT_BROADCAST_MSG: [MessageHandler(~filters.COMMAND & ~MENU_FILTER, receive_broadcast_msg)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(MENU_FILTER, cancel)
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
