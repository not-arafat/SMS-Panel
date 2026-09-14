import asyncio
import logging
import os
import re
import json
import base64
import sqlite3
import threading
import hashlib
import html
import time
from functools import wraps

import httpx
from dotenv import load_dotenv

try:
    import firebase_admin
    from firebase_admin import credentials, db
    HAS_FIREBASE_LIB = True
except ImportError:
    HAS_FIREBASE_LIB = False

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, LinkPreviewOptions
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

API_URL = os.environ.get("API_URL", "")
API_TOKEN = os.environ.get("API_TOKEN", "")

FIREBASE_JSON_PATH = "temp_firebase.json"
CURRENT_DB_MODE = "SQLite (Local)"

OTP_ID_RETENTION_SECONDS = 24 * 60 * 60  # 24 hours
CLEANUP_EVERY_N_CYCLES = 720  # ~1 hour at 5s interval

# ---------------- IN-MEMORY GLOBAL CACHE ----------------
SETTINGS_CACHE = {}
SERVICES_CACHE = {}  # {service_name: {country_name: available_count}}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

if not API_URL:
    logging.warning("API_URL is not set — OTP polling will stay idle until it's configured in the environment.")

MENU_FILTER = filters.Regex("(?i)^(Get Number|Profile|Wallet|Channel|Support|Admin Panel|Services|Admin Control|Global Settings|Edit Links|Edit API|Number Quantity|Connect Firebase|Broadcast|Extra|Back)$")


# ---------------- PURE HELPERS ----------------
def escape_md(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def clean_tg_link(val: str) -> str:
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


def extract_otp(text: str) -> str:
    if not text:
        return "N/A"

    match = re.search(r'\b\d{4,8}\b', text)
    if match:
        return match.group(0)

    cleaned = re.sub(r'[\s/\-]', '', text)
    match = re.search(r'\d{4,8}', cleaned)
    if match:
        return match.group(0)

    return "N/A"


def create_button(text: str, callback_data: str = None, url: str = None, copy_text: str = None, style: str = None) -> dict:
    btn = {"text": str(text).upper() if text else ""}
    if callback_data:
        btn["callback_data"] = callback_data
    if url:
        btn["url"] = url
    if copy_text:
        btn["copy_text"] = {"text": copy_text}
    if style:
        btn["style"] = style
    return btn


def mask_number_aph(num_str: str) -> str:
    num_str = str(num_str).strip()
    if len(num_str) >= 6:
        return num_str[:-6] + "APH" + num_str[-3:]
    return num_str


# ---------------- DATABASE (SYNC / BLOCKING) ----------------
def get_db_connection():
    conn = sqlite3.connect("bot_database.db", timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_sqlite():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0
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
            msg_id TEXT PRIMARY KEY,
            ts INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    try:
        cursor.execute("ALTER TABLE seen_otps ADD COLUMN ts INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_numbers_lookup ON numbers (service, country, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_seen_otps_ts ON seen_otps (ts)")

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channel', 'https://t.me/your_channel')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('support', '@your_support')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('otp_group_link', 'https://t.me/your_otp_group')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('number_quantity', '2')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('show_message', 'true')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('show_country_count', 'false')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('dev_username', 'developer')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('dev_link', 'https://t.me/developer')")
    conn.commit()
    conn.close()

init_sqlite()


# ---------------- CACHE MANAGEMENT ----------------
def refresh_all_caches_sync():
    global SETTINGS_CACHE, SERVICES_CACHE
    SETTINGS_CACHE.clear()
    SERVICES_CACHE.clear()

    # 1. Load Settings from SQLite
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        for k, v in cursor.fetchall():
            SETTINGS_CACHE[str(k)] = str(v)
        conn.close()
    except Exception as e:
        logging.error(f"Error loading settings into cache: {e}")

    # Overlay with Firebase settings if Firebase is active
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_settings = db.reference("settings").get()
            if fb_settings and isinstance(fb_settings, dict):
                for k, v in fb_settings.items():
                    if v is not None:
                        SETTINGS_CACHE[str(k)] = str(v)
        except Exception as e:
            logging.error(f"Error merging Firebase settings to cache: {e}")

    # 2. Load Services Summary into RAM
    refresh_services_cache_sync()


def refresh_services_cache_sync():
    global SERVICES_CACHE
    SERVICES_CACHE.clear()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            srv_ref = db.reference("services").get()
            globally_allocated = set()
            try:
                alloc_data = db.reference("allocations").get() or {}
                if isinstance(alloc_data, dict):
                    globally_allocated = {str(k) for k in alloc_data.keys()}
            except Exception as e:
                logging.error(f"Error loading global allocation locks for cache: {e}")

            if srv_ref and isinstance(srv_ref, dict):
                for srv in srv_ref.keys():
                    SERVICES_CACHE[srv] = {}
                    cnt_ref = db.reference(f"services/{srv}").get()
                    if cnt_ref and isinstance(cnt_ref, dict):
                        for cnt in cnt_ref.keys():
                            num_ref = db.reference(f"numbers/{srv}/{cnt}").get()
                            avail_count = 0
                            if num_ref and isinstance(num_ref, dict):
                                for n_key, n_val in num_ref.items():
                                    if not isinstance(n_val, dict):
                                        continue
                                    num_val = str(n_val.get("number", n_key))
                                    if n_val.get("status") == "available" and num_val not in globally_allocated:
                                        avail_count += 1
                            SERVICES_CACHE[srv][cnt] = avail_count
            return
        except Exception as e:
            logging.error(f"Error populating services cache from Firebase: {e}")

    # Fallback / Local SQLite Cache Loading
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_name, country_name FROM services")
        pairs = cursor.fetchall()
        for srv, cnt in pairs:
            if srv not in SERVICES_CACHE:
                SERVICES_CACHE[srv] = {}
            cursor.execute("""
                SELECT COUNT(*) FROM numbers n
                WHERE n.service = ? AND n.country = ? AND n.status = 'available'
                  AND NOT EXISTS (SELECT 1 FROM allocations a WHERE a.number = n.number)
            """, (srv, cnt))
            cnt_val = cursor.fetchone()[0]
            SERVICES_CACHE[srv][cnt] = cnt_val
        conn.close()
    except Exception as e:
        logging.error(f"Error populating services cache from SQLite: {e}")


# ---------------- SETTINGS & CONFIG READ/WRITE (CACHE-FIRST) ----------------
def get_setting(key: str, default_val: str = "") -> str:
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]
    return default_val


def set_setting(key: str, value: str):
    str_val = str(value)
    SETTINGS_CACHE[key] = str_val  # Direct RAM update

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"settings/{key}").set(str_val)
        except Exception as e:
            logging.error(f"Error writing setting to Firebase: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str_val))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error writing setting to SQLite: {e}")


