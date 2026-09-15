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
import datetime
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
OTP_GROUP_ID = os.environ.get("OTP_GROUP_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL")

CURRENT_DB_MODE = "SQLite (Local)"

OTP_ID_RETENTION_SECONDS = 24 * 60 * 60  # 24 hours
CLEANUP_EVERY_N_CYCLES = 720  # ~1 hour

# ---------------- IN-MEMORY GLOBAL CACHE ----------------
SETTINGS_CACHE = {}
SERVICES_CACHE = {}  # {service_name: {country_name: available_count}}
ADMINS_CACHE = set() # {user_id, ...}
PANEL_TASKS = {}     # Dynamic background tasks for API panels

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

MENU_FILTER = filters.Regex("(?i)^(Get Number|Profile|Wallet|Ranking|Leaderboard|Support|Admin Panel|Services|Admin Control|Global Settings|Edit Links|Edit API|Number Quantity|Broadcast|Extra|Manage Payouts|Withdraw|Back)$")


# ---------------- PURE HELPERS ----------------
def get_bd_date_str() -> str:
    tz_bd = datetime.timezone(datetime.timedelta(hours=6))
    return datetime.datetime.now(tz_bd).strftime('%Y-%m-%d')


def get_current_friday_str() -> str:
    tz_bd = datetime.timezone(datetime.timedelta(hours=6))
    now_bd = datetime.datetime.now(tz_bd)
    days_since_friday = (now_bd.weekday() - 4) % 7
    last_friday = now_bd - datetime.timedelta(days=days_since_friday)
    return last_friday.strftime('%Y-%m-%d')


def fmt_num(val: float) -> str:
    if val is None:
        return "0"
    if val == int(val):
        return str(int(val))
    return f"{val:.2f}".rstrip('0').rstrip('.')


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
    if len(num_str) > 6:
        return num_str[:-6] + "APH" + num_str[-3:]
    return num_str