# ---------------- ASYNC <-> BLOCKING BRIDGE ----------------
async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------------- SENSITIVE REAL-TIME DATA (BALANCE / ALLOCATION) ----------------
def save_user(user_id: int):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            user_ref = db.reference(f"users/{user_id}")
            if not user_ref.get():
                user_ref.set({"exists": True, "balance": 0.0})
        except Exception as e:
            logging.error(f"Error saving user to Firebase: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error saving user to SQLite: {e}")


def get_user_balance_sync(user_id: int) -> float:
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            bal = db.reference(f"users/{user_id}/balance").get()
            if bal is not None:
                return float(bal)
        except Exception as e:
            logging.error(f"Firebase get balance error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        logging.error(f"SQLite get balance error: {e}")
    return 0.0


def add_user_balance_sync(user_id: int, amount: float = 1.0) -> float:
    curr_bal = get_user_balance_sync(user_id)
    new_bal = curr_bal + amount

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"users/{user_id}/balance").set(new_bal)
            db.reference(f"users/{user_id}/exists").set(True)
        except Exception as e:
            logging.error(f"Firebase update balance error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (user_id, new_bal))
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite update balance error: {e}")

    return new_bal


def get_all_users() -> list:
    users = []
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_users = db.reference("users").get()
            if fb_users and isinstance(fb_users, dict):
                users = [int(uid) for uid in fb_users.keys() if str(uid).isdigit()]
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


def sync_firebase_to_sqlite():
    if not HAS_FIREBASE_LIB or not firebase_admin._apps:
        return
    try:
        fb_settings = db.reference("settings").get()
        if fb_settings and isinstance(fb_settings, dict):
            conn = get_db_connection()
            cursor = conn.cursor()
            for k, v in fb_settings.items():
                if v is not None:
                    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (str(k), str(v)))
            conn.commit()
            conn.close()

        fb_users = db.reference("users").get()
        if fb_users and isinstance(fb_users, dict):
            conn = get_db_connection()
            cursor = conn.cursor()
            for uid, udata in fb_users.items():
                if str(uid).isdigit():
                    bal = 0.0
                    if isinstance(udata, dict):
                        bal = float(udata.get("balance", 0.0))
                    elif isinstance(udata, (int, float)):
                        bal = float(udata)

                    cursor.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (int(uid), bal))
                    cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (bal, int(uid)))
            conn.commit()
            conn.close()

        refresh_all_caches_sync()
    except Exception as e:
        logging.error(f"Error syncing Firebase data to SQLite: {e}")


def get_admin_services_summary():
    return SERVICES_CACHE


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
    refresh_services_cache_sync()


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
    refresh_services_cache_sync()


def get_countries_for_service(service: str) -> list:
    if service in SERVICES_CACHE:
        return list(SERVICES_CACHE[service].keys())
    return []


def save_numbers_sync(service: str, country: str, numbers: list) -> int:
    """Add numbers without creating duplicate inventory entries.

    A number is globally considered used once it has ever been allocated.
    Uploading it again only refreshes/creates an available inventory entry if
    it has never been allocated before.
    """
    cleaned_numbers = []
    seen = set()
    for num in numbers:
        clean_num = re.sub(r'\D', '', str(num))
        if clean_num and clean_num not in seen:
            seen.add(clean_num)
            cleaned_numbers.append(clean_num)

    if not cleaned_numbers:
        return 0

    # Build a global set of numbers that have already been allocated.
    globally_used = set()
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            used = db.reference("allocations").get() or {}
            if isinstance(used, dict):
                globally_used.update(str(k) for k in used.keys())
        except Exception as e:
            logging.error(f"Error reading Firebase allocations before upload: {e}")

        ref = db.reference(f"numbers/{service}/{country}")
        existing = ref.get() or {}
        batch = {}
        for num in cleaned_numbers:
            if num in globally_used:
                # Never make an already-allocated number available again.
                continue
            if isinstance(existing, dict) and num in existing:
                # Keep existing state; importantly, do not reset an allocated number.
                continue
            batch[num] = {"number": num, "status": "available", "user_id": 0}

        if batch:
            ref.update(batch)
        db.reference(f"services/{service}/{country}").set(True)

        # Return the number of newly inserted inventory records.
        inserted = len(batch)
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO services (service_name, country_name) VALUES (?, ?)", (service, country))

        # Existing allocations are permanent and globally block re-use.
        cursor.execute("SELECT number FROM allocations")
        globally_used = {str(row[0]) for row in cursor.fetchall()}

        inserted = 0
        for num in cleaned_numbers:
            if num in globally_used:
                continue
            cursor.execute(
                "SELECT id, status FROM numbers WHERE service = ? AND country = ? AND number = ? LIMIT 1",
                (service, country, num)
            )
            row = cursor.fetchone()
            if row:
                # Never reset an existing allocated row to available.
                continue
            cursor.execute(
                "INSERT INTO numbers (service, country, number, status, user_id) VALUES (?, ?, ?, 'available', 0)",
                (service, country, num)
            )
            inserted += 1

        conn.commit()
        conn.close()

    refresh_services_cache_sync()
    return inserted