def mask_api_key(key: str) -> str:
    if not key:
        return "N/A"
    key_str = str(key).strip()
    length = len(key_str)
    if length > 12:
        return f"{key_str[:6]}******{key_str[-6:]}"
    elif length > 8:
        return f"{key_str[:4]}******{key_str[-4:]}"
    elif length > 4:
        return f"{key_str[:2]}******{key_str[-2:]}"
    return "******"


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
            balance REAL DEFAULT 0.0,
            today_earned REAL DEFAULT 0.0,
            total_earned REAL DEFAULT 0.0,
            refer_earned REAL DEFAULT 0.0,
            total_otps INTEGER DEFAULT 0,
            weekly_otps INTEGER DEFAULT 0,
            first_name TEXT DEFAULT '',
            last_earn_date TEXT DEFAULT ''
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            name TEXT
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_panels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            url TEXT,
            token TEXT,
            polling_interval REAL DEFAULT 5.0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdraw_methods (
            name TEXT PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            method TEXT,
            wallet_number TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            reject_reason TEXT DEFAULT '',
            created_at INTEGER
        )
    ''')

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN weekly_otps INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_numbers_lookup ON numbers (service, country, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_seen_otps_ts ON seen_otps (ts)")

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channel', 'https://t.me/your_channel')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('support', '@your_support')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('otp_group_link', 'https://t.me/your_otp_group')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('otp_group_id', ?)", (OTP_GROUP_ID,))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('number_quantity', '2')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('show_message', 'true')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('show_country_count', 'false')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('show_developer', 'true')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('withdraw_enabled', 'true')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ranking_bonus_enabled', 'true')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rank_bonus_1', '50')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rank_bonus_2', '30')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rank_bonus_3', '20')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('last_weekly_reset_friday', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('dev_username', 'developer')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('dev_link', 'https://t.me/developer')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdraw_amount', '50')")

    cursor.execute("INSERT OR IGNORE INTO withdraw_methods (name) VALUES ('Bkash')")
    cursor.execute("INSERT OR IGNORE INTO withdraw_methods (name) VALUES ('Nagad')")
    cursor.execute("INSERT OR IGNORE INTO withdraw_methods (name) VALUES ('TRC20')")

    conn.commit()
    conn.close()

init_sqlite()


# ---------------- WITHDRAW DB OPERATIONS ----------------
def get_withdraw_methods_sync() -> list:
    methods = []
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_m = db.reference("withdraw_methods").get()
            if fb_m and isinstance(fb_m, dict):
                methods = list(fb_m.keys())
        except Exception as e:
            logging.error(f"Firebase get withdraw methods error: {e}")

    if not methods:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM withdraw_methods")
            methods = [r[0] for r in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logging.error(f"SQLite get withdraw methods error: {e}")
    return methods


def add_withdraw_method_sync(name: str):
    name = name.strip()
    if not name:
        return
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"withdraw_methods/{name}").set(True)
        except Exception as e:
            logging.error(f"Firebase add withdraw method error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO withdraw_methods (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite add withdraw method error: {e}")


def delete_withdraw_method_sync(name: str):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"withdraw_methods/{name}").delete()
        except Exception as e:
            logging.error(f"Firebase delete withdraw method error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM withdraw_methods WHERE name = ?", (name,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite delete withdraw method error: {e}")


def _mirror_balance_to_sqlite(user_id: int, new_bal: float):
    """Best-effort mirror of an already-decided balance into the local SQLite cache."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
        conn.commit()
    except Exception as e:
        logging.error(f"SQLite mirror balance error: {e}")
    finally:
        if conn:
            conn.close()


def deduct_user_balance_sync(user_id: int, amount: float) -> bool:
    """Atomically checks-and-deducts balance so concurrent requests (e.g. a double withdraw
    tap, or a withdraw racing an OTP credit) can never both succeed on stale data."""
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        bal_ref = db.reference(f"users/{user_id}/balance")
        result = {"ok": False, "new_bal": None}

        def txn(current):
            cur_bal = float(current or 0.0)
            if cur_bal < amount:
                result["ok"] = False
                return current  # no-op, abort the deduction
            result["ok"] = True
            result["new_bal"] = cur_bal - amount
            return result["new_bal"]

        try:
            bal_ref.transaction(txn)
        except Exception as e:
            logging.error(f"Firebase deduct balance error: {e}")
            return False

        if result["ok"]:
            _mirror_balance_to_sqlite(user_id, result["new_bal"])
        return result["ok"]

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        curr_bal = float(row[0]) if row and row[0] is not None else 0.0
        if curr_bal < amount:
            conn.rollback()
            return False
        new_bal = curr_bal - amount
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"SQLite deduct balance error: {e}")
        return False
    finally:
        conn.close()


def refund_user_balance_sync(user_id: int, amount: float):
    """Atomically adds to balance (used for refunds / rank bonuses)."""
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        bal_ref = db.reference(f"users/{user_id}/balance")
        result = {"new_bal": None}

        def txn(current):
            new_val = float(current or 0.0) + amount
            result["new_bal"] = new_val
            return new_val

        try:
            bal_ref.transaction(txn)
        except Exception as e:
            logging.error(f"Firebase refund balance error: {e}")

        if result["new_bal"] is not None:
            _mirror_balance_to_sqlite(user_id, result["new_bal"])
        return

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        curr_bal = float(row[0]) if row and row[0] is not None else 0.0
        new_bal = curr_bal + amount
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"SQLite refund balance error: {e}")
    finally:
        conn.close()


def create_withdraw_request_sync(user_id: int, method: str, wallet_number: str, amount: float) -> int:
    ts = int(time.time())
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO withdraw_requests (user_id, method, wallet_number, amount, status, reject_reason, created_at)
        VALUES (?, ?, ?, ?, 'pending', '', ?)
    """, (user_id, method, wallet_number, amount, ts))
    req_id = cursor.lastrowid
    conn.commit()
    conn.close()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"withdraw_requests/{req_id}").set({
                "id": req_id,
                "user_id": user_id,
                "method": method,
                "wallet_number": wallet_number,
                "amount": amount,
                "status": "pending",
                "reject_reason": "",
                "created_at": ts
            })
        except Exception as e:
            logging.error(f"Firebase create withdraw req error: {e}")
    return req_id


def get_all_withdraw_requests_sync() -> list:
    requests = []
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_reqs = db.reference("withdraw_requests").get()
            if fb_reqs and isinstance(fb_reqs, dict):
                for rid, rdata in fb_reqs.items():
                    if isinstance(rdata, dict):
                        requests.append({
                            "id": int(rdata.get("id", rid)),
                            "user_id": int(rdata.get("user_id", 0)),
                            "method": str(rdata.get("method", "")),
                            "wallet_number": str(rdata.get("wallet_number", "")),
                            "amount": float(rdata.get("amount", 0.0)),
                            "status": str(rdata.get("status", "pending")),
                            "reject_reason": str(rdata.get("reject_reason", "")),
                            "created_at": int(rdata.get("created_at", 0))
                        })
        except Exception as e:
            logging.error(f"Firebase fetch withdraw reqs error: {e}")

    if not requests:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id, user_id, method, wallet_number, amount, status, reject_reason, created_at FROM withdraw_requests ORDER BY id ASC")
            for r in cursor.fetchall():
                requests.append({
                    "id": r[0],
                    "user_id": r[1],
                    "method": r[2],
                    "wallet_number": r[3],
                    "amount": r[4],
                    "status": r[5],
                    "reject_reason": r[6],
                    "created_at": r[7]
                })
            conn.close()
        except Exception as e:
            logging.error(f"SQLite fetch withdraw reqs error: {e}")
    else:
        requests.sort(key=lambda x: x["id"])
    return requests


def get_withdraw_request_by_id_sync(req_id: int) -> dict:
    reqs = get_all_withdraw_requests_sync()
    for r in reqs:
        if r["id"] == req_id:
            return r
    return None


def update_withdraw_status_sync(req_id: int, status: str, reject_reason: str = ""):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"withdraw_requests/{req_id}").update({
                "status": status,
                "reject_reason": reject_reason
            })
        except Exception as e:
            logging.error(f"Firebase update withdraw status error: {e}")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE withdraw_requests SET status = ?, reject_reason = ? WHERE id = ?", (status, reject_reason, req_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite update withdraw status error: {e}")


# ---------------- ADMIN MANAGEMENT DB OPERATIONS ----------------
def is_admin_sync(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    return user_id in ADMINS_CACHE


def get_all_admins_sync() -> list:
    admins = {}
    if ADMIN_ID:
        admins[ADMIN_ID] = {"user_id": ADMIN_ID, "name": "Main Owner", "is_owner": True}

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_admins = db.reference("admins").get()
            if fb_admins and isinstance(fb_admins, dict):
                for uid, adata in fb_admins.items():
                    if str(uid).isdigit():
                        uid_int = int(uid)
                        name = adata.get("name", "Admin") if isinstance(adata, dict) else "Admin"
                        if uid_int == ADMIN_ID:
                            admins[uid_int]["name"] = f"{name} (Owner)"
                        else:
                            admins[uid_int] = {"user_id": uid_int, "name": name, "is_owner": False}
        except Exception as e:
            logging.error(f"Firebase get admins error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name FROM admins")
        for r in cursor.fetchall():
            uid_int = int(r[0])
            name = str(r[1])
            if uid_int == ADMIN_ID:
                admins[uid_int]["name"] = f"{name} (Owner)"
            else:
                admins[uid_int] = {"user_id": uid_int, "name": name, "is_owner": False}
        conn.close()
    except Exception as e:
        logging.error(f"SQLite get admins error: {e}")

    return list(admins.values())


def add_admin_sync(user_id: int, name: str):
    ADMINS_CACHE.add(user_id)

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"admins/{user_id}").set({"name": name})
        except Exception as e:
            logging.error(f"Firebase add admin error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO admins (user_id, name) VALUES (?, ?)", (user_id, name))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite add admin error: {e}")


def delete_admin_sync(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False

    if user_id in ADMINS_CACHE:
        ADMINS_CACHE.remove(user_id)

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"admins/{user_id}").delete()
        except Exception as e:
            logging.error(f"Firebase delete admin error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite delete admin error: {e}")

    return True


# ---------------- API PANELS DB OPERATIONS ----------------
def get_all_api_panels_sync() -> list:
    panels = []
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_panels = db.reference("api_panels").get()
            if fb_panels and isinstance(fb_panels, dict):
                for pid, pdata in fb_panels.items():
                    if isinstance(pdata, dict):
                        panels.append({
                            "id": str(pdata.get("id", pid)),
                            "name": str(pdata.get("name", "")),
                            "url": str(pdata.get("url", "")),
                            "token": str(pdata.get("token", "")),
                            "polling_interval": float(pdata.get("polling_interval", 5.0))
                        })
                return panels
        except Exception as e:
            logging.error(f"Firebase get API panels error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url, token, polling_interval FROM api_panels")
        rows = cursor.fetchall()
        for r in rows:
            panels.append({
                "id": str(r[0]),
                "name": str(r[1]),
                "url": str(r[2]),
                "token": str(r[3]),
                "polling_interval": float(r[4]) if r[4] else 5.0
            })
        conn.close()
    except Exception as e:
        logging.error(f"SQLite get API panels error: {e}")
    return panels


def get_api_panel_sync(panel_id: str) -> dict:
    panels = get_all_api_panels_sync()
    for p in panels:
        if str(p["id"]) == str(panel_id):
            return p
    return None


def save_api_panel_sync(name: str, url: str, token: str, polling_interval: float = 5.0) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO api_panels (name, url, token, polling_interval) VALUES (?, ?, ?, ?)",
        (name, url, token, polling_interval)
    )
    pid = str(cursor.lastrowid)
    conn.commit()
    conn.close()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"api_panels/{pid}").set({
                "id": pid,
                "name": name,
                "url": url,
                "token": token,
                "polling_interval": polling_interval
            })
        except Exception as e:
            logging.error(f"Firebase save API panel error: {e}")

    return pid


def delete_api_panel_sync(panel_id: str):
    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"api_panels/{panel_id}").delete()
        except Exception as e:
            logging.error(f"Firebase delete API panel error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_panels WHERE id = ?", (panel_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"SQLite delete API panel error: {e}")


# ---------------- CACHE MANAGEMENT ----------------
def refresh_all_caches_sync():
    global SETTINGS_CACHE, SERVICES_CACHE, ADMINS_CACHE
    SETTINGS_CACHE.clear()
    SERVICES_CACHE.clear()
    ADMINS_CACHE.clear()

    if ADMIN_ID:
        ADMINS_CACHE.add(ADMIN_ID)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        for k, v in cursor.fetchall():
            SETTINGS_CACHE[str(k)] = str(v)

        cursor.execute("SELECT user_id FROM admins")
        for r in cursor.fetchall():
            ADMINS_CACHE.add(int(r[0]))
        conn.close()
    except Exception as e:
        logging.error(f"Error loading settings/admins into cache: {e}")

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_settings = db.reference("settings").get()
            if fb_settings and isinstance(fb_settings, dict):
                for k, v in fb_settings.items():
                    if v is not None:
                        SETTINGS_CACHE[str(k)] = str(v)

            fb_admins = db.reference("admins").get()
            if fb_admins and isinstance(fb_admins, dict):
                for uid in fb_admins.keys():
                    if str(uid).isdigit():
                        ADMINS_CACHE.add(int(uid))
        except Exception as e:
            logging.error(f"Error merging Firebase settings/admins to cache: {e}")

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


def get_setting(key: str, default_val: str = "") -> str:
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]
    return default_val


def set_setting(key: str, value: str):
    str_val = str(value)
    SETTINGS_CACHE[key] = str_val

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


async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def save_user(user_id: int, first_name: str = ""):
    cur_date = get_bd_date_str()
    first_name = first_name.strip() if first_name else ""

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            user_ref = db.reference(f"users/{user_id}")
            u_data = user_ref.get()
            if not u_data:
                user_ref.set({
                    "exists": True, 
                    "balance": 0.0,
                    "today_earned": 0.0,
                    "total_earned": 0.0,
                    "refer_earned": 0.0,
                    "total_otps": 0,
                    "weekly_otps": 0,
                    "first_name": first_name,
                    "last_earn_date": cur_date
                })
            elif first_name:
                db.reference(f"users/{user_id}/first_name").set(first_name)
        except Exception as e:
            logging.error(f"Error saving user to Firebase: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, balance, today_earned, total_earned, refer_earned, total_otps, weekly_otps, first_name, last_earn_date) VALUES (?, 0.0, 0.0, 0.0, 0.0, 0, 0, ?, ?)", (user_id, first_name, cur_date))
        if first_name:
            cursor.execute("UPDATE users SET first_name = ? WHERE user_id = ?", (first_name, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error saving user to SQLite: {e}")


def check_and_process_weekly_reset_sync(bot_app=None):
    current_friday = get_current_friday_str()
    last_reset = get_setting("last_weekly_reset_friday", "")

    if not last_reset:
        set_setting("last_weekly_reset_friday", current_friday)
        return

    if last_reset != current_friday:
        is_bonus_enabled = get_setting("ranking_bonus_enabled", "true") == "true"
        top_users = []

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            try:
                fb_users = db.reference("users").get() or {}
                u_list = []
                if isinstance(fb_users, dict):
                    for uid, udata in fb_users.items():
                        if isinstance(udata, dict) and str(uid).isdigit():
                            w_otps = int(udata.get("weekly_otps", 0))
                            t_otps = int(udata.get("total_otps", 0))
                            if w_otps > 0:
                                u_list.append((int(uid), w_otps, t_otps))
                u_list.sort(key=lambda x: (x[1], x[2]), reverse=True)
                top_users = u_list[:3]
            except Exception as e:
                logging.error(f"Firebase fetch top ranking error: {e}")
        else:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT user_id, weekly_otps, total_otps FROM users WHERE weekly_otps > 0 ORDER BY weekly_otps DESC, total_otps DESC LIMIT 3")
                top_users = cursor.fetchall()
                conn.close()
            except Exception as e:
                logging.error(f"SQLite fetch top ranking error: {e}")

        if is_bonus_enabled and top_users:
            for rank_idx, u_info in enumerate(top_users, start=1):
                uid = u_info[0]
                b_str = get_setting(f"rank_bonus_{rank_idx}", "0")
                try:
                    b_amt = float(b_str)
                except ValueError:
                    b_amt = 0.0

                if b_amt > 0:
                    refund_user_balance_sync(uid, b_amt)
                    if bot_app:
                        msg = (
                            "🎉 <b>CONGRATULATIONS! WEEKLY RANKING BONUS!</b>\n\n"
                            f"You earned a <b>{fmt_num(b_amt)} ৳</b> bonus for ranking <b>Top {rank_idx}</b> this week! 🏆\n"
                            "Bonus added to your wallet."
                        )
                        try:
                            asyncio.create_task(bot_app.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML"))
                        except Exception as e:
                            logging.error(f"Failed sending rank bonus notification to {uid}: {e}")

        if CURRENT_DB_MODE == "Firebase (Cloud)":
            try:
                fb_users = db.reference("users").get() or {}
                if isinstance(fb_users, dict):
                    for uid in fb_users.keys():
                        if str(uid).isdigit():
                            db.reference(f"users/{uid}/weekly_otps").set(0)
            except Exception as e:
                logging.error(f"Firebase reset weekly otps error: {e}")

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET weekly_otps = 0")
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"SQLite reset weekly otps error: {e}")

        set_setting("last_weekly_reset_friday", current_friday)


def get_ranking_leaderboard_sync(user_id: int, bot_app=None) -> str:
    check_and_process_weekly_reset_sync(bot_app=bot_app)

    top_5 = []
    user_rank = "N/A"
    user_weekly_otps = 0

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            fb_users = db.reference("users").get() or {}
            all_list = []
            if isinstance(fb_users, dict):
                for uid, udata in fb_users.items():
                    if isinstance(udata, dict) and str(uid).isdigit():
                        uid_int = int(uid)
                        w_otps = int(udata.get("weekly_otps", 0))
                        t_otps = int(udata.get("total_otps", 0))
                        fname = str(udata.get("first_name", f"User {uid_int}")) or f"User {uid_int}"
                        if uid_int == user_id:
                            user_weekly_otps = w_otps
                        if w_otps > 0:
                            all_list.append((uid_int, fname, w_otps, t_otps))

            all_list.sort(key=lambda x: (x[2], x[3]), reverse=True)
            top_5 = all_list[:5]

            for idx, item in enumerate(all_list, start=1):
                if item[0] == user_id:
                    user_rank = f"#{idx}"
                    break
        except Exception as e:
            logging.error(f"Firebase leaderboard fetch error: {e}")
    else:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, first_name, weekly_otps, total_otps FROM users WHERE weekly_otps > 0 ORDER BY weekly_otps DESC, total_otps DESC LIMIT 5")
            top_5 = cursor.fetchall()

            cursor.execute("SELECT weekly_otps FROM users WHERE user_id = ?", (user_id,))
            u_row = cursor.fetchone()
            if u_row:
                user_weekly_otps = u_row[0] or 0

            if user_weekly_otps > 0:
                cursor.execute("SELECT COUNT(*) FROM users WHERE weekly_otps > ?", (user_weekly_otps,))
                higher_cnt = cursor.fetchone()[0]
                user_rank = f"#{higher_cnt + 1}"

            conn.close()
        except Exception as e:
            logging.error(f"SQLite leaderboard fetch error: {e}")

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    leaderboard_text = "🏆 <b>WEEKLY TOP OTP RECEIVERS</b>\n━━━━━━━━━━━━━━━━━━━━\n"

    if top_5:
        for idx, row in enumerate(top_5):
            uid = row[0]
            fname = row[1] if row[1] else f"User {uid}"
            w_otps = row[2]
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            safe_fname = html.escape(fname)
            user_link = f'<a href="tg://user?id={uid}">{safe_fname}</a>'
            leaderboard_text += f"{medal} {user_link} — <b>{w_otps}</b> OTPs\n"
    else:
        leaderboard_text += "<i>No OTP receivers this week yet. Be the first!</i>\n"

    leaderboard_text += "━━━━━━━━━━━━━━━━━━━━\n"
    leaderboard_text += f"👤 <b>Your Rank:</b> {user_rank} ({user_weekly_otps} OTPs)"
    return leaderboard_text


def get_user_profile_sync(user_id: int) -> dict:
    current_date = get_bd_date_str()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            u_data = db.reference(f"users/{user_id}").get()
            if u_data and isinstance(u_data, dict):
                bal = float(u_data.get("balance", 0.0))
                last_date = str(u_data.get("last_earn_date", ""))
                today_earned = float(u_data.get("today_earned", 0.0)) if last_date == current_date else 0.0
                total_earned = float(u_data.get("total_earned", 0.0))
                refer_earned = float(u_data.get("refer_earned", 0.0))
                total_otps = int(u_data.get("total_otps", 0))

                if last_date != current_date:
                    db.reference(f"users/{user_id}/today_earned").set(0.0)
                    db.reference(f"users/{user_id}/last_earn_date").set(current_date)

                return {
                    "balance": bal,
                    "today_earned": today_earned,
                    "total_earned": total_earned,
                    "refer_earned": refer_earned,
                    "total_otps": total_otps
                }
        except Exception as e:
            logging.error(f"Firebase profile fetch error: {e}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, today_earned, total_earned, refer_earned, total_otps, last_earn_date FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            bal, today_e, total_e, refer_e, otps, last_date = row
            bal = float(bal or 0.0)
            today_e = float(today_e or 0.0)
            total_e = float(total_e or 0.0)
            refer_e = float(refer_e or 0.0)
            otps = int(otps or 0)
            last_date = str(last_date or "")

            if last_date != current_date:
                today_e = 0.0
                cursor.execute("UPDATE users SET today_earned = 0.0, last_earn_date = ? WHERE user_id = ?", (current_date, user_id))
                conn.commit()

            conn.close()
            return {
                "balance": bal,
                "today_earned": today_e,
                "total_earned": total_e,
                "refer_earned": refer_e,
                "total_otps": otps
            }
        conn.close()
    except Exception as e:
        logging.error(f"SQLite profile fetch error: {e}")

    return {
        "balance": 0.0,
        "today_earned": 0.0,
        "total_earned": 0.0,
        "refer_earned": 0.0,
        "total_otps": 0
    }


def add_user_balance_and_otp_sync(user_id: int, amount: float = 1.0) -> dict:
    """Credits one OTP's worth of balance atomically. Two OTPs landing for the same
    user at the same instant (different panels, same poll cycle) can no longer clobber
    each other's update."""
    current_date = get_bd_date_str()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        user_ref = db.reference(f"users/{user_id}")
        result = {"data": None}

        def txn(current):
            data = dict(current) if isinstance(current, dict) else {}
            bal = float(data.get("balance", 0.0))
            total_earned = float(data.get("total_earned", 0.0))
            total_otps = int(data.get("total_otps", 0))
            weekly_otps = int(data.get("weekly_otps", 0))
            last_date = str(data.get("last_earn_date", ""))
            today_earned = float(data.get("today_earned", 0.0)) if last_date == current_date else 0.0

            data["balance"] = bal + amount
            data["total_earned"] = total_earned + amount
            data["today_earned"] = today_earned + amount
            data["total_otps"] = total_otps + 1
            data["weekly_otps"] = weekly_otps + 1
            data["last_earn_date"] = current_date
            data["exists"] = True
            data.setdefault("refer_earned", 0.0)
            data.setdefault("first_name", "")
            result["data"] = data
            return data

        try:
            user_ref.transaction(txn)
        except Exception as e:
            logging.error(f"Firebase update user earnings error: {e}")

        data = result["data"]
        if data:
            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("INSERT OR IGNORE INTO users (user_id, balance, today_earned, total_earned, refer_earned, total_otps, weekly_otps, last_earn_date) VALUES (?, 0.0, 0.0, 0.0, 0.0, 0, 0, ?)", (user_id, current_date))
                cursor.execute("""
                    UPDATE users SET balance = ?, today_earned = ?, total_earned = ?, total_otps = ?, weekly_otps = ?, last_earn_date = ?
                    WHERE user_id = ?
                """, (data["balance"], data["today_earned"], data["total_earned"], data["total_otps"], data["weekly_otps"], current_date, user_id))
                conn.commit()
            except Exception as e:
                logging.error(f"SQLite mirror update user earnings error: {e}")
            finally:
                if conn:
                    conn.close()
            return {
                "balance": data["balance"],
                "today_earned": data["today_earned"],
                "total_earned": data["total_earned"],
                "refer_earned": float(data.get("refer_earned", 0.0)),
                "total_otps": data["total_otps"]
            }
        return get_user_profile_sync(user_id)

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, balance, today_earned, total_earned, refer_earned, total_otps, weekly_otps, first_name, last_earn_date) VALUES (?, 0.0, 0.0, 0.0, 0.0, 0, 0, '', ?)",
            (user_id, current_date)
        )
        cursor.execute("SELECT balance, today_earned, total_earned, refer_earned, total_otps, weekly_otps, last_earn_date FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        bal, today_e, total_e, refer_e, otps, w_otps, last_date = row
        bal = float(bal or 0.0)
        today_e = float(today_e or 0.0) if str(last_date or "") == current_date else 0.0
        total_e = float(total_e or 0.0)
        refer_e = float(refer_e or 0.0)
        otps = int(otps or 0)
        w_otps = int(w_otps or 0)

        new_bal = bal + amount
        new_today = today_e + amount
        new_total = total_e + amount
        new_otps = otps + 1
        new_weekly = w_otps + 1

        cursor.execute("""
            UPDATE users SET balance = ?, today_earned = ?, total_earned = ?, total_otps = ?, weekly_otps = ?, last_earn_date = ?
            WHERE user_id = ?
        """, (new_bal, new_today, new_total, new_otps, new_weekly, current_date, user_id))
        conn.commit()

        return {
            "balance": new_bal,
            "today_earned": new_today,
            "total_earned": new_total,
            "refer_earned": refer_e,
            "total_otps": new_otps
        }
    except Exception as e:
        conn.rollback()
        logging.error(f"SQLite update user earnings error: {e}")
        return get_user_profile_sync(user_id)
    finally:
        conn.close()


def get_user_balance_sync(user_id: int) -> float:
    prof = get_user_profile_sync(user_id)
    return prof["balance"]


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
        conn = get_db_connection()
        cursor = conn.cursor()

        fb_settings = db.reference("settings").get()
        if fb_settings and isinstance(fb_settings, dict):
            for k, v in fb_settings.items():
                if v is not None:
                    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (str(k), str(v)))

        fb_users = db.reference("users").get()
        if fb_users and isinstance(fb_users, dict):
            for uid, udata in fb_users.items():
                if str(uid).isdigit():
                    bal, t_e, tot_e, ref_e, otps, w_otps, fname, l_date = 0.0, 0.0, 0.0, 0.0, 0, 0, "", ""
                    if isinstance(udata, dict):
                        bal = float(udata.get("balance", 0.0))
                        t_e = float(udata.get("today_earned", 0.0))
                        tot_e = float(udata.get("total_earned", 0.0))
                        ref_e = float(udata.get("refer_earned", 0.0))
                        otps = int(udata.get("total_otps", 0))
                        w_otps = int(udata.get("weekly_otps", 0))
                        fname = str(udata.get("first_name", ""))
                        l_date = str(udata.get("last_earn_date", ""))
                    elif isinstance(udata, (int, float)):
                        bal = float(udata)

                    cursor.execute("INSERT OR IGNORE INTO users (user_id, balance, today_earned, total_earned, refer_earned, total_otps, weekly_otps, first_name, last_earn_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (int(uid), bal, t_e, tot_e, ref_e, otps, w_otps, fname, l_date))
                    cursor.execute("UPDATE users SET balance = ?, today_earned = ?, total_earned = ?, refer_earned = ?, total_otps = ?, weekly_otps = ?, first_name = ?, last_earn_date = ? WHERE user_id = ?", (bal, t_e, tot_e, ref_e, otps, w_otps, fname, l_date, int(uid)))

        fb_admins = db.reference("admins").get()
        if fb_admins and isinstance(fb_admins, dict):
            for uid, adata in fb_admins.items():
                if str(uid).isdigit():
                    name = adata.get("name", "Admin") if isinstance(adata, dict) else "Admin"
                    cursor.execute("INSERT OR REPLACE INTO admins (user_id, name) VALUES (?, ?)", (int(uid), str(name)))

        fb_panels = db.reference("api_panels").get()
        if fb_panels and isinstance(fb_panels, dict):
            for pid, pdata in fb_panels.items():
                if isinstance(pdata, dict):
                    p_id = str(pdata.get("id", pid))
                    name = str(pdata.get("name", ""))
                    url = str(pdata.get("url", ""))
                    token = str(pdata.get("token", ""))
                    pinterval = float(pdata.get("polling_interval", 5.0))
                    cursor.execute("INSERT OR REPLACE INTO api_panels (id, name, url, token, polling_interval) VALUES (?, ?, ?, ?, ?)", (p_id, name, url, token, pinterval))

        fb_methods = db.reference("withdraw_methods").get()
        if fb_methods and isinstance(fb_methods, dict):
            for m_name in fb_methods.keys():
                cursor.execute("INSERT OR IGNORE INTO withdraw_methods (name) VALUES (?)", (str(m_name),))

        fb_wreqs = db.reference("withdraw_requests").get()
        if fb_wreqs and isinstance(fb_wreqs, dict):
            for rid, rdata in fb_wreqs.items():
                if isinstance(rdata, dict):
                    r_id = int(rdata.get("id", rid))
                    u_id = int(rdata.get("user_id", 0))
                    meth = str(rdata.get("method", ""))
                    wnum = str(rdata.get("wallet_number", ""))
                    amt = float(rdata.get("amount", 0.0))
                    st = str(rdata.get("status", "pending"))
                    rr = str(rdata.get("reject_reason", ""))
                    ca = int(rdata.get("created_at", 0))
                    cursor.execute("INSERT OR REPLACE INTO withdraw_requests (id, user_id, method, wallet_number, amount, status, reject_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (r_id, u_id, meth, wnum, amt, st, rr, ca))

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
    cleaned_numbers = []
    seen = set()
    for num in numbers:
        clean_num = re.sub(r'\D', '', str(num))
        if clean_num and clean_num not in seen:
            seen.add(clean_num)
            cleaned_numbers.append(clean_num)

    if not cleaned_numbers:
        return 0

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
                continue
            if isinstance(existing, dict) and num in existing:
                continue
            batch[num] = {"number": num, "status": "available", "user_id": 0}

        if batch:
            ref.update(batch)
        db.reference(f"services/{service}/{country}").set(True)
        inserted = len(batch)
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO services (service_name, country_name) VALUES (?, ?)", (service, country))

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
    exclude = {str(x) for x in (exclude or [])}
    target_qty = max(1, int(target_qty))

    if CURRENT_DB_MODE == "Firebase (Cloud)":
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

            try:
                db.reference(f"numbers/{service}/{country}/{num}").update({
                    "status": "allocated",
                    "user_id": user_id,
                    "allocated_at": int(time.time())
                })
                assigned.append(num)
            except Exception as e:
                logging.error(f"Firebase number status update failed for {num}: {e}")
                try:
                    lock_ref.delete()
                except Exception:
                    pass

        refresh_services_cache_sync()
        return assigned

    conn = get_db_connection()
    cursor = conn.cursor()
    assigned = []
    try:
        conn.execute("BEGIN IMMEDIATE")

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

        conn.commit()
        refresh_services_cache_sync()
        return assigned
    except Exception as e:
        conn.rollback()
        logging.error(f"SQLite allocation failed: {e}")
        return []
    finally:
        conn.close()


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
def build_admin_control_view():
    admins = get_all_admins_sync()
    text = "🛠 **ADMIN CONTROL MANAGEMENT**\n\nList of System Admins:\n"
    buttons = []

    for adm in admins:
        uid = adm["user_id"]
        name = adm["name"]
        is_owner = adm.get("is_owner", False) or (uid == ADMIN_ID)
        role_label = "👑 Main Owner" if is_owner else "🛡️ Admin"

        text += f"\n• **{escape_md(name)}** (`{uid}`) - {role_label}"
        if not is_owner:
            buttons.append([create_button(f"🗑️ Remove {name}", callback_data=f"adm:delconf:{uid}", style="danger")])

    buttons.append([create_button("➕ Add New Admin", callback_data="adm:add:start", style="success")])
    return text, InlineKeyboardMarkup(buttons)


def build_admin_services_view():
    summary = get_admin_services_summary()
    if not summary:
        text = "📱 **SERVICES MANAGEMENT**\n\nNo services added yet."
        buttons = [[create_button("➕ Add New Service", callback_data="adm:srv:add", style="success")]]
        return text, InlineKeyboardMarkup(buttons)

    text = "📱 **SERVICES MANAGEMENT**\n\nSummary:"
    buttons = []
    for srv, cnts in summary.items():
        total_avail = sum(cnts.values())
        text += f"\n🔹 **{escape_md(srv)}** (Total Available: `{total_avail}`)"
        for cnt, count in cnts.items():
            text += f"\n   └ {escape_md(cnt)}: `{count}`"
        buttons.append([create_button(f"⚙️ {srv}", callback_data=f"adm:srv:view:{srv}", style="primary")])

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
    otp_grp_id = get_setting("otp_group_id", OTP_GROUP_ID if OTP_GROUP_ID else "Not Set")
    dev_name = get_setting("dev_username", "developer")
    dev_link = get_setting("dev_link", "https://t.me/developer")

    text = (
        f"🔗 **EDIT LINKS & FORWARD SETTINGS**\n\n"
        f"📢 **Channel:** {escape_md(ch_val)}\n"
        f"🎧 **Support:** {escape_md(sp_val)}\n"
        f"🔗 **OTP Group Link:** {escape_md(otp_link)}\n"
        f"🆔 **OTP Forward Group ID:** `{escape_md(str(otp_grp_id))}`\n"
        f"👨‍💻 **Dev Name:** `{escape_md(dev_name)}`\n"
        f"🔗 **Dev Link:** {escape_md(dev_link)}\n\n"
        f"Click below to modify settings:"
    )
    buttons = [
        [
            create_button("📢 Edit Channel", callback_data="adm:set:channel", style="primary"),
            create_button("🎧 Edit Support", callback_data="adm:set:support", style="primary")
        ],
        [
            create_button("🔗 Edit Group Link", callback_data="adm:set:otplink", style="primary"),
            create_button("🆔 Edit Forward Group ID", callback_data="adm:set:otpgroupid", style="primary")
        ],
        [
            create_button("👨‍💻 Edit Dev Name", callback_data="adm:set:devname", style="primary"),
            create_button("🔗 Edit Dev Link", callback_data="adm:set:devlink", style="primary")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_api_panels_view():
    panels = get_all_api_panels_sync()
    if not panels:
        text = "🌐 **API PANELS MANAGEMENT**\n\nNo API panels connected yet."
        buttons = [[create_button("➕ Connect New Panel", callback_data="adm:api:add", style="success")]]
        return text, InlineKeyboardMarkup(buttons)

    text = "🌐 **API PANELS MANAGEMENT**\n\nConnected API Panels List:\n"
    buttons = []
    for p in panels:
        pid = p["id"]
        pname = p["name"]
        pinterval = p.get("polling_interval", 5.0)
        text += f"\n🔹 **{escape_md(pname)}** (Polling: `{pinterval}s`)"
        buttons.append([create_button(f"⚙️ {pname}", callback_data=f"adm:api:view:{pid}", style="primary")])

    buttons.append([create_button("➕ Connect New Panel", callback_data="adm:api:add", style="success")])
    return text, InlineKeyboardMarkup(buttons)


def build_panel_manage_view(panel_id: str):
    p = get_api_panel_sync(panel_id)
    if not p:
        return "⚠️ Panel not found.", InlineKeyboardMarkup([[create_button("Back to Panels", callback_data="adm:api:list", style="danger")]])

    masked_token = mask_api_key(p['token'])

    text = (
        f"⚙️ **PANEL DETAILS: {escape_md(p['name'])}**\n\n"
        f"📌 **Panel Name:** {escape_md(p['name'])}\n"
        f"🔗 **Base URL:** `{escape_md(p['url'])}` \n"
        f"🔑 **API Key/Token:** `{escape_md(masked_token)}` \n"
        f"⏱️ **Polling Time:** `{p['polling_interval']}s`\n"
    )
    buttons = [
        [create_button("🗑️ Delete Panel", callback_data=f"adm:api:delconf:{panel_id}", style="danger")],
        [create_button("Back to Panels", callback_data="adm:api:list", style="primary")]
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
    show_dev = get_setting("show_developer", "true") == "true"
    withdraw_on = get_setting("withdraw_enabled", "true") == "true"
    ranking_bonus_on = get_setting("ranking_bonus_enabled", "true") == "true"

    msg_status = "ENABLED 🟢" if show_msg else "DISABLED 🔴"
    count_status = "ENABLED 🟢" if show_country_count else "DISABLED 🔴"
    dev_status = "ENABLED 🟢" if show_dev else "DISABLED 🔴"
    withdraw_status = "ENABLED 🟢" if withdraw_on else "DISABLED 🔴"
    ranking_status = "ENABLED 🟢" if ranking_bonus_on else "DISABLED 🔴"

    text = (
        "⚙️ **EXTRA SETTINGS**\n\n"
        f"📩 **Show OTP Message:** `{msg_status}`\n"
        f"🔢 **Show Country Number Count:** `{count_status}`\n"
        f"👨‍💻 **Show Developer Info:** `{dev_status}`\n"
        f"💸 **Withdraw System:** `{withdraw_status}`\n"
        f"🏆 **Ranking Bonus System:** `{ranking_status}`\n\n"
        "Use the buttons below to enable/disable or configure options."
    )
    buttons = [
        [create_button(f"Show Message: {msg_status}", callback_data="adm:toggle:show_msg", style="success" if show_msg else "danger")],
        [create_button(f"Country Count: {count_status}", callback_data="adm:toggle:country_count", style="success" if show_country_count else "danger")],
        [create_button(f"Show Developer: {dev_status}", callback_data="adm:toggle:show_dev", style="success" if show_dev else "danger")],
        [create_button(f"Withdraw System: {withdraw_status}", callback_data="adm:toggle:withdraw", style="success" if withdraw_on else "danger")],
        [create_button(f"Ranking Bonus: {ranking_status}", callback_data="adm:toggle:ranking_bonus", style="success" if ranking_bonus_on else "danger")],
        [
            create_button("💳 Withdraw Settings", callback_data="adm:w_settings", style="primary"),
            create_button("🏆 Ranking Bonuses", callback_data="adm:r_bonus_settings", style="primary")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_ranking_bonus_settings_view():
    bonus_enabled = get_setting("ranking_bonus_enabled", "true") == "true"
    status_str = "ENABLED 🟢" if bonus_enabled else "DISABLED 🔴"

    b1 = get_setting("rank_bonus_1", "50")
    b2 = get_setting("rank_bonus_2", "30")
    b3 = get_setting("rank_bonus_3", "20")

    text = (
        "🏆 **RANKING BONUS SETTINGS**\n\n"
        f"📌 **Status:** `{status_str}`\n\n"
        f"🥇 **Top 1 Bonus:** `{b1} ৳`\n"
        f"🥈 **Top 2 Bonus:** `{b2} ৳`\n"
        f"🥉 **Top 3 Bonus:** `{b3} ৳`\n"
    )
    buttons = [
        [create_button("✏️ Edit Top 1 Bonus", callback_data="adm:r_set_b1", style="primary")],
        [create_button("✏️ Edit Top 2 Bonus", callback_data="adm:r_set_b2", style="primary")],
        [create_button("✏️ Edit Top 3 Bonus", callback_data="adm:r_set_b3", style="primary")],
        [create_button("Back", callback_data="adm:extra_back", style="danger")]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_withdraw_settings_view():
    min_w = get_setting("min_withdraw_amount", "50")
    methods = get_withdraw_methods_sync()

    text = (
        "💳 **WITHDRAW SETTINGS**\n\n"
        f"💰 **Minimum Withdraw Amount:** `{min_w} ৳`\n\n"
        "📌 **Active Payment Methods:**\n"
    )
    if methods:
        for m in methods:
            text += f"• **{escape_md(m)}**\n"
    else:
        text += "No active payment methods.\n"

    buttons = [
        [
            create_button("➕ Add Method", callback_data="adm:w_add_m", style="success"),
            create_button("❌ Remove Method", callback_data="adm:w_del_m_list", style="danger")
        ],
        [
            create_button("✏️ Edit Min Amount", callback_data="adm:w_set_min", style="primary")
        ],
        [
            create_button("Back", callback_data="adm:extra_back", style="danger")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_admin_withdraw_requests_view(page: int = 1):
    reqs = get_all_withdraw_requests_sync()
    per_page = 25
    total_items = len(reqs)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    start_idx = (page - 1) * per_page
    page_reqs = reqs[start_idx:start_idx + per_page]

    text = f"📋 **WITHDRAW REQUESTS (Page {page}/{total_pages})**\nTotal Requests: `{total_items}`\n\nClick on any request to view & manage:"

    buttons = []
    for r in page_reqs:
        st_icon = "⏳" if r["status"] == "pending" else ("✅" if r["status"] == "approved" else "❌")
        btn_text = f"{r['wallet_number']} • {fmt_num(r['amount'])}৳ {st_icon}"
        buttons.append([create_button(btn_text, callback_data=f"adm:w_view:{r['id']}", style="primary")])

    nav_row = []
    if page > 1:
        nav_row.append(create_button("⬅️ Prev", callback_data=f"adm:w_page:{page-1}", style="primary"))
    if total_pages > 1:
        nav_row.append(create_button(f"{page}/{total_pages}", callback_data="noop", style="secondary"))
    if page < total_pages:
        nav_row.append(create_button("Next ➡️", callback_data=f"adm:w_page:{page+1}", style="primary"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([create_button("Back to Panel", callback_data="adm:panel_back", style="danger")])
    return text, InlineKeyboardMarkup(buttons)


def build_withdraw_detail_view(req_id: int):
    r = get_withdraw_request_by_id_sync(req_id)
    if not r:
        return "⚠️ Request not found.", InlineKeyboardMarkup([[create_button("Back", callback_data="adm:w_page:1", style="danger")]])

    dt_str = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else "N/A"
    st_text = r["status"].upper()

    text = (
        f"💳 **WITHDRAW REQUEST #{r['id']}**\n\n"
        f"👤 **User ID:** `{r['user_id']}`\n"
        f"📱 **Method:** `{escape_md(r['method'])}` \n"
        f"💳 **Account / Wallet:** `{escape_md(r['wallet_number'])}` \n"
        f"💰 **Amount:** `{fmt_num(r['amount'])} ৳`\n"
        f"📌 **Status:** `{st_text}`\n"
        f"📅 **Requested At:** `{dt_str}`\n"
    )
    if r["status"] == "rejected" and r["reject_reason"]:
        text += f"\n❌ **Reason:**\n> {escape_md(r['reject_reason'])}\n"

    buttons = []
    if r["status"] == "pending":
        buttons.append([
            create_button("✅ APPROVE", callback_data=f"adm:w_app:{r['id']}", style="success"),
            create_button("❌ REJECT", callback_data=f"adm:w_rej_start:{r['id']}", style="danger")
        ])
    buttons.append([create_button("Back to Requests", callback_data="adm:w_page:1", style="primary")])

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
def init_firebase_system(run_migration=True):
    """Connects to Firebase using credentials from environment variables only
    (FIREBASE_BASE64 or FIREBASE_CONFIG_JSON in .env / host env vars).

    Because these are real environment variables rather than a file written to local
    disk, they survive restarts/redeploys on hosts with an ephemeral filesystem
    (Render, Railway, etc.) — so the bot reconnects to Firebase automatically on every
    boot, before it starts polling for OTPs. No manual "upload firebase.json" step needed."""
    global CURRENT_DB_MODE
    if not HAS_FIREBASE_LIB:
        CURRENT_DB_MODE = "SQLite (Local)"
        refresh_all_caches_sync()
        return False

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
        if firebase_b64:
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
            logging.info("Firebase connected successfully from environment variables!")
            return True
        else:
            logging.info("No FIREBASE_BASE64 / FIREBASE_CONFIG_JSON set — running in SQLite (Local) mode.")
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

    cursor.execute("SELECT user_id, balance, today_earned, total_earned, refer_earned, total_otps, weekly_otps, first_name, last_earn_date FROM users")
    users = cursor.fetchall()
    for u in users:
        db.reference(f"users/{u[0]}").set({
            "exists": True, 
            "balance": u[1] if len(u) > 1 else 0.0,
            "today_earned": u[2] if len(u) > 2 else 0.0,
            "total_earned": u[3] if len(u) > 3 else 0.0,
            "refer_earned": u[4] if len(u) > 4 else 0.0,
            "total_otps": u[5] if len(u) > 5 else 0,
            "weekly_otps": u[6] if len(u) > 6 else 0,
            "first_name": u[7] if len(u) > 7 else "",
            "last_earn_date": u[8] if len(u) > 8 else ""
        })

    cursor.execute("SELECT user_id, name FROM admins")
    admins = cursor.fetchall()
    for a in admins:
        db.reference(f"admins/{a[0]}").set({"name": a[1]})

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

    existing_fb_panels = db.reference("api_panels").get() or {}
    cursor.execute("SELECT id, name, url, token, polling_interval FROM api_panels")
    rows = cursor.fetchall()
    for row in rows:
        pid, name, url, token, pinterval = row
        db.reference(f"api_panels/{pid}").set({
            "id": str(pid), "name": name, "url": url, "token": token, "polling_interval": pinterval
        })
    if not rows and existing_fb_panels and isinstance(existing_fb_panels, dict):
        for pid, pdata in existing_fb_panels.items():
            if isinstance(pdata, dict):
                p_id = str(pdata.get("id", pid))
                cursor.execute("INSERT OR REPLACE INTO api_panels (id, name, url, token, polling_interval) VALUES (?, ?, ?, ?, ?)",
                    (p_id, str(pdata.get("name","")), str(pdata.get("url","")), str(pdata.get("token","")), float(pdata.get("polling_interval", 5.0))))

    cursor.execute("SELECT name FROM withdraw_methods")
    methods = cursor.fetchall()
    for m in methods:
        db.reference(f"withdraw_methods/{m[0]}").set(True)

    cursor.execute("SELECT id, user_id, method, wallet_number, amount, status, reject_reason, created_at FROM withdraw_requests")
    reqs = cursor.fetchall()
    for r in reqs:
        db.reference(f"withdraw_requests/{r[0]}").set({
            "id": r[0], "user_id": r[1], "method": r[2], "wallet_number": r[3],
            "amount": r[4], "status": r[5], "reject_reason": r[6], "created_at": r[7]
        })

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


init_firebase_system(run_migration=True)

app = Flask(__name__)

@app.route('/')
def home():
    return f"Bot running! Current DB Mode: {CURRENT_DB_MODE}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# States for Admin & User Conversations
(
    ADD_SERVICE,
    ADD_COUNTRY,
    ADD_NUMBERS,
    WAIT_CHANNEL,
    WAIT_SUPPORT,
    WAIT_OTP_LINK,
    WAIT_OTP_GROUP_ID,
    WAIT_BROADCAST_MSG,
    WAIT_PANEL_NAME,
    WAIT_PANEL_URL,
    WAIT_PANEL_TOKEN,
    WAIT_PANEL_INTERVAL,
    WAIT_ADMIN_ID,
    WAIT_ADMIN_NAME,
    WAIT_WITHDRAW_WALLET,
    WAIT_WITHDRAW_AMOUNT,
    WAIT_NEW_WITHDRAW_METHOD,
    WAIT_MIN_WITHDRAW_AMOUNT,
    WAIT_REJECT_REASON,
    WAIT_DEV_NAME,
    WAIT_DEV_LINK,
    WAIT_RANK1_BONUS,
    WAIT_RANK2_BONUS,
    WAIT_RANK3_BONUS,
) = range(24)


# ---------------- AUTH DECORATOR ----------------
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        query = update.callback_query
        user = update.effective_user
        if query:
            await query.answer()
        if not user or not (await run_db(is_admin_sync, user.id)):
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
            {"text": "RANKING", "style": "danger"},
            {"text": "SUPPORT", "style": "danger"}
        ]
    ]
    if is_admin_sync(user_id):
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
            {"text": "MANAGE PAYOUTS", "style": "primary"},
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
            {"text": "NUMBER QUANTITY", "style": "primary"}
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
    first_name = update.effective_user.first_name or ""
    await run_db(save_user, user_id, first_name)

    context.user_data.pop('service_name', None)
    context.user_data.pop('country_name', None)
    context.user_data['current_menu'] = 'main'
    first_name_esc = escape_md(first_name or "User")
    msg = f"Welcome, {first_name_esc}!\nPlease select an option from the menu:"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")


async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or ""
    await run_db(save_user, user_id, first_name)

    text_upper = text.strip().upper()
    user_is_admin = is_admin_sync(user_id)

    if text_upper == "GET NUMBER":
        kbd, msg = get_services_keyboard()
        if not kbd:
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(msg, reply_markup=kbd)

    elif text_upper == "PROFILE":
        first_name_esc = escape_md(first_name or "User")
        bot_username = context.bot.username or "bot"
        refer_link = f"https://t.me/{bot_username}?start={user_id}"
        
        prof = await run_db(get_user_profile_sync, user_id)
        bal_str = fmt_num(prof["balance"])
        today_str = fmt_num(prof["today_earned"])
        total_str = fmt_num(prof["total_earned"])
        refer_str = fmt_num(prof["refer_earned"])
        total_otps = prof["total_otps"]

        profile_text = (
            "👤 **USER PROFILE**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Name: {first_name_esc}\n"
            f"🆔 ID: `{user_id}`\n"
            f"💰 Balance: {bal_str} ৳\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Today's Earn: {today_str} ৳\n"
            f"📈 Total Earned: {total_str} ৳\n"
            f"👥 Earned from Refer: {refer_str} ৳\n"
            f"📨 Total OTPs: {total_otps}\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        kbd = InlineKeyboardMarkup([
            [create_button("Referral Link", copy_text=refer_link, style="success")]
        ])
        await update.message.reply_text(profile_text, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "WALLET":
        bal = await run_db(get_user_balance_sync, user_id)
        bal_str = fmt_num(bal)
        wallet_text = (
            f"👛 **YOUR WALLET**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Balance:** `{bal_str} ৳`"
        )
        kbd = InlineKeyboardMarkup([
            [create_button("💸 Withdraw", callback_data="usr:withdraw", style="success")]
        ])
        await update.message.reply_text(wallet_text, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper in ["RANKING", "LEADERBOARD"]:
        leaderboard_msg = await run_db(get_ranking_leaderboard_sync, user_id, context.application)
        await update.message.reply_text(leaderboard_msg, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

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

    elif text_upper == "ADMIN PANEL" and user_is_admin:
        context.user_data['current_menu'] = 'admin'
        total_users = len(await run_db(get_all_users))
        await update.message.reply_text(
            f"**ADMIN PANEL**\n\n"
            f"⚙️ DB Mode: **{CURRENT_DB_MODE}**\n"
            f"👥 Total Registered Users: `{total_users}`",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )

    elif (text_upper == "MANAGE PAYOUTS" or text_upper == "WITHDRAW") and user_is_admin:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_withdraw_requests_view(1)
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "SERVICES" and user_is_admin:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_services_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "GLOBAL SETTINGS" and user_is_admin:
        context.user_data['current_menu'] = 'global_settings'
        await update.message.reply_text(
            "⚙️ **GLOBAL SETTINGS MENU**\n\nSelect an option from below keyboard:",
            reply_markup=get_global_settings_keyboard(),
            parse_mode="Markdown"
        )

    elif text_upper == "EDIT LINKS" and user_is_admin:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_edit_links_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "EDIT API" and user_is_admin:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_api_panels_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "NUMBER QUANTITY" and user_is_admin:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_number_quantity_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "EXTRA" and user_is_admin:
        context.user_data['current_menu'] = 'global_settings'
        text_msg, kbd = build_extra_settings_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "ADMIN CONTROL" and user_is_admin:
        context.user_data['current_menu'] = 'admin'
        text_msg, kbd = build_admin_control_view()
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "BACK":
        curr_menu = context.user_data.get('current_menu', 'main')
        if curr_menu == 'global_settings' and user_is_admin:
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


# ---------------- USER WITHDRAW CONVERSATION ----------------
async def user_start_withdraw_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    withdraw_on = get_setting("withdraw_enabled", "true") == "true"
    if not withdraw_on:
        await query.edit_message_text("❌ Withdraw system is currently disabled by Admin.")
        return ConversationHandler.END

    method = query.data.split(":", 2)[2]
    user_id = query.from_user.id
    
    bal = await run_db(get_user_balance_sync, user_id)
    min_w = float(get_setting("min_withdraw_amount", "50"))

    if bal < min_w:
        await query.edit_message_text(f"❌ Minimum withdraw amount is `{fmt_num(min_w)} ৳`.\nYour current balance is `{fmt_num(bal)} ৳`.", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data['w_method'] = method
    await query.edit_message_text(f"Please enter your **{escape_md(method)}** account number / wallet address:", parse_mode="Markdown")
    return WAIT_WITHDRAW_WALLET


async def receive_withdraw_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet_no = update.message.text.strip()
    user_id = update.effective_user.id
    method = context.user_data.get('w_method', '')

    method_lower = method.lower()
    is_valid = True
    error_msg = ""

    if any(m in method_lower for m in ['bkash', 'nagad', 'rocket', 'upay', 'cellfin']):
        clean_num = re.sub(r'\D', '', wallet_no)
        if len(clean_num) < 11:
            is_valid = False
            error_msg = "❌ Invalid Account Number! Mobile banking numbers (bKash/Nagad/Rocket) must be at least 11 digits."
    elif any(m in method_lower for m in ['trc', 'usdt', 'crypto', 'wallet']):
        if len(wallet_no) < 30 or not wallet_no.isalnum():
            is_valid = False
            error_msg = "❌ Invalid Wallet Address! Please enter a valid Crypto/TRC20 wallet address."
    else:
        if len(wallet_no) < 8:
            is_valid = False
            error_msg = "❌ Invalid Details! Account / Wallet address must be at least 8 characters long."

    if not is_valid:
        await update.message.reply_text(f"{error_msg}\n\nPlease enter a valid **{escape_md(method)}** account number / wallet address again:", parse_mode="Markdown")
        return WAIT_WITHDRAW_WALLET

    context.user_data['w_wallet'] = wallet_no

    bal = await run_db(get_user_balance_sync, user_id)
    min_w = float(get_setting("min_withdraw_amount", "50"))

    await update.message.reply_text(
        f"Selected Method: **{escape_md(method)}**\n"
        f"Wallet/Account: `{escape_md(wallet_no)}` \n\n"
        f"Enter withdraw amount:\n"
        f"📌 Minimum Amount: `{fmt_num(min_w)} ৳`\n"
        f"💰 Available Balance: `{fmt_num(bal)} ৳`",
        parse_mode="Markdown"
    )
    return WAIT_WITHDRAW_AMOUNT


async def receive_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    input_text = update.message.text.strip()

    try:
        amount = float(input_text)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount! Please enter numbers only.\nType /cancel to abort.")
        return WAIT_WITHDRAW_AMOUNT

    bal = await run_db(get_user_balance_sync, user_id)
    min_w = float(get_setting("min_withdraw_amount", "50"))

    if amount < min_w:
        await update.message.reply_text(f"❌ Amount cannot be less than minimum withdraw limit (`{fmt_num(min_w)} ৳`).\nPlease enter again:")
        return WAIT_WITHDRAW_AMOUNT

    if amount > bal:
        await update.message.reply_text(f"❌ Insufficient balance! Your available balance is `{fmt_num(bal)} ৳`.\nPlease enter again:")
        return WAIT_WITHDRAW_AMOUNT

    method = context.user_data.get('w_method')
    wallet_no = context.user_data.get('w_wallet')

    success = await run_db(deduct_user_balance_sync, user_id, amount)
    if not success:
        await update.message.reply_text("❌ Failed to process withdraw. Insufficient balance.")
        context.user_data.pop('w_method', None)
        context.user_data.pop('w_wallet', None)
        return ConversationHandler.END

    req_id = await run_db(create_withdraw_request_sync, user_id, method, wallet_no, amount)

    success_msg = (
        "✅ **WITHDRAWAL REQUEST SUBMITTED**\n\n"
        f"📌 **Request ID:** `#{req_id}`\n"
        f"📱 **Method:** `{escape_md(method)}` \n"
        f"💳 **Account:** `{escape_md(wallet_no)}` \n"
        f"💰 **Amount:** `{fmt_num(amount)} ৳`\n\n"
        "Your request has been submitted to Admin for approval."
    )
    await update.message.reply_text(success_msg, parse_mode="Markdown")

    context.user_data.pop('w_method', None)
    context.user_data.pop('w_wallet', None)
    return ConversationHandler.END


# ---------------- RANKING BONUS CONVERSATION HANDLERS ----------------
@admin_only
async def set_rank1_bonus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("Enter bonus amount for **Top 1** user (e.g., 50):", parse_mode="Markdown")
    return WAIT_RANK1_BONUS

async def receive_rank1_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val_str = update.message.text.strip()
    try:
        val = float(val_str)
        if val < 0: val = 0.0
    except ValueError:
        val = 50.0

    await run_db(set_setting, "rank_bonus_1", str(val))
    await update.message.reply_text(f"✅ Top 1 bonus set to `{fmt_num(val)} ৳`!", parse_mode="Markdown")
    text_msg, kbd = build_ranking_bonus_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

@admin_only
async def set_rank2_bonus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("Enter bonus amount for **Top 2** user (e.g., 30):", parse_mode="Markdown")
    return WAIT_RANK2_BONUS

async def receive_rank2_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val_str = update.message.text.strip()
    try:
        val = float(val_str)
        if val < 0: val = 0.0
    except ValueError:
        val = 30.0

    await run_db(set_setting, "rank_bonus_2", str(val))
    await update.message.reply_text(f"✅ Top 2 bonus set to `{fmt_num(val)} ৳`!", parse_mode="Markdown")
    text_msg, kbd = build_ranking_bonus_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

@admin_only
async def set_rank3_bonus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("Enter bonus amount for **Top 3** user (e.g., 20):", parse_mode="Markdown")
    return WAIT_RANK3_BONUS

async def receive_rank3_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val_str = update.message.text.strip()
    try:
        val = float(val_str)
        if val < 0: val = 0.0
    except ValueError:
        val = 20.0

    await run_db(set_setting, "rank_bonus_3", str(val))
    await update.message.reply_text(f"✅ Top 3 bonus set to `{fmt_num(val)} ৳`!", parse_mode="Markdown")
    text_msg, kbd = build_ranking_bonus_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------- DEVELOPER INFO & ADMIN SETTINGS CONVERSATIONS ----------------
@admin_only
async def set_dev_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.reply_text("Enter the new Developer Name/Tag (e.g., John Doe):")
    return WAIT_DEV_NAME


async def receive_dev_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    await run_db(set_setting, "dev_username", new_name)
    await update.message.reply_text(f"✅ Developer Name updated successfully!\nCurrent Name: `{escape_md(new_name)}`", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


@admin_only
async def set_dev_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.reply_text("Enter the new Developer Telegram Link/Username (e.g., https://t.me/developer or @developer):")
    return WAIT_DEV_LINK


async def receive_dev_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_link = update.message.text.strip()
    await run_db(set_setting, "dev_link", new_link)
    await update.message.reply_text(f"✅ Developer Link updated successfully!\nCurrent Link: {escape_md(new_link)}", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


@admin_only
async def admin_add_w_method_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("Enter new payment method name (e.g., PayTM, Rocket):")
    return WAIT_NEW_WITHDRAW_METHOD


async def receive_new_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method_name = update.message.text.strip()
    if method_name:
        await run_db(add_withdraw_method_sync, method_name)
        await update.message.reply_text(f"✅ Payment method **{escape_md(method_name)}** added successfully!", parse_mode="Markdown")
    text_msg, kbd = build_withdraw_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


@admin_only
async def admin_set_min_w_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("Enter new minimum withdraw amount (e.g., 50):")
    return WAIT_MIN_WITHDRAW_AMOUNT


async def receive_min_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_text = update.message.text.strip()
    try:
        val = float(input_text)
        if val < 0:
            val = 0.0
    except ValueError:
        val = 50.0

    await run_db(set_setting, "min_withdraw_amount", str(val))
    await update.message.reply_text(f"✅ Minimum withdraw amount set to `{fmt_num(val)} ৳`!", parse_mode="Markdown")
    text_msg, kbd = build_withdraw_settings_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


@admin_only
async def admin_reject_w_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    req_id = int(query.data.split(":", 2)[2])
    context.user_data['reject_req_id'] = req_id
    await query.message.reply_text(f"Please enter the rejection reason for Withdraw Request `#{req_id}`:", parse_mode="Markdown")
    return WAIT_REJECT_REASON


async def receive_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason_text = update.message.text.strip()
    req_id = context.user_data.get('reject_req_id')

    if req_id:
        req = await run_db(get_withdraw_request_by_id_sync, req_id)
        if req and req["status"] == "pending":
            await run_db(refund_user_balance_sync, req["user_id"], req["amount"])
            await run_db(update_withdraw_status_sync, req_id, "rejected", reason_text)

            user_msg = (
                "❌ <b>WITHDRAWAL REJECTED</b>\n\n"
                f"Your withdrawal request of <b>{fmt_num(req['amount'])} ৳</b> via <b>{html.escape(req['method'])}</b> has been rejected.\n"
                f"<b>{fmt_num(req['amount'])} ৳</b> has been refunded to your wallet.\n\n"
                "<b>Reason:</b>\n"
                f"<blockquote expandable>{html.escape(reason_text)}</blockquote>"
            )
            try:
                await context.bot.send_message(chat_id=req["user_id"], text=user_msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to send rejection notification: {e}")

            await update.message.reply_text(f"✅ Request `#{req_id}` rejected and user notified.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Request was already processed or not found.")

    context.user_data.pop('reject_req_id', None)
    text_msg, kbd = build_admin_withdraw_requests_view(1)
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------- CONVERSATION HANDLERS (ADMIN SETUP) ----------------
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


# ---------------- ADD ADMIN CONVERSATION ----------------
@admin_only
async def admin_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.reply_text("Enter the Telegram User ID of the new Admin (e.g., 123456789):")
    else:
        await update.message.reply_text("Enter the Telegram User ID of the new Admin (e.g., 123456789):")
    return WAIT_ADMIN_ID


async def receive_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_text = update.message.text.strip()
    if not input_text.isdigit():
        await update.message.reply_text("❌ Invalid Telegram User ID! Please enter numbers only.\nType /cancel to abort.")
        return WAIT_ADMIN_ID

    context.user_data['new_admin_id'] = int(input_text)
    await update.message.reply_text("Enter Admin Name/Tag (e.g., Co-Admin John):")
    return WAIT_ADMIN_NAME


async def receive_admin_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_name = update.message.text.strip()
    new_id = context.user_data.get('new_admin_id')

    if new_id and admin_name:
        await run_db(add_admin_sync, new_id, admin_name)
        await update.message.reply_text(f"✅ Admin **{escape_md(admin_name)}** (`{new_id}`) added successfully!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Failed to add admin. Incomplete data.")

    context.user_data.pop('new_admin_id', None)
    text_msg, kbd = build_admin_control_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------- API PANEL CONVERSATION ----------------
@admin_only
async def admin_add_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.reply_text("Enter the Panel Name (e.g., Main Provider, Panel 1):")
    else:
        await update.message.reply_text("Enter the Panel Name (e.g., Main Provider, Panel 1):")
    return WAIT_PANEL_NAME


async def receive_panel_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_api_name'] = update.message.text.strip()
    await update.message.reply_text("Enter Base API URL (e.g., http://domain.com/viewstasx):")
    return WAIT_PANEL_URL


async def receive_panel_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_val = update.message.text.strip()
    context.user_data['new_api_url'] = url_val
    await update.message.reply_text("Enter API Key / Token:")
    return WAIT_PANEL_TOKEN


async def receive_panel_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_api_token'] = update.message.text.strip()
    await update.message.reply_text("Enter Polling Interval in seconds (Default: 5, Min: 3.1, Max: 10):")
    return WAIT_PANEL_INTERVAL


async def receive_panel_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_text = update.message.text.strip()
    try:
        interval_val = float(input_text)
        if interval_val < 3.1:
            interval_val = 3.1
        elif interval_val > 10.0:
            interval_val = 10.0
    except ValueError:
        interval_val = 5.0

    p_name = context.user_data.get('new_api_name')
    p_url = context.user_data.get('new_api_url')
    p_token = context.user_data.get('new_api_token')

    if p_name and p_url and p_token:
        pid = await run_db(save_api_panel_sync, p_name, p_url, p_token, interval_val)
        await update.message.reply_text(
            f"✅ **API Panel Connected Successfully!**\n\n"
            f"📌 Name: `{escape_md(p_name)}` \n"
            f"⏱️ Polling: `{interval_val}s`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Incomplete panel data. Please try again.")

    context.user_data.pop('new_api_name', None)
    context.user_data.pop('new_api_url', None)
    context.user_data.pop('new_api_token', None)

    text_msg, kbd = build_api_panels_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


@admin_only
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

    await run_db(set_setting, "channel", new_link)
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

    await run_db(set_setting, "support", new_link)
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

    await run_db(set_setting, "otp_group_link", new_link)
    await update.message.reply_text(f"✅ OTP Group link updated successfully!\nCurrent link: {escape_md(new_link)}", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END

@admin_only
async def set_otpgroupid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.reply_text("Enter the OTP Forward Group Chat ID (e.g., -1001234567890):")
    return WAIT_OTP_GROUP_ID

async def receive_otp_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_id = update.message.text.strip()
    await run_db(set_setting, "otp_group_id", new_id)
    await update.message.reply_text(f"✅ OTP Forward Group ID updated successfully!\nCurrent Group ID: `{escape_md(new_id)}`", parse_mode="Markdown")
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
    context.user_data.pop('new_api_name', None)
    context.user_data.pop('new_api_url', None)
    context.user_data.pop('new_api_token', None)
    context.user_data.pop('new_admin_id', None)
    context.user_data.pop('w_method', None)
    context.user_data.pop('w_wallet', None)
    context.user_data.pop('reject_req_id', None)

    if update.message and update.message.text:
        await handle_text_menu(update, context)

    return ConversationHandler.END


# ---------------- INLINE CALLBACK HANDLER ----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    first_name = query.from_user.first_name or ""
    user_is_admin = is_admin_sync(user_id)
    await run_db(save_user, user_id, first_name)

    if data == "noop":
        await query.answer()
        return

    if data == "back_to_services":
        await query.answer()
        kbd, msg = get_services_keyboard()
        if not kbd:
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text(msg, reply_markup=kbd)

    elif data == "usr:withdraw":
        withdraw_on = get_setting("withdraw_enabled", "true") == "true"
        if not withdraw_on:
            await query.answer("❌ Withdraw feature is currently disabled by Admin.", show_alert=True)
            return

        bal = await run_db(get_user_balance_sync, user_id)
        min_w = float(get_setting("min_withdraw_amount", "50"))

        if bal < min_w:
            await query.answer(f"❌ আপনার ব্যালেন্স পর্যাপ্ত নয়! মিনিমাম উইথড্র: {fmt_num(min_w)} ৳। আপনার ব্যালেন্স: {fmt_num(bal)} ৳।", show_alert=True)
            return

        await query.answer()
        methods = await run_db(get_withdraw_methods_sync)
        if not methods:
            await query.edit_message_text("❌ No withdraw methods are currently available. Please try again later.")
            return

        buttons = []
        for m in methods:
            buttons.append([create_button(m, callback_data=f"usr:w_method:{m}", style="primary")])

        await query.edit_message_text("💳 **SELECT WITHDRAW METHOD:**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data == "adm:w_settings":
        await query.answer()
        if not user_is_admin:
            return
        text_msg, kbd = build_withdraw_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:r_bonus_settings":
        await query.answer()
        if not user_is_admin:
            return
        text_msg, kbd = build_ranking_bonus_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:extra_back":
        await query.answer()
        if not user_is_admin:
            return
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:panel_back":
        await query.answer()
        if not user_is_admin:
            return
        total_users = len(await run_db(get_all_users))
        await query.message.reply_text(
            f"**ADMIN PANEL**\n\n"
            f"⚙️ DB Mode: **{CURRENT_DB_MODE}**\n"
            f"👥 Total Registered Users: `{total_users}`",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "adm:w_del_m_list":
        await query.answer()
        if not user_is_admin:
            return
        methods = await run_db(get_withdraw_methods_sync)
        buttons = []
        for m in methods:
            buttons.append([create_button(f"❌ Delete {m}", callback_data=f"adm:w_del_m:{m}", style="danger")])
        buttons.append([create_button("Back", callback_data="adm:w_settings", style="primary")])
        await query.edit_message_text("Select payment method to remove:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm:w_del_m:"):
        if not user_is_admin:
            await query.answer()
            return
        m_name = data.split(":", 2)[2]
        await run_db(delete_withdraw_method_sync, m_name)
        await query.answer(f"Method {m_name} removed!", show_alert=True)
        text_msg, kbd = build_withdraw_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:w_page:"):
        await query.answer()
        if not user_is_admin:
            return
        page = int(data.split(":", 2)[2])
        text_msg, kbd = build_admin_withdraw_requests_view(page)
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:w_view:"):
        await query.answer()
        if not user_is_admin:
            return
        req_id = int(data.split(":", 2)[2])
        text_msg, kbd = build_withdraw_detail_view(req_id)
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:w_app:"):
        if not user_is_admin:
            await query.answer()
            return
        req_id = int(data.split(":", 2)[2])
        req = await run_db(get_withdraw_request_by_id_sync, req_id)
        if req and req["status"] == "pending":
            await run_db(update_withdraw_status_sync, req_id, "approved", "")
            await query.answer("Withdraw Request Approved!", show_alert=True)

            user_msg = (
                "✅ <b>WITHDRAWAL APPROVED</b>\n\n"
                f"Your withdrawal request of <b>{fmt_num(req['amount'])} ৳</b> via <b>{html.escape(req['method'])}</b> has been approved!\n"
                f"Account / Wallet: <code>{html.escape(req['wallet_number'])}</code>"
            )
            try:
                await context.bot.send_message(chat_id=req["user_id"], text=user_msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to send approval notification: {e}")

            text_msg, kbd = build_withdraw_detail_view(req_id)
            await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
        else:
            await query.answer("Request already processed or invalid!", show_alert=True)

    # Admin Control Inline Controls
    elif data == "adm:ctrl:list":
        await query.answer()
        if not user_is_admin:
            return
        text_msg, kbd = build_admin_control_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:delconf:"):
        await query.answer()
        if not user_is_admin:
            return
        target_uid = int(data.split(":", 2)[2])
        if target_uid == ADMIN_ID:
            await query.answer("❌ Main Owner cannot be removed!", show_alert=True)
            return

        admins = get_all_admins_sync()
        target_adm = next((a for a in admins if a["user_id"] == target_uid), None)
        target_name = target_adm["name"] if target_adm else "Admin"

        text = (
            f"⚠️ **CONFIRMATION REQUIRED**\n\n"
            f"Are you sure you want to remove admin **{escape_md(target_name)}** (`{target_uid}`)?\n"
            f"This action cannot be undone."
        )
        kbd = InlineKeyboardMarkup([
            [
                create_button("✅ YES, REMOVE", callback_data=f"adm:del:{target_uid}", style="danger"),
                create_button("❌ CANCEL", callback_data="adm:ctrl:list", style="primary")
            ]
        ])
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:del:"):
        if not user_is_admin:
            await query.answer()
            return
        target_uid = int(data.split(":", 2)[2])
        if target_uid == ADMIN_ID:
            await query.answer("❌ Main Owner cannot be removed!", show_alert=True)
            return

        success = await run_db(delete_admin_sync, target_uid)
        if success:
            await query.answer("Admin removed successfully!", show_alert=True)
        else:
            await query.answer("Failed to remove admin.", show_alert=True)

        text_msg, kbd = build_admin_control_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:setqty:"):
        await query.answer()
        if not user_is_admin:
            return
        qty_val = data.split(":", 2)[2]
        await run_db(set_setting, "number_quantity", qty_val)
        await query.answer(f"Number quantity set to {qty_val}!", show_alert=True)
        text_msg, kbd = build_number_quantity_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:show_msg":
        await query.answer()
        if not user_is_admin:
            return
        curr_val = get_setting("show_message", "true")
        new_val = "false" if curr_val == "true" else "true"
        await run_db(set_setting, "show_message", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Show Message option is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:country_count":
        await query.answer()
        if not user_is_admin:
            return
        curr_val = get_setting("show_country_count", "false")
        new_val = "false" if curr_val == "true" else "true"
        await run_db(set_setting, "show_country_count", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Country number count is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:show_dev":
        await query.answer()
        if not user_is_admin:
            return
        curr_val = get_setting("show_developer", "true")
        new_val = "false" if curr_val == "true" else "true"
        await run_db(set_setting, "show_developer", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Developer Info is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:withdraw":
        await query.answer()
        if not user_is_admin:
            return
        curr_val = get_setting("withdraw_enabled", "true")
        new_val = "false" if curr_val == "true" else "true"
        await run_db(set_setting, "withdraw_enabled", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Withdraw system is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:toggle:ranking_bonus":
        await query.answer()
        if not user_is_admin:
            return
        curr_val = get_setting("ranking_bonus_enabled", "true")
        new_val = "false" if curr_val == "true" else "true"
        await run_db(set_setting, "ranking_bonus_enabled", new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"Ranking Bonus System is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:srv:list":
        await query.answer()
        if not user_is_admin:
            return
        text, kbd = build_admin_services_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:srv:view:"):
        await query.answer()
        if not user_is_admin:
            return
        service = data.split(":", 3)[3]
        text, kbd = build_service_manage_view(service)
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:srv:del:"):
        if not user_is_admin:
            await query.answer()
            return
        service = data.split(":", 3)[3]
        await run_db(delete_service_db, service)
        await query.answer(f"Service {service} deleted successfully!", show_alert=True)
        text, kbd = build_admin_services_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:cnt:delli:"):
        await query.answer()
        if not user_is_admin:
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
        if not user_is_admin:
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

    # API Panel Inline Controls
    elif data == "adm:api:list":
        await query.answer()
        if not user_is_admin:
            return
        text, kbd = build_api_panels_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:api:view:"):
        await query.answer()
        if not user_is_admin:
            return
        panel_id = data.split(":", 3)[3]
        text, kbd = build_panel_manage_view(panel_id)
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:api:delconf:"):
        await query.answer()
        if not user_is_admin:
            return
        panel_id = data.split(":", 3)[3]
        p = await run_db(get_api_panel_sync, panel_id)
        if p:
            text = f"⚠️ **ARE YOU SURE?**\n\nDo you really want to delete the panel **'{escape_md(p['name'])}'**?"
            kbd = InlineKeyboardMarkup([
                [
                    create_button("✅ YES, DELETE", callback_data=f"adm:api:del:{panel_id}", style="danger"),
                    create_button("❌ CANCEL", callback_data=f"adm:api:view:{panel_id}", style="primary")
                ]
            ])
            await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:api:del:"):
        if not user_is_admin:
            await query.answer()
            return
        panel_id = data.split(":", 3)[3]
        await run_db(delete_api_panel_sync, panel_id)
        await query.answer("Panel deleted successfully!", show_alert=True)
        text, kbd = build_api_panels_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

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
        await query.edit_message_text(f"📍 Select country for {escape_md(service)}:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

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
            await query.answer("Successfully changed all numbers!", show_alert=False)
            alloc_msg = (
                "━━━━━━━━━━━━━━━\n"
                f"{escape_md(service)} ➜ {escape_md(country)}'s Numbers Allocated:"
            )
            kbd = build_allocation_keyboard(service, country, new_numbers)
            await query.edit_message_text(alloc_msg, reply_markup=kbd, parse_mode="Markdown")
        else:
            await query.answer(f"Sorry, not enough ({target_qty}) new numbers available to change!", show_alert=True)


# ---------------- OTP POLLING SERVICE & MULTI-API MANAGER ----------------
def load_seen_otp_ids_sync() -> dict:
    """Loads the union of seen-OTP ids from both stores. Reading both (instead of only
    whichever CURRENT_DB_MODE happens to be at boot) means a restart can never make an
    already-forwarded OTP look 'new' just because Firebase hadn't reconnected yet."""
    result = {}
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT msg_id FROM seen_otps")
        result = {row[0]: True for row in cursor.fetchall()}
    except Exception as e:
        logging.error(f"SQLite load seen otps error: {e}")
    finally:
        if conn:
            conn.close()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            seen_ref = db.reference("seen_otp_ids").get()
            if seen_ref and isinstance(seen_ref, dict):
                result.update({k: True for k in seen_ref.keys()})
        except Exception as e:
            logging.error(f"Firebase load seen otps error: {e}")
    return result


def mark_otp_seen_sync(msg_id: str):
    ts = int(time.time())
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO seen_otps (msg_id, ts) VALUES (?, ?)", (msg_id, ts))
        conn.commit()
    except Exception as e:
        logging.error(f"SQLite mark otp seen error: {e}")
    finally:
        if conn:
            conn.close()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            db.reference(f"seen_otp_ids/{msg_id}").set(ts)
        except Exception as e:
            logging.error(f"Firebase mark otp seen error: {e}")


def cleanup_old_otp_ids_sync():
    cutoff = int(time.time()) - OTP_ID_RETENTION_SECONDS
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM seen_otps WHERE ts > 0 AND ts < ?", (cutoff,))
        conn.commit()
    except Exception as e:
        logging.error(f"SQLite cleanup seen otps error: {e}")
    finally:
        if conn:
            conn.close()

    if CURRENT_DB_MODE == "Firebase (Cloud)":
        try:
            seen_ref = db.reference("seen_otp_ids").get()
            if seen_ref and isinstance(seen_ref, dict):
                for msg_id, ts in seen_ref.items():
                    if isinstance(ts, (int, float)) and ts < cutoff:
                        db.reference(f"seen_otp_ids/{msg_id}").delete()
        except Exception as e:
            logging.error(f"Firebase cleanup seen otps error: {e}")


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


async def process_otp_items(items: list, application: Application, processed_ids: dict):
    bot_info = await application.bot.get_me()
    bot_username = bot_info.username or ""
    bot_link = f"https://t.me/{bot_username}" if bot_username else "https://t.me"

    ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))
    show_msg_enabled = get_setting("show_message", "true") == "true"
    show_dev_enabled = get_setting("show_developer", "true") == "true"
    
    dev_username = get_setting("dev_username", "developer")
    dev_link = clean_tg_link(get_setting("dev_link", "https://t.me/developer"))
    dev_html = f'<a href="{dev_link}">{html.escape(dev_username)}</a>'

    target_otp_group = get_setting("otp_group_id", OTP_GROUP_ID)

    for item in items:
        if not isinstance(item, dict):
            continue

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
        cli = item.get("cli", "").strip()

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

        display_service = cli if cli else (service_name if service_name else "Service")

        otp_code = extract_otp(msg)

        safe_msg = html.escape(msg)
        safe_service = html.escape(display_service)
        safe_num = html.escape(num)

        masked_num = mask_number_aph(num)
        safe_masked_num = html.escape(masked_num)

        dev_footer = f"\n━━━━━━━━━━━━━━━━━\n🖥️ Dᴇᴠᴇʟᴏᴘᴇʀ {dev_html}" if show_dev_enabled else ""

        if target_otp_group:
            if show_msg_enabled:
                group_text = (
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>SERVICE</b>:  {safe_service}\n"
                    f"🌐 NUM: {safe_masked_num}\n\n"
                    "🗨️ MESSAGE:\n"
                    f"<blockquote expandable>{safe_msg}</blockquote>"
                    f"{dev_footer}"
                )
            else:
                group_text = (
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>SERVICE</b>:  {safe_service}\n"
                    f"🌐 NUM: {safe_masked_num}"
                    f"{dev_footer}"
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
                    chat_id=target_otp_group,
                    text=group_text,
                    reply_markup=group_kbd,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            except Exception as e:
                logging.error(f"Group Forward Error: {e}")

        if allocated_user:
            prof = await run_db(add_user_balance_and_otp_sync, allocated_user, 1.0)
            new_bal = prof["balance"]
            bal_str = fmt_num(new_bal)

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


async def poll_single_panel(panel_id: str, application: Application, processed_ids: dict):
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            panel = await run_db(get_api_panel_sync, panel_id)
            if not panel:
                break

            url = panel["url"]
            token = panel["token"]
            interval = float(panel.get("polling_interval", 5.0))
            if interval < 3.1:
                interval = 3.1
            elif interval > 10.0:
                interval = 10.0

            try:
                params = {"records": 200}
                if token and "token=" not in url:
                    params["token"] = token

                res = await client.get(url, params=params if params else None)
                if res.status_code == 200:
                    res_data = res.json()
                    if res_data.get("status") == "success":
                        items = res_data.get("data", [])
                        if isinstance(items, list) and items:
                            await process_otp_items(items, application, processed_ids)
            except Exception as e:
                logging.error(f"Polling Exception for Panel {panel_id}: {e}")

            await asyncio.sleep(interval)


async def otp_poller_manager(application: Application):
    processed_ids = await run_db(load_seen_otp_ids_sync)
    cycle_count = 0

    while True:
        try:
            panels = await run_db(get_all_api_panels_sync)
            active_ids = {str(p["id"]) for p in panels}

            for pid in list(PANEL_TASKS.keys()):
                if pid not in active_ids or PANEL_TASKS[pid].done():
                    if not PANEL_TASKS[pid].done():
                        PANEL_TASKS[pid].cancel()
                    del PANEL_TASKS[pid]

            for p in panels:
                pid = str(p["id"])
                if pid not in PANEL_TASKS or PANEL_TASKS[pid].done():
                    PANEL_TASKS[pid] = asyncio.create_task(
                        poll_single_panel(pid, application, processed_ids)
                    )

            cycle_count += 1
            if cycle_count % 120 == 0:
                await run_db(cleanup_old_otp_ids_sync)

            await run_db(check_and_process_weekly_reset_sync, bot_app=application)

        except Exception as e:
            logging.error(f"OTP Poller Manager Error: {e}")

        await asyncio.sleep(5)


# ---------------- MAIN FUNCTION ----------------
def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_add_service_start, pattern="^adm:srv:add$"),
            CallbackQueryHandler(admin_add_service_with_name, pattern="^adm:srv:add:"),
            CallbackQueryHandler(admin_add_panel_start, pattern="^adm:api:add$"),
            CallbackQueryHandler(admin_add_admin_start, pattern="^adm:add:start$"),
            CallbackQueryHandler(set_channel_start, pattern="^adm:set:channel$"),
            CallbackQueryHandler(set_support_start, pattern="^adm:set:support$"),
            CallbackQueryHandler(set_otplink_start, pattern="^adm:set:otplink$"),
            CallbackQueryHandler(set_otpgroupid_start, pattern="^adm:set:otpgroupid$"),
            CallbackQueryHandler(set_dev_name_start, pattern="^adm:set:devname$"),
            CallbackQueryHandler(set_dev_link_start, pattern="^adm:set:devlink$"),
            CallbackQueryHandler(user_start_withdraw_flow, pattern="^usr:w_method:"),
            CallbackQueryHandler(admin_add_w_method_start, pattern="^adm:w_add_m$"),
            CallbackQueryHandler(admin_set_min_w_start, pattern="^adm:w_set_min$"),
            CallbackQueryHandler(admin_reject_w_start, pattern="^adm:w_rej_start:"),
            CallbackQueryHandler(set_rank1_bonus_start, pattern="^adm:r_set_b1$"),
            CallbackQueryHandler(set_rank2_bonus_start, pattern="^adm:r_set_b2$"),
            CallbackQueryHandler(set_rank3_bonus_start, pattern="^adm:r_set_b3$"),
            MessageHandler(filters.Regex("(?i)^Broadcast$"), broadcast_start),
        ],
        states={
            ADD_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_service_name)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_country_name)],
            ADD_NUMBERS: [MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND & ~MENU_FILTER, receive_numbers)],
            WAIT_PANEL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_panel_name)],
            WAIT_PANEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_panel_url)],
            WAIT_PANEL_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_panel_token)],
            WAIT_PANEL_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_panel_interval)],
            WAIT_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_admin_id)],
            WAIT_ADMIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_admin_name)],
            WAIT_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_channel_link)],
            WAIT_SUPPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_support_link)],
            WAIT_OTP_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_otp_link)],
            WAIT_OTP_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_otp_group_id)],
            WAIT_BROADCAST_MSG: [MessageHandler(~filters.COMMAND & ~MENU_FILTER, receive_broadcast_msg)],
            WAIT_WITHDRAW_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_withdraw_wallet)],
            WAIT_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_withdraw_amount)],
            WAIT_NEW_WITHDRAW_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_new_withdraw_method)],
            WAIT_MIN_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_min_withdraw_amount)],
            WAIT_REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_reject_reason)],
            WAIT_DEV_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_dev_name)],
            WAIT_DEV_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_dev_link)],
            WAIT_RANK1_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_rank1_bonus)],
            WAIT_RANK2_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_rank2_bonus)],
            WAIT_RANK3_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_rank3_bonus)],
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
        asyncio.create_task(otp_poller_manager(app))

    application.post_init = post_init
    application.run_polling()

if __name__ == "__main__":
    main()