def allocate_numbers_sync(service: str, country: str, user_id: int, target_qty: int, exclude: list = None) -> list:
    """Atomically reserve NEW numbers for a user.

    Critical rule: once a number has been allocated, it is NEVER released back
    to the available pool and can NEVER be allocated to anyone again, including
    the same user.
    """
    exclude = {str(x) for x in (exclude or [])}
    target_qty = max(1, int(target_qty))

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        # First read only the requested category.  Global allocation locks are
        # then created transactionally, so the same number cannot be won by two
        # concurrent requests.
        ref = db.reference(f"numbers/{service}/{country}")
        current_data = ref.get() or {}
        if not isinstance(current_data, dict):
            return []

        candidates = []
        for key, val in current_data.items():
            if not isinstance(val, dict):
                continue
            num = str(val.get("number", key)).strip()
            if not num or num in exclude or val.get("status") != "available":
                continue
            candidates.append(num)
            if len(candidates) >= target_qty * 3:
                break

        if len(candidates) < target_qty:
            return []

        assigned = []
        # allocations/{number} is the permanent global ownership record.
        for num in candidates:
            if len(assigned) >= target_qty:
                break
            lock_ref = db.reference(f"allocations/{num}")
            result_holder = {"won": False}

            def lock_txn(current):
                if current is not None:
                    return current
                result_holder["won"] = True
                return {"user_id": user_id, "service": service, "country": country, "allocated_at": int(time.time())}

            try:
                lock_ref.transaction(lock_txn)
            except Exception as e:
                logging.error(f"Firebase global allocation lock failed for {num}: {e}")
                continue

            if not result_holder["won"]:
                # This number was already permanently allocated elsewhere (or by
                # an earlier request). Keep the inventory mirror from advertising
                # it as available in this category.
                try:
                    existing_lock = lock_ref.get()
                    if isinstance(existing_lock, dict):
                        db.reference(f"numbers/{service}/{country}/{num}").update({
                            "status": "allocated",
                            "user_id": int(existing_lock.get("user_id", 0) or 0)
                        })
                except Exception as e:
                    logging.error(f"Firebase duplicate-allocation cleanup failed for {num}: {e}")
                continue

            # Permanently mark the inventory record allocated.  It is NEVER reset
            # by Change All or any other user flow.
            try:
                db.reference(f"numbers/{service}/{country}/{num}").update({
                    "status": "allocated",
                    "user_id": user_id,
                    "allocated_at": int(time.time())
                })
                assigned.append(num)
            except Exception as e:
                logging.error(f"Firebase number status update failed for {num}: {e}")
                # If the inventory write failed, remove the lock so the number
                # does not become permanently unusable without an allocation.
                try:
                    lock_ref.delete()
                except Exception:
                    pass

        refresh_services_cache_sync()
        # Never roll back successful allocations. If fewer than requested were
        # available because another request won some candidates concurrently,
        # return the numbers that were successfully and permanently allocated.
        return assigned

    conn = get_db_connection()
    cursor = conn.cursor()
    assigned = []
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Exclude every number that has ever been allocated globally.
        if exclude:
            placeholders = ','.join(['?'] * len(exclude))
            sql = f"""
                SELECT id, number FROM numbers
                WHERE service = ? AND country = ? AND status = 'available'
                  AND number NOT IN ({placeholders})
                  AND number NOT IN (SELECT number FROM allocations)
                ORDER BY id ASC LIMIT ?
            """
            params = [service, country] + list(exclude) + [target_qty]
        else:
            sql = """
                SELECT id, number FROM numbers
                WHERE service = ? AND country = ? AND status = 'available'
                  AND number NOT IN (SELECT number FROM allocations)
                ORDER BY id ASC LIMIT ?
            """
            params = [service, country, target_qty]

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        if len(rows) < target_qty:
            conn.rollback()
            return []

        now = int(time.time())
        for num_id, num in rows:
            num_str = str(num)
            assigned.append(num_str)
            cursor.execute(
                "UPDATE numbers SET status = 'allocated', user_id = ? WHERE id = ? AND status = 'available'",
                (user_id, num_id)
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Number {num_str} could not be locked")
            cursor.execute(
                "INSERT OR IGNORE INTO allocations (number, user_id, service, country) VALUES (?, ?, ?, ?)",
                (num_str, user_id, service, country)
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Number {num_str} was already allocated")

        # Preserve the user's permanent allocations; no release happens later.
        conn.commit()
        refresh_services_cache_sync()
        return assigned
    except Exception as e:
        conn.rollback()
        logging.error(f"SQLite allocation failed: {e}")
        return []
    finally:
        conn.close()


def release_numbers_sync(service: str, country: str, numbers: list):
    """Legacy compatibility function. Allocations are intentionally permanent.

    Older versions released numbers when the user pressed Change All. That made
    already-used numbers available again. This function now deliberately does
    nothing so an allocated number can never re-enter the pool.
    """
    logging.info(
        "release_numbers_sync ignored: allocations are permanent. service=%s country=%s count=%s",
        service, country, len(numbers or [])
    )
    return


def get_user_allocations_sync(user_id: int, service: str, country: str) -> list:
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        alloc_ref = db.reference("allocations").get()
        result = []
        if alloc_ref and isinstance(alloc_ref, dict):
            for num_k, num_v in alloc_ref.items():
                if isinstance(num_v, dict) and num_v.get("user_id") == user_id and num_v.get("service") == service and num_v.get("country") == country:
                    result.append(str(num_k))
        return result

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT number FROM allocations WHERE user_id = ? AND service = ? AND country = ?", (user_id, service, country))
    result = [str(r[0]) for r in cursor.fetchall()]
    conn.close()
    return result


# ---------------- VIEW BUILDERS ----------------
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


def build_extra_settings_view():
    show_msg = get_setting("show_message", "true") == "true"
    show_country_count = get_setting("show_country_count", "false") == "true"
    msg_status = "ENABLED 🟢" if show_msg else "DISABLED 🔴"
    count_status = "ENABLED 🟢" if show_country_count else "DISABLED 🔴"

    text = (
        "⚙️ **EXTRA SETTINGS**\n\n"
        f"📩 **Show OTP Message:** `{msg_status}`\n"
        f"🔢 **Show Country Number Count:** `{count_status}`\n\n"
        "Use the buttons below to enable/disable each option."
    )
    buttons = [
        [create_button(f"Show Message: {msg_status}", callback_data="adm:toggle:show_msg", style="success" if show_msg else "danger")],
        [create_button(f"Country Count: {count_status}", callback_data="adm:toggle:country_count", style="success" if show_country_count else "danger")]
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
    services = list(SERVICES_CACHE.keys())

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


# ---------------- FIREBASE CONNECTION MANAGEMENT ----------------
def init_firebase_system(run_migration=False, force_reinit=False):
    global CURRENT_DB_MODE
    if not HAS_FIREBASE_LIB:
        CURRENT_DB_MODE = "SQLite (Local)"
        refresh_all_caches_sync()
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
    refresh_all_caches_sync()
    return False


def migrate_sqlite_to_firebase():
    if not firebase_admin._apps:
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id, balance FROM users")
    users = cursor.fetchall()
    for u in users:
        db.reference(f"users/{u[0]}").set({"exists": True, "balance": u[1] if len(u) > 1 else 0.0})

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
    refresh_all_caches_sync()


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


# ---------------- AUTH DECORATOR ----------------
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        query = update.callback_query
        user = update.effective_user
        if query:
            await query.answer()
        if not user or user.id != ADMIN_ID:
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


# ---------------- KEYBOARDS ----------------
def get_main_keyboard(user_id: int):
    keyboard_layout = [
        [
            {"text": "GET NUMBER", "style": "success"}
        ],
        [
            {"text": "PROFILE", "style": "primary"},
            {"text": "WALLET", "style": "primary"}
        ],
        [
            {"text": "CHANNEL", "style": "danger"},
            {"text": "SUPPORT", "style": "danger"}
        ]
    ]
    if user_id == ADMIN_ID:
        keyboard_layout.append([{"text": "ADMIN PANEL", "style": "danger"}])

    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


def get_admin_keyboard():
    keyboard_layout = [
        [
            {"text": "SERVICES", "style": "success"},
            {"text": "BROADCAST", "style": "success"}
        ],
        [
            {"text": "ADMIN CONTROL", "style": "primary"},
            {"text": "GLOBAL SETTINGS", "style": "primary"}
        ],
        [
            {"text": "BACK", "style": "danger"}
        ]
    ]
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


def get_global_settings_keyboard():
    keyboard_layout = [
        [
            {"text": "EDIT LINKS", "style": "success"},
            {"text": "EDIT API", "style": "success"}
        ],
        [
            {"text": "NUMBER QUANTITY", "style": "primary"},
            {"text": "CONNECT FIREBASE", "style": "primary"}
        ],
        [
            {"text": "EXTRA", "style": "primary"},
            {"text": "BACK", "style": "danger"}
        ]
    ]
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


# ---------------- BOT HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await run_db(save_user, user_id)

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'main'
    first_name = escape_md(update.effective_user.first_name or "User")
    msg = f"Welcome, {first_name}!\nPlease select an option from the menu:"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")


async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    await run_db(save_user, user_id)

    text_upper = text.strip().upper()

    if text_upper == "GET NUMBER":
        kbd, msg = get_services_keyboard()
        if not kbd:
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=kbd)

    elif text_upper == "PROFILE":
        first_name = escape_md(update.effective_user.first_name or "User")
        bot_username = context.bot.username or "bot"
        refer_link = f"https://t.me/{bot_username}?start={user_id}"
        bal = await run_db(get_user_balance_sync, user_id)
        bal_str = f"{int(bal)}" if bal.is_integer() else f"{bal:.2f}"

        profile_text = (
            f"👤 **USER PROFILE**\n\n"
            f"📝 **Name:** {first_name}\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"💰 **Balance:** `{bal_str} ৳`"
        )
        kbd = InlineKeyboardMarkup([
            [create_button("Referral Link", copy_text=refer_link, style="success")]
        ])
        await update.message.reply_text(profile_text, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "WALLET":
        bal = await run_db(get_user_balance_sync, user_id)
        bal_str = f"{int(bal)}" if bal.is_integer() else f"{bal:.2f}"
        wallet_text = (
            f"👛 **YOUR WALLET**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Balance:** `{bal_str} ৳`"
        )
        await update.message.reply_text(wallet_text, parse_mode="Markdown")

    elif text_upper == "CHANNEL":
        ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))
        kbd = InlineKeyboardMarkup([
            [create_button("Join Channel", url=ch_link, style="primary")]
        ])
        await update.message.reply_text("Click below to join our official channel:", reply_markup=kbd)

    elif text_upper == "SUPPORT":
        sp_link = clean_tg_link(get_setting("support", "@your_support"))
        ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))

        kbd = InlineKeyboardMarkup([
            [
                create_button("Support", url=sp_link, style="primary"),
                create_button("Channel", url=ch_link, style="primary")
            ]
        ])
        await update.message.reply_text("Click below to contact support or join our channel:", reply_markup=kbd)

    elif text_upper == "ADMIN PANEL" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        total_users = len(await run_db(get_all_users))
        await update.message.reply_text(
            f"**ADMIN PANEL**\n\n"
            f"⚙️ DB Mode: **{CURRENT_DB_MODE}**\n"
            f"👥 Total Registered Users: `{total_users}`",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )

    elif text_upper == "SERVICES" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_services_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "GLOBAL SETTINGS" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text(
            "⚙️ **GLOBAL SETTINGS MENU**\n\nSelect an option from below keyboard:",
            reply_markup=get_global_settings_keyboard(),
            parse_mode="Markdown"
        )

    elif text_upper == "EDIT LINKS" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_edit_links_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "EDIT API" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text("⚠️ **Edit API feature is currently unavailable.**", parse_mode="Markdown")

    elif text_upper == "NUMBER QUANTITY" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_number_quantity_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "EXTRA" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_extra_settings_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "ADMIN CONTROL" and user_id == ADMIN_ID:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text("🛠 **ADMIN CONTROL**\n\nSystem control settings panel.", parse_mode="Markdown")

    elif text_upper == "BACK":
        curr_menu = context.user_data.get('current_menu', 'main')
        if curr_menu == 'global_settings' and user_id == ADMIN_ID:
            context.user_data['current_menu'] = 'admin'
            total_users = len(await run_db(get_all_users))
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
@admin_only
async def admin_add_service_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("Enter the service name (e.g., TikTok, Facebook):")
    return ADD_SERVICE

@admin_only
async def admin_add_service_with_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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
        valid_count = await run_db(save_numbers_sync, service, country, numbers)
        await update.message.reply_text(f"Successfully added {valid_count} numbers!", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("Incomplete data provided. Please try again.", reply_markup=get_admin_keyboard())

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'admin'
    return ConversationHandler.END

@admin_only
async def admin_upload_firebase_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
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

    success = await run_db(init_firebase_system, run_migration=True, force_reinit=True)
    if success:
        await update.message.reply_text("Firebase file received! Database successfully connected and migrated to Firebase. 🚀", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("File saved, but failed to connect to Firebase. Please check the JSON content.", reply_markup=get_admin_keyboard())

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'admin'
    return ConversationHandler.END

@admin_only
async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
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

@admin_only
async def set_support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
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

@admin_only
async def set_otplink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
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

@admin_only
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = await run_db(get_all_users)
    await update.message.reply_text(
        f"📢 **BROADCAST SYSTEM**\n\n"
        f"Target Audience: `{len(all_users)}` users\n\n"
        f"Please send or forward the message (text, photo, video, document, etc.) you want to broadcast to all users.\n"
        f"Type /cancel to abort.",
        parse_mode="Markdown"
    )
    return WAIT_BROADCAST_MSG

@admin_only
async def receive_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = await run_db(get_all_users)
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
    await run_db(save_user, user_id)

    if data == "back_to_services":
        await query.answer()
        kbd, msg = get_services_keyboard()
        if not kbd:
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text(msg, reply_markup=kbd)

    elif data.startswith("adm:setqty:"):
        await query.answer()
        if user_id != ADMIN_ID:
            return
        qty_val = data.split(":", 2)[2]
        set_setting("number_quantity", qty_val)
        await query.answer(f"Number quantity set to {qty_val}!", show_alert=True)
        text_msg, kbd = build_number_quantity_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:show_msg":
        await query.answer()
        if user_id != ADMIN_ID:
            return
        curr_val = get_setting("show_message", "true")
        new_val = "false" if curr_val == "true" else "true"
        set_setting("show_message", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Show Message option is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:country_count":
        await query.answer()
        if user_id != ADMIN_ID:
            return
        curr_val = get_setting("show_country_count", "false")
        new_val = "false" if curr_val == "true" else "true"
        set_setting("show_country_count", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Country number count is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:srv:list":
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
        await run_db(delete_service_db, service)
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
            await run_db(delete_country_db, service, country)
            await query.answer(f"Deleted {country} from {service}!", show_alert=True)
            text, kbd = build_service_manage_view(service)
            await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")
        else:
            await query.answer()

    # User Get Number Flow
    elif data.startswith("srv_"):
        await query.answer()
        service = data.split("_", 1)[1]
        countries = get_countries_for_service(service)

        if not countries:
            await query.edit_message_text("No countries available for this service.")
            return

        buttons = []
        show_country_count = get_setting("show_country_count", "false") == "true"
        for i in range(0, len(countries), 2):
            country_a = countries[i]
            label_a = f"{country_a} ({SERVICES_CACHE.get(service, {}).get(country_a, 0)})" if show_country_count else country_a
            row = [create_button(label_a, callback_data=f"cnt_{service}_{country_a}", style="primary")]
            if i + 1 < len(countries):
                country_b = countries[i + 1]
                label_b = f"{country_b} ({SERVICES_CACHE.get(service, {}).get(country_b, 0)})" if show_country_count else country_b
                row.append(create_button(label_b, callback_data=f"cnt_{service}_{country_b}", style="primary"))
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

        assigned_numbers = await run_db(allocate_numbers_sync, service, country, user_id, target_qty)

        if not assigned_numbers:
            await query.edit_message_text(f"Sorry, not enough ({target_qty}) numbers available in this category.")
            return

        alloc_msg = (
            "━━━━━━━━━━━━━━━\n"
            f"{escape_md(service)} ➜ {escape_md(country)}'s Numbers Allocated:"
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

        old_numbers = await run_db(get_user_allocations_sync, user_id, service, country)
        new_numbers = await run_db(allocate_numbers_sync, service, country, user_id, target_qty, old_numbers)

        if new_numbers:
            # IMPORTANT: old allocations are permanent and must never return to inventory.
            # Do not release them here.
            await query.answer("Successfully changed all numbers!", show_alert=False)
            alloc_msg = (
                "━━━━━━━━━━━━━━━\n"
                f"{escape_md(service)} ➜ {escape_md(country)} {len(new_numbers)} Numbers Allocated:"
            )
            kbd = build_allocation_keyboard(service, country, new_numbers)
            await query.edit_message_text(alloc_msg, reply_markup=kbd, parse_mode="Markdown")
        else:
            await query.answer(f"Sorry, not enough ({target_qty}) new numbers available to change!", show_alert=True)


# ---------------- OTP POLLING SERVICE (CR API) ----------------
def load_seen_otp_ids_sync() -> dict:
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        seen_ref = db.reference("seen_otp_ids").get()
        if seen_ref and isinstance(seen_ref, dict):
            return {k: True for k in seen_ref.keys()}
        return {}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT msg_id FROM seen_otps")
    result = {row[0]: True for row in cursor.fetchall()}
    conn.close()
    return result


def mark_otp_seen_sync(msg_id: str):
    ts = int(time.time())
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        db.reference(f"seen_otp_ids/{msg_id}").set(ts)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO seen_otps (msg_id, ts) VALUES (?, ?)", (msg_id, ts))
    conn.commit()
    conn.close()


def cleanup_old_otp_ids_sync():
    cutoff = int(time.time()) - OTP_ID_RETENTION_SECONDS
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        seen_ref = db.reference("seen_otp_ids").get()
        if seen_ref and isinstance(seen_ref, dict):
            for msg_id, ts in seen_ref.items():
                if isinstance(ts, (int, float)) and ts < cutoff:
                    db.reference(f"seen_otp_ids/{msg_id}").delete()
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM seen_otps WHERE ts > 0 AND ts < ?", (cutoff,))
    conn.commit()
    conn.close()


def lookup_allocation_sync(num: str, clean_num: str):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        alloc_ref = db.reference(f"allocations/{num}").get() or db.reference(f"allocations/{clean_num}").get()
        if alloc_ref and isinstance(alloc_ref, dict):
            return alloc_ref.get("user_id"), alloc_ref.get("service")
        return None, None
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, service FROM allocations WHERE number = ? OR number = ?", (num, clean_num))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, None


async def otp_poller(application: Application):
    processed_ids = await run_db(load_seen_otp_ids_sync)

    bot_info = await application.bot.get_me()
    bot_username = bot_info.username or ""
    bot_link = f"https://t.me/{bot_username}" if bot_username else "https://t.me"

    cycle_count = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                if API_URL:
                    params = {}
                    if API_TOKEN and "token=" not in API_URL:
                        params["token"] = API_TOKEN
                        params["records"] = 200

                    res = await client.get(API_URL, params=params if params else None)
                    if res.status_code == 200:
                        res_data = res.json()
                        if res_data.get("status") == "success":
                            items = res_data.get("data", [])
                            if isinstance(items, list) and items:
                                ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))
                                show_msg_enabled = get_setting("show_message", "true") == "true"
                                dev_username = get_setting("dev_username", "developer")
                                dev_link = clean_tg_link(get_setting("dev_link", "https://t.me/developer"))
                                dev_html = f'<a href="{dev_link}">{html.escape(dev_username)}</a>'

                                for item in items:
                                    if not isinstance(item, dict):
                                        continue

                                    # 1. PAYOUT ZERO CHECK (Silent Skip)
                                    payout_raw = item.get("payout", "0")
                                    try:
                                        payout_val = float(payout_raw)
                                    except (ValueError, TypeError):
                                        payout_val = 0.0

                                    if payout_val <= 0.0:
                                        continue

                                    num = str(item.get("num", "")).strip()
                                    msg = item.get("message", "")
                                    dt = item.get("dt", "")
                                    cli = item.get("cli", "")

                                    if not num or not msg:
                                        continue

                                    unique_str = f"{num}_{dt}_{msg}"
                                    msg_id = hashlib.md5(unique_str.encode()).hexdigest()

                                    if msg_id in processed_ids:
                                        continue

                                    processed_ids[msg_id] = True
                                    if len(processed_ids) > 2000:
                                        for old_id in list(processed_ids.keys())[:1000]:
                                            del processed_ids[old_id]

                                    await run_db(mark_otp_seen_sync, msg_id)

                                    clean_num = re.sub(r'\D', '', num)
                                    allocated_user, service_name = await run_db(lookup_allocation_sync, num, clean_num)
                                    if not service_name:
                                        service_name = cli if cli else "Service"

                                    otp_code = extract_otp(msg)

                                    safe_msg = html.escape(msg)
                                    safe_service = html.escape(service_name)
                                    safe_num = html.escape(num)

                                    # 2. MASKED NUMBER FOR GROUP
                                    masked_num = mask_number_aph(num)
                                    safe_masked_num = html.escape(masked_num)

                                    # Send to OTP Forwarding Group (Masked Number)
                                    if OTP_GROUP_ID:
                                        if show_msg_enabled:
                                            group_text = (
                                                "━━━━━━━━━━━━━━━━━\n"
                                                f"📱 <b>SERVICE</b>:  {safe_service}\n"
                                                f"🌐 NUM: {safe_masked_num}\n\n"
                                                "🗨️ MESSAGE:\n"
                                                f"<blockquote expandable>{safe_msg}</blockquote>\n"
                                                "━━━━━━━━━━━━━━━━━\n"
                                                f"🖥️ Dᴇᴠᴇʟᴏᴘᴇʀ {dev_html}"
                                            )
                                        else:
                                            group_text = (
                                                "━━━━━━━━━━━━━━━━━\n"
                                                f"📱 <b>SERVICE</b>:  {safe_service}\n"
                                                f"🌐 NUM: {safe_masked_num}\n"
                                                "━━━━━━━━━━━━━━━━━\n"
                                                f"🖥️ Dᴇᴠᴇʟᴏᴘᴇʀ {dev_html}"
                                            )

                                        group_kbd = InlineKeyboardMarkup([
                                            [
                                                create_button("Channel", url=ch_link, style="primary"),
                                                create_button("Get Number", url=bot_link, style="primary")
                                            ],
                                            [
                                                create_button(f"{otp_code}", copy_text=otp_code, style="success")
                                            ]
                                        ])
                                        try:
                                            await application.bot.send_message(
                                                chat_id=OTP_GROUP_ID,
                                                text=group_text,
                                                reply_markup=group_kbd,
                                                parse_mode="HTML",
                                                link_preview_options=LinkPreviewOptions(is_disabled=True)
                                            )
                                        except Exception as e:
                                            logging.error(f"Group Forward Error: {e}")

                                    # Send to User Inbox & Update Balance
                                    if allocated_user:
                                        new_bal = await run_db(add_user_balance_sync, allocated_user, 1.0)
                                        bal_str = f"{int(new_bal)}" if new_bal.is_integer() else f"{new_bal:.2f}"

                                        if show_msg_enabled:
                                            user_text = (
                                                "— — — — — — — — — —\n"
                                                f"<blockquote>📱 SERVICE: {safe_service}</blockquote>\n"
                                                f"<blockquote>📞 NUMBER: {safe_num}</blockquote>\n"
                                                "<blockquote>➕ ADDED  ➜ 1 TK</blockquote>\n"
                                                f"<blockquote>💳 BALANCE ➜ {bal_str} TK</blockquote>\n"
                                                "🗨️ MESSAGE: \n"
                                                f"<blockquote expandable>{safe_msg}</blockquote>\n"
                                                "— — — — — — — — — —"
                                            )
                                        else:
                                            user_text = (
                                                "— — — — — — — — — —\n"
                                                f"<blockquote>📱 SERVICE: {safe_service}</blockquote>\n"
                                                f"<blockquote>📞 NUMBER: {safe_num}</blockquote>\n"
                                                "<blockquote>➕ ADDED  ➜ 1 TK</blockquote>\n"
                                                f"<blockquote>💳 BALANCE ➜ {bal_str} TK</blockquote>\n"
                                                "— — — — — — — — — —"
                                            )

                                        user_kbd = InlineKeyboardMarkup([
                                            [
                                                create_button(f"{otp_code}", copy_text=otp_code, style="success")
                                            ]
                                        ])
                                        try:
                                            await application.bot.send_message(
                                                chat_id=allocated_user,
                                                text=user_text,
                                                reply_markup=user_kbd,
                                                parse_mode="HTML"
                                            )
                                        except Exception as e:
                                            logging.error(f"User Forward Error: {e}")

            except Exception as e:
                logging.error(f"Polling Exception: {e}")

            cycle_count += 1
            if cycle_count % CLEANUP_EVERY_N_CYCLES == 0:
                await run_db(cleanup_old_otp_ids_sync)

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
            MessageHandler(filters.Regex("(?i)^Connect Firebase$") & filters.User(user_id=ADMIN_ID), admin_upload_firebase_start),
            MessageHandler(filters.Regex("(?i)^Broadcast$") & filters.User(user_id=ADMIN_ID), broadcast_start),
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
