import asyncio
import logging
import os
import re
import json
import base64
import hashlib
import html
import time
import datetime
import threading
from collections import OrderedDict
from functools import wraps
from typing import Optional, List, Dict, Any, Tuple

import httpx
from dotenv import load_dotenv
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions as SupabaseClientOptions

try:
    import firebase_admin
    from firebase_admin import credentials, db as firebase_db
    HAS_FIREBASE_LIB = True
except ImportError:
    HAS_FIREBASE_LIB = False

from flask import Flask, jsonify
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

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

FIREBASE_B64 = os.environ.get("FIREBASE_BASE64")
FIREBASE_JSON_ENV = os.environ.get("FIREBASE_CONFIG_JSON")

CURRENT_DB_MODE = "Supabase+Firebase"

# 6 hours retention (was 24h — cuts seen_otps DB size ~75%)
OTP_ID_RETENTION_SECONDS = 6 * 60 * 60
CLEANUP_EVERY_N_CYCLES = 720

DEFAULT_PAYOUT = 0.5
BROADCAST_CONCURRENCY = 20
BROADCAST_PER_MSG_DELAY = 0.05

# Firebase backup cadence — 10 min per 500 users max (quota-safe)
FIREBASE_BACKUP_INTERVAL = 600
FIREBASE_BACKUP_BATCH_SIZE = 500
# Weekly reset check every hour
WEEKLY_RESET_CHECK_INTERVAL = 3600
# Leaderboard in-memory TTL
LEADERBOARD_CACHE_TTL = 60
# Known users LRU bound
MAX_KNOWN_USERS = 20000
# Processed OTP IDs LRU bound (avoid RAM pressure)
MAX_PROCESSED_IN_MEMORY = 5000

# Supabase pagination page size (PostgREST hard cap = 1000)
SUPABASE_PAGE_SIZE = 1000

# Batch RPC availability flag (auto-disabled on first failure)
_BATCH_RPC_AVAILABLE = True

# ---------------- IN-MEMORY GLOBAL CACHE ----------------
SETTINGS_CACHE = {}
SERVICES_CACHE = {}   # {service_name: {country_name: available_count}}
PAYOUTS_CACHE = {}    # {service_name: {country_name: payout_amount}}
ADMINS_CACHE = set()
PANEL_TASKS = {}


class LRUSet:
    """Bounded LRU set — keeps RAM bounded on Render free tier."""
    def __init__(self, maxsize: int = 20000):
        self.maxsize = maxsize
        self._data: OrderedDict = OrderedDict()

    def __contains__(self, key):
        return key in self._data

    def add(self, key):
        if key in self._data:
            self._data.move_to_end(key)
        else:
            self._data[key] = True
            if len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)


KNOWN_USERS = LRUSet(maxsize=MAX_KNOWN_USERS)
PROCESSED_OTP_IDS_CACHE: OrderedDict = OrderedDict()
LEADERBOARD_CACHE = {"data": None, "ts": 0}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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


def fmt_num(val) -> str:
    if val is None:
        return "0"
    try:
        val = float(val)
    except (TypeError, ValueError):
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


async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


# ============================================================
# PAGED SUPABASE FETCH — bypasses PostgREST 1000-row default limit
# ============================================================
def fetch_all_rows(table: str, columns: str,
                   filters: dict = None,
                   order_col: str = None, order_desc: bool = False,
                   in_filter: tuple = None,
                   chunk_size: int = SUPABASE_PAGE_SIZE) -> list:
    """
    Fetch every row from Supabase by paginating past the default 1000 limit.

    - filters: {"service": "X", "status": "available"}  (equality only)
    - in_filter: ("number", [list])  → chunked .in_()
    - order_col: column to order by (recommended for consistent pagination)
    """
    all_rows = []
    try:
        # Chunked .in_() path
        if in_filter:
            col, values = in_filter
            for i in range(0, len(values), chunk_size):
                chunk = values[i:i + chunk_size]
                offset = 0
                while True:
                    q = supabase.table(table).select(columns).in_(col, chunk)
                    if filters:
                        for k, v in filters.items():
                            q = q.eq(k, v)
                    if order_col:
                        q = q.order(order_col, desc=order_desc)
                    r = q.range(offset, offset + chunk_size - 1).execute()
                    if not r.data:
                        break
                    all_rows.extend(r.data)
                    if len(r.data) < chunk_size:
                        break
                    offset += chunk_size
            return all_rows

        # Normal paginated path
        offset = 0
        while True:
            q = supabase.table(table).select(columns)
            if filters:
                for k, v in filters.items():
                    q = q.eq(k, v)
            if order_col:
                q = q.order(order_col, desc=order_desc)
            r = q.range(offset, offset + chunk_size - 1).execute()
            if not r.data:
                break
            all_rows.extend(r.data)
            if len(r.data) < chunk_size:
                break
            offset += chunk_size
    except Exception as e:
        logger.error(f"fetch_all_rows({table}) error: {e}")
    return all_rows


# ============================================================
# SUPABASE CLIENT
# ============================================================
supabase: Optional[Client] = None


def init_supabase() -> bool:
    global supabase
    try:
        options = SupabaseClientOptions(
            auto_refresh_token=False,
            persist_session=False,
        )
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
        logger.info("Supabase client initialized")
        return True
    except Exception as e:
        logger.error(f"Supabase init error: {e}")
        return False


# ============================================================
# FIREBASE (BACKUP ONLY)
# ============================================================
firebase_db_ref = None


def init_firebase() -> bool:
    global firebase_db_ref
    if not HAS_FIREBASE_LIB:
        logger.info("Firebase Admin SDK not installed — backup disabled")
        return False
    if firebase_admin._apps:
        firebase_db_ref = firebase_db
        return True

    cred_dict = None
    try:
        if FIREBASE_B64:
            cred_dict = json.loads(base64.b64decode(FIREBASE_B64).decode('utf-8'))
        elif FIREBASE_JSON_ENV:
            cred_dict = json.loads(FIREBASE_JSON_ENV)
            if "private_key" in cred_dict:
                cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
        if cred_dict:
            cred = credentials.Certificate(cred_dict)
            options = {}
            if DATABASE_URL:
                options['databaseURL'] = DATABASE_URL
            firebase_admin.initialize_app(cred, options if options else None)
            firebase_db_ref = firebase_db
            logger.info("Firebase connected (backup mode)")
            return True
        else:
            logger.info("No Firebase credentials — backup disabled")
            return False
    except Exception as e:
        logger.error(f"Firebase Init Error: {e}")
        return False


# ============================================================
# PAYOUTS
# ============================================================
def get_payout_sync(service: str, country: str) -> float:
    if not service or not country:
        return DEFAULT_PAYOUT
    if service in PAYOUTS_CACHE and country in PAYOUTS_CACHE[service]:
        return float(PAYOUTS_CACHE[service][country])
    try:
        res = supabase.table("payouts").select("payout").eq("service_name", service).eq("country_name", country).execute()
        if res.data and res.data[0].get("payout") is not None:
            fval = float(res.data[0]["payout"])
            PAYOUTS_CACHE.setdefault(service, {})[country] = fval
            return fval
    except Exception as e:
        logger.error(f"Supabase get payout error: {e}")
    return DEFAULT_PAYOUT


def set_payout_sync(service: str, country: str, amount: float):
    if not service or not country:
        return
    try:
        amount = float(amount)
        if amount < 0:
            amount = 0.0
    except (TypeError, ValueError):
        amount = DEFAULT_PAYOUT

    PAYOUTS_CACHE.setdefault(service, {})[country] = amount

    try:
        supabase.table("payouts").upsert({
            "service_name": service, "country_name": country, "payout": amount
        }).execute()
    except Exception as e:
        logger.error(f"Supabase set payout error: {e}")

    if firebase_db_ref:
        try:
            firebase_db_ref.reference(f"payouts/{service}/{country}").set(amount)
        except Exception as e:
            logger.error(f"Firebase set payout error: {e}")


def refresh_payouts_cache_sync():
    global PAYOUTS_CACHE
    new_cache = {}
    try:
        rows = fetch_all_rows("payouts", "service_name, country_name, payout")
        for r in rows:
            new_cache.setdefault(r["service_name"], {})[r["country_name"]] = (
                float(r["payout"]) if r.get("payout") is not None else DEFAULT_PAYOUT
            )
    except Exception as e:
        logger.error(f"Supabase payouts cache load error: {e}")
    PAYOUTS_CACHE = new_cache


# ============================================================
# WITHDRAW METHODS
# ============================================================
def get_withdraw_methods_sync() -> list:
    try:
        rows = fetch_all_rows("withdraw_methods", "name")
        return [r["name"] for r in rows]
    except Exception as e:
        logger.error(f"Supabase get withdraw methods error: {e}")
        return []


def add_withdraw_method_sync(name: str):
    name = name.strip()
    if not name:
        return
    try:
        supabase.table("withdraw_methods").upsert({"name": name}).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"withdraw_methods/{name}").set(True)
    except Exception as e:
        logger.error(f"Supabase add withdraw method error: {e}")


def delete_withdraw_method_sync(name: str):
    try:
        supabase.table("withdraw_methods").delete().eq("name", name).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"withdraw_methods/{name}").delete()
    except Exception as e:
        logger.error(f"Supabase delete withdraw method error: {e}")


# ============================================================
# USERS
# ============================================================
def deduct_user_balance_sync(user_id: int, amount: float) -> bool:
    try:
        res = supabase.rpc("deduct_user_balance", {
            "p_user_id": user_id, "p_amount": amount,
        }).execute()
        return bool(res.data)
    except Exception as e:
        logger.error(f"Supabase deduct balance RPC error: {e}")
        prof = get_user_profile_sync(user_id)
        if prof["balance"] < amount:
            return False
        try:
            supabase.table("users").update({
                "balance": prof["balance"] - amount, "dirty": True,
            }).eq("user_id", user_id).execute()
            return True
        except Exception:
            return False


def refund_user_balance_sync(user_id: int, amount: float):
    prof = get_user_profile_sync(user_id)
    try:
        supabase.table("users").update({
            "balance": prof["balance"] + amount, "dirty": True,
        }).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"Supabase refund balance error: {e}")


def create_withdraw_request_sync(user_id: int, method: str, wallet_number: str, amount: float) -> int:
    ts = int(time.time())
    try:
        res = supabase.table("withdraw_requests").insert({
            "user_id": user_id, "method": method, "wallet_number": wallet_number,
            "amount": amount, "status": "pending", "reject_reason": "", "created_at": ts,
        }).execute()
        req_id = res.data[0]["id"]
        if firebase_db_ref:
            firebase_db_ref.reference(f"withdraw_requests/{req_id}").set({
                "id": req_id, "user_id": user_id, "method": method,
                "wallet_number": wallet_number, "amount": amount,
                "status": "pending", "reject_reason": "", "created_at": ts,
            })
        return req_id
    except Exception as e:
        logger.error(f"Supabase create withdraw req error: {e}")
        return 0


def get_all_withdraw_requests_sync() -> list:
    try:
        rows = fetch_all_rows(
            "withdraw_requests",
            "id, user_id, method, wallet_number, amount, status, reject_reason, created_at",
            order_col="id", order_desc=False,
        )
        return [{
            "id": r["id"], "user_id": r["user_id"], "method": r["method"],
            "wallet_number": r["wallet_number"], "amount": r["amount"],
            "status": r["status"], "reject_reason": r.get("reject_reason", ""),
            "created_at": r.get("created_at", 0),
        } for r in rows]
    except Exception as e:
        logger.error(f"Supabase fetch withdraw reqs error: {e}")
        return []


def get_withdraw_request_by_id_sync(req_id: int):
    try:
        res = supabase.table("withdraw_requests").select("*").eq("id", req_id).execute()
        if res.data:
            r = res.data[0]
            return {
                "id": r["id"], "user_id": r["user_id"], "method": r["method"],
                "wallet_number": r["wallet_number"], "amount": r["amount"],
                "status": r["status"], "reject_reason": r.get("reject_reason", ""),
                "created_at": r.get("created_at", 0),
            }
    except Exception as e:
        logger.error(f"Supabase fetch single withdraw req error: {e}")
    return None


def atomic_transition_withdraw_status_sync(req_id: int, from_status: str, to_status: str, reject_reason: str = "") -> bool:
    try:
        res = supabase.rpc("transition_withdraw", {
            "p_req_id": req_id, "p_from_status": from_status,
            "p_to_status": to_status, "p_reject_reason": reject_reason,
        }).execute()
        ok = bool(res.data)
        if ok and firebase_db_ref:
            try:
                firebase_db_ref.reference(f"withdraw_requests/{req_id}").update({
                    "status": to_status, "reject_reason": reject_reason,
                })
            except Exception:
                pass
        return ok
    except Exception as e:
        logger.error(f"Supabase atomic withdraw transition error: {e}")
        return False


def update_withdraw_status_sync(req_id: int, status: str, reject_reason: str = ""):
    try:
        supabase.table("withdraw_requests").update({
            "status": status, "reject_reason": reject_reason,
        }).eq("id", req_id).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"withdraw_requests/{req_id}").update({
                "status": status, "reject_reason": reject_reason,
            })
    except Exception as e:
        logger.error(f"Supabase update withdraw status error: {e}")


# ============================================================
# ADMINS
# ============================================================
def is_admin_sync(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    return user_id in ADMINS_CACHE


def get_all_admins_sync() -> list:
    admins = {}
    if ADMIN_ID:
        admins[ADMIN_ID] = {"user_id": ADMIN_ID, "name": "Main Owner", "is_owner": True}
    try:
        rows = fetch_all_rows("admins", "user_id, name")
        for r in rows:
            uid_int = int(r["user_id"])
            name = str(r["name"])
            if uid_int == ADMIN_ID:
                admins[uid_int]["name"] = f"{name} (Owner)"
            else:
                admins[uid_int] = {"user_id": uid_int, "name": name, "is_owner": False}
    except Exception as e:
        logger.error(f"Supabase get admins error: {e}")
    return list(admins.values())


def add_admin_sync(user_id: int, name: str):
    ADMINS_CACHE.add(user_id)
    try:
        supabase.table("admins").upsert({"user_id": user_id, "name": name}).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"admins/{user_id}").set({"name": name})
    except Exception as e:
        logger.error(f"Supabase add admin error: {e}")


def delete_admin_sync(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    if user_id in ADMINS_CACHE:
        ADMINS_CACHE.remove(user_id)
    try:
        supabase.table("admins").delete().eq("user_id", user_id).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"admins/{user_id}").delete()
        return True
    except Exception as e:
        logger.error(f"Supabase delete admin error: {e}")
        return False


# ============================================================
# API PANELS
# ============================================================
def get_all_api_panels_sync() -> list:
    try:
        rows = fetch_all_rows("api_panels", "id, name, url, token, polling_interval")
        return [{
            "id": str(r["id"]), "name": str(r["name"]), "url": str(r["url"]),
            "token": str(r["token"]),
            "polling_interval": float(r["polling_interval"]) if r.get("polling_interval") else 5.0,
        } for r in rows]
    except Exception as e:
        logger.error(f"Supabase get API panels error: {e}")
        return []


def get_api_panel_sync(panel_id: str):
    try:
        res = supabase.table("api_panels").select("id, name, url, token, polling_interval").eq("id", int(panel_id)).execute()
        if res.data:
            r = res.data[0]
            return {
                "id": str(r["id"]), "name": str(r["name"]), "url": str(r["url"]),
                "token": str(r["token"]),
                "polling_interval": float(r["polling_interval"]) if r.get("polling_interval") else 5.0,
            }
    except Exception as e:
        logger.error(f"Supabase get single panel error: {e}")
    return None


def save_api_panel_sync(name: str, url: str, token: str, polling_interval: float = 5.0) -> str:
    try:
        res = supabase.table("api_panels").insert({
            "name": name, "url": url, "token": token, "polling_interval": polling_interval,
        }).execute()
        pid = str(res.data[0]["id"])
        if firebase_db_ref:
            firebase_db_ref.reference(f"api_panels/{pid}").set({
                "id": pid, "name": name, "url": url, "token": token, "polling_interval": polling_interval,
            })
        return pid
    except Exception as e:
        logger.error(f"Supabase save API panel error: {e}")
        return ""


def delete_api_panel_sync(panel_id: str):
    try:
        supabase.table("api_panels").delete().eq("id", int(panel_id)).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"api_panels/{panel_id}").delete()
    except Exception as e:
        logger.error(f"Supabase delete API panel error: {e}")


# ============================================================
# SETTINGS
# ============================================================
def get_setting(key: str, default_val: str = "") -> str:
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]
    return default_val


def set_setting(key: str, value: str):
    str_val = str(value)
    SETTINGS_CACHE[key] = str_val
    try:
        supabase.table("settings").upsert({"key": key, "value": str_val}).execute()
        if firebase_db_ref:
            firebase_db_ref.reference(f"settings/{key}").set(str_val)
    except Exception as e:
        logger.error(f"Supabase set setting error: {e}")


# ============================================================
# USERS
# ============================================================
def save_user(user_id: int, first_name: str = ""):
    """Hot path — only writes on first sighting (tracked in LRU)."""
    if user_id in KNOWN_USERS:
        return
    cur_date = get_bd_date_str()
    first_name = first_name.strip() if first_name else ""
    try:
        existing = supabase.table("users").select("user_id").eq("user_id", user_id).limit(1).execute()
        if not existing.data:
            supabase.table("users").insert({
                "user_id": user_id, "balance": 0.0, "today_earned": 0.0,
                "total_earned": 0.0, "refer_earned": 0.0, "total_otps": 0,
                "weekly_otps": 0, "first_name": first_name,
                "last_earn_date": cur_date, "dirty": True,
            }).execute()
        KNOWN_USERS.add(user_id)
    except Exception as e:
        logger.error(f"Supabase save user error: {e}")


def get_user_profile_sync(user_id: int) -> dict:
    current_date = get_bd_date_str()
    try:
        res = supabase.table("users").select(
            "balance, today_earned, total_earned, refer_earned, total_otps, last_earn_date"
        ).eq("user_id", user_id).limit(1).execute()
        if res.data:
            u = res.data[0]
            bal = float(u.get("balance") or 0.0)
            today_e = float(u.get("today_earned") or 0.0)
            total_e = float(u.get("total_earned") or 0.0)
            refer_e = float(u.get("refer_earned") or 0.0)
            otps = int(u.get("total_otps") or 0)
            last_date = str(u.get("last_earn_date") or "")
            if last_date != current_date:
                today_e = 0.0
                try:
                    supabase.table("users").update({
                        "today_earned": 0.0, "last_earn_date": current_date,
                    }).eq("user_id", user_id).execute()
                except Exception:
                    pass
            return {
                "balance": bal, "today_earned": today_e, "total_earned": total_e,
                "refer_earned": refer_e, "total_otps": otps,
            }
    except Exception as e:
        logger.error(f"Supabase profile fetch error: {e}")
    return {"balance": 0.0, "today_earned": 0.0, "total_earned": 0.0, "refer_earned": 0.0, "total_otps": 0}


def add_user_balance_and_otp_sync(user_id: int, amount: float = 1.0, count: int = 1) -> dict:
    """Per-user RPC — kept as fallback for batch RPC."""
    current_date = get_bd_date_str()
    try:
        res = supabase.rpc("add_otp_earnings", {
            "p_user_id": user_id,
            "p_amount": amount,
            "p_count": count,
            "p_date": current_date,
        }).execute()
        if res.data:
            row = res.data[0] if isinstance(res.data, list) else res.data
            return {
                "balance": float(row.get("new_balance", 0.0)),
                "total_otps": int(row.get("new_total_otps", 0)),
            }
    except Exception as e:
        logger.error(f"Supabase add_otp_earnings RPC error: {e}")

    # Fallback (non-atomic)
    prof = get_user_profile_sync(user_id)
    new_bal = prof["balance"] + amount
    try:
        supabase.table("users").update({
            "balance": new_bal,
            "total_earned": prof["total_earned"] + amount,
            "today_earned": prof["today_earned"] + amount,
            "total_otps": prof["total_otps"] + count,
            "last_earn_date": current_date,
            "dirty": True,
        }).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"Supabase fallback earnings error: {e}")
    return {"balance": new_bal, "total_otps": prof["total_otps"] + count}


def add_otp_earnings_batch_sync(updates: list) -> dict:
    """
    Batch-update multiple users' earnings in ONE Supabase RPC call.

    updates = [{"user_id": int, "amount": float, "count": int}, ...]
    Returns = {user_id: {"balance": float, "total_otps": int}}

    Requires SQL function 'add_otp_earnings_batch' (see migration).
    Auto-falls back to per-user RPC if the function is missing.
    """
    global _BATCH_RPC_AVAILABLE
    if not updates:
        return {}

    current_date = get_bd_date_str()

    # ---- Fast path: single batch RPC ----
    if _BATCH_RPC_AVAILABLE:
        try:
            res = supabase.rpc("add_otp_earnings_batch", {
                "p_updates": updates,
                "p_date": current_date,
            }).execute()
            if res.data:
                result = {}
                for row in res.data:
                    result[int(row["out_user_id"])] = {
                        "balance": float(row.get("out_balance", 0.0)),
                        "total_otps": int(row.get("out_total_otps", 0)),
                    }
                return result
        except Exception as e:
            err_str = str(e)
            if ("does not exist" in err_str
                    or "PGRST202" in err_str
                    or "Could not find" in err_str
                    or "not find the function" in err_str):
                _BATCH_RPC_AVAILABLE = False
                logger.warning(
                    "add_otp_earnings_batch RPC not found — falling back to per-user RPC. "
                    "Run the SQL migration to enable batch mode."
                )
            else:
                logger.error(f"Batch earnings RPC error: {e}")

    # ---- Fallback: per-user RPC ----
    result = {}
    for u in updates:
        try:
            r = add_user_balance_and_otp_sync(u["user_id"], u["amount"], u["count"])
        except Exception as e:
            logger.error(f"Fallback per-user earnings error for {u.get('user_id')}: {e}")
            r = {"balance": 0.0, "total_otps": 0}
        result[u["user_id"]] = r
    return result


def get_user_balance_sync(user_id: int) -> float:
    return get_user_profile_sync(user_id)["balance"]


def get_all_users() -> list:
    users = []
    try:
        rows = fetch_all_rows("users", "user_id")
        users = [int(r["user_id"]) for r in rows]
    except Exception as e:
        logger.error(f"Error fetching users from Supabase: {e}")
    return list(set(users))


# ============================================================
# WEEKLY RESET & LEADERBOARD
# ============================================================
def check_and_process_weekly_reset_sync() -> list:
    """Cheap early-return; all reads from Supabase."""
    current_friday = get_current_friday_str()
    last_reset = get_setting("last_weekly_reset_friday", "")
    if not last_reset:
        set_setting("last_weekly_reset_friday", current_friday)
        return []
    if last_reset == current_friday:
        return []

    is_bonus_enabled = get_setting("ranking_bonus_enabled", "true") == "true"
    top_users = []
    try:
        res = supabase.table("users").select("user_id, weekly_otps, total_otps").gt("weekly_otps", 0).order(
            "weekly_otps", desc=True).order("total_otps", desc=True).limit(3).execute()
        top_users = res.data
    except Exception as e:
        logger.error(f"Supabase fetch top ranking error: {e}")

    notifications = []
    if is_bonus_enabled and top_users:
        for rank_idx, u in enumerate(top_users, start=1):
            uid = u["user_id"]
            b_str = get_setting(f"rank_bonus_{rank_idx}", "0")
            try:
                b_amt = float(b_str)
            except (ValueError, TypeError):
                b_amt = 0.0
            if b_amt > 0:
                refund_user_balance_sync(uid, b_amt)
                notifications.append({"uid": uid, "amount": b_amt, "rank": rank_idx})

    try:
        supabase.table("users").update({"weekly_otps": 0, "dirty": True}).gt("weekly_otps", 0).execute()
    except Exception as e:
        logger.error(f"Supabase reset weekly otps error: {e}")

    set_setting("last_weekly_reset_friday", current_friday)
    return notifications


def get_ranking_leaderboard_sync(user_id: int) -> str:
    now = time.time()
    top_5 = None
    if LEADERBOARD_CACHE["data"] is not None and now - LEADERBOARD_CACHE["ts"] < LEADERBOARD_CACHE_TTL:
        top_5 = LEADERBOARD_CACHE["data"]
    else:
        try:
            res = supabase.table("users").select("user_id, first_name, weekly_otps, total_otps").gt("weekly_otps", 0).order(
                "weekly_otps", desc=True).order("total_otps", desc=True).limit(5).execute()
            top_5 = res.data
            LEADERBOARD_CACHE["data"] = top_5
            LEADERBOARD_CACHE["ts"] = now
        except Exception as e:
            logger.error(f"Supabase leaderboard fetch error: {e}")
            top_5 = []

    user_rank = "N/A"
    user_weekly_otps = 0
    try:
        res = supabase.table("users").select("weekly_otps").eq("user_id", user_id).limit(1).execute()
        if res.data:
            user_weekly_otps = int(res.data[0].get("weekly_otps") or 0)
        if user_weekly_otps > 0:
            hr = supabase.table("users").select("user_id", count="exact").gt("weekly_otps", user_weekly_otps).execute()
            higher_cnt = hr.count if hr.count else 0
            user_rank = f"#{higher_cnt + 1}"
    except Exception as e:
        logger.error(f"Supabase user rank error: {e}")

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text = "🏆 <b>WEEKLY TOP OTP RECEIVERS</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    if top_5:
        for idx, u in enumerate(top_5):
            uid = u["user_id"]
            fname = u.get("first_name") or f"User {uid}"
            w_otps = u.get("weekly_otps", 0)
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            safe_fname = html.escape(fname)
            user_link = f'<a href="tg://user?id={uid}">{safe_fname}</a>'
            text += f"{medal} {user_link} — <b>{w_otps}</b> OTPs\n"
    else:
        text += "<i>No OTP receivers this week yet. Be the first!</i>\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    text += f"👤 <b>Your Rank:</b> {user_rank} ({user_weekly_otps} OTPs)"
    return text


# ============================================================
# FIREBASE BACKUP WORKER
# ============================================================
def push_dirty_to_firebase_sync() -> int:
    """Push only dirty user rows using Firebase multi-path update (1 write per batch)."""
    if not firebase_db_ref:
        return 0
    total_pushed = 0
    try:
        while True:
            res = supabase.table("users").select(
                "user_id, balance, today_earned, total_earned, total_otps, weekly_otps, last_earn_date, first_name"
            ).eq("dirty", True).limit(FIREBASE_BACKUP_BATCH_SIZE).execute()
            rows = res.data
            if not rows:
                break
            updates = {}
            user_ids = []
            for u in rows:
                uid = u["user_id"]
                updates[f"users/{uid}/balance"] = float(u.get("balance") or 0.0)
                updates[f"users/{uid}/today_earned"] = float(u.get("today_earned") or 0.0)
                updates[f"users/{uid}/total_earned"] = float(u.get("total_earned") or 0.0)
                updates[f"users/{uid}/total_otps"] = int(u.get("total_otps") or 0)
                updates[f"users/{uid}/weekly_otps"] = int(u.get("weekly_otps") or 0)
                updates[f"users/{uid}/last_earn_date"] = str(u.get("last_earn_date") or "")
                updates[f"users/{uid}/exists"] = True
                fn = u.get("first_name") or ""
                if fn:
                    updates[f"users/{uid}/first_name"] = fn
                user_ids.append(uid)
            if updates:
                firebase_db_ref.reference("/").update(updates)
                try:
                    supabase.table("users").update({"dirty": False}).in_("user_id", user_ids).execute()
                except Exception as e:
                    logger.error(f"clear dirty error: {e}")
                total_pushed += len(user_ids)
            if len(rows) < FIREBASE_BACKUP_BATCH_SIZE:
                break
        if total_pushed:
            logger.info(f"Firebase backup: pushed {total_pushed} dirty users")
    except Exception as e:
        logger.error(f"push_dirty_to_firebase_sync error: {e}")
    return total_pushed


async def firebase_backup_worker():
    await asyncio.sleep(30)
    try:
        await asyncio.to_thread(push_dirty_to_firebase_sync)
    except Exception as e:
        logger.error(f"Initial Firebase backup error: {e}")

    while True:
        await asyncio.sleep(FIREBASE_BACKUP_INTERVAL)
        try:
            await asyncio.to_thread(push_dirty_to_firebase_sync)
        except Exception as e:
            logger.error(f"Firebase backup worker error: {e}")


# ============================================================
# RESTORE FROM FIREBASE (only if Supabase is empty)
# ============================================================
def restore_from_firebase_to_supabase():
    if not firebase_db_ref:
        return
    try:
        res = supabase.table("users").select("user_id", count="exact").limit(1).execute()
        if res.count and res.count > 0:
            logger.info("Supabase has data — skipping Firebase restore")
            return
        logger.info("Supabase empty — restoring from Firebase...")

        # Users
        fb_users = firebase_db_ref.reference("users").get()
        if fb_users and isinstance(fb_users, dict):
            batch = []
            for uid, u in fb_users.items():
                if not str(uid).isdigit():
                    continue
                batch.append({
                    "user_id": int(uid),
                    "balance": float(u.get("balance") or 0.0),
                    "today_earned": float(u.get("today_earned") or 0.0),
                    "total_earned": float(u.get("total_earned") or 0.0),
                    "refer_earned": float(u.get("refer_earned") or 0.0),
                    "total_otps": int(u.get("total_otps") or 0),
                    "weekly_otps": int(u.get("weekly_otps") or 0),
                    "first_name": str(u.get("first_name") or ""),
                    "last_earn_date": str(u.get("last_earn_date") or ""),
                    "dirty": False,
                })
            for i in range(0, len(batch), 500):
                try:
                    supabase.table("users").upsert(batch[i:i+500]).execute()
                except Exception as e:
                    logger.error(f"restore users error: {e}")

        # Settings
        fb_settings = firebase_db_ref.reference("settings").get()
        if fb_settings and isinstance(fb_settings, dict):
            rows = [{"key": str(k), "value": str(v)} for k, v in fb_settings.items() if v is not None]
            if rows:
                try:
                    supabase.table("settings").upsert(rows).execute()
                except Exception as e:
                    logger.error(f"restore settings error: {e}")

        # Payouts
        fb_payouts = firebase_db_ref.reference("payouts").get()
        if fb_payouts and isinstance(fb_payouts, dict):
            rows = []
            for srv, cnts in fb_payouts.items():
                if isinstance(cnts, dict):
                    for cnt, val in cnts.items():
                        rows.append({"service_name": srv, "country_name": cnt, "payout": float(val)})
            if rows:
                try:
                    supabase.table("payouts").upsert(rows).execute()
                except Exception as e:
                    logger.error(f"restore payouts error: {e}")

        # Admins
        fb_admins = firebase_db_ref.reference("admins").get()
        if fb_admins and isinstance(fb_admins, dict):
            rows = []
            for uid, adata in fb_admins.items():
                if str(uid).isdigit():
                    name = adata.get("name", "Admin") if isinstance(adata, dict) else "Admin"
                    rows.append({"user_id": int(uid), "name": str(name)})
            if rows:
                try:
                    supabase.table("admins").upsert(rows).execute()
                except Exception as e:
                    logger.error(f"restore admins error: {e}")

        # API panels
        fb_panels = firebase_db_ref.reference("api_panels").get()
        if fb_panels and isinstance(fb_panels, dict):
            for pid, pdata in fb_panels.items():
                if isinstance(pdata, dict):
                    try:
                        supabase.table("api_panels").upsert({
                            "id": int(pdata.get("id", pid)),
                            "name": str(pdata.get("name", "")),
                            "url": str(pdata.get("url", "")),
                            "token": str(pdata.get("token", "")),
                            "polling_interval": float(pdata.get("polling_interval", 5.0)),
                        }).execute()
                    except Exception:
                        pass

        # Withdraw methods
        fb_methods = firebase_db_ref.reference("withdraw_methods").get()
        if fb_methods and isinstance(fb_methods, dict):
            rows = [{"name": str(m)} for m in fb_methods.keys()]
            if rows:
                try:
                    supabase.table("withdraw_methods").upsert(rows).execute()
                except Exception:
                    pass

        # Withdraw requests
        fb_wreqs = firebase_db_ref.reference("withdraw_requests").get()
        if fb_wreqs and isinstance(fb_wreqs, dict):
            for rid, rdata in fb_wreqs.items():
                if isinstance(rdata, dict):
                    try:
                        supabase.table("withdraw_requests").upsert({
                            "id": int(rdata.get("id", rid)),
                            "user_id": int(rdata.get("user_id", 0)),
                            "method": str(rdata.get("method", "")),
                            "wallet_number": str(rdata.get("wallet_number", "")),
                            "amount": float(rdata.get("amount", 0.0)),
                            "status": str(rdata.get("status", "pending")),
                            "reject_reason": str(rdata.get("reject_reason", "")),
                            "created_at": int(rdata.get("created_at", 0)),
                        }).execute()
                    except Exception:
                        pass

        # Services + numbers
        fb_services = firebase_db_ref.reference("services").get()
        if fb_services and isinstance(fb_services, dict):
            for srv, cnts in fb_services.items():
                if isinstance(cnts, dict):
                    for cnt in cnts.keys():
                        try:
                            supabase.table("services").upsert({
                                "service_name": srv, "country_name": cnt,
                            }).execute()
                        except Exception:
                            pass
                        nums = firebase_db_ref.reference(f"numbers/{srv}/{cnt}").get()
                        if nums and isinstance(nums, dict):
                            batch = []
                            for nk, nv in nums.items():
                                if isinstance(nv, dict):
                                    batch.append({
                                        "service": srv, "country": cnt,
                                        "number": str(nv.get("number", nk)),
                                        "status": str(nv.get("status", "available")),
                                        "user_id": int(nv.get("user_id", 0) or 0),
                                    })
                            for i in range(0, len(batch), 500):
                                try:
                                    supabase.table("numbers").upsert(batch[i:i+500]).execute()
                                except Exception:
                                    pass

        # Allocations
        fb_alloc = firebase_db_ref.reference("allocations").get()
        if fb_alloc and isinstance(fb_alloc, dict):
            batch = []
            for num, adata in fb_alloc.items():
                if isinstance(adata, dict):
                    batch.append({
                        "number": num,
                        "user_id": int(adata.get("user_id", 0) or 0),
                        "service": str(adata.get("service", "")),
                        "country": str(adata.get("country", "")),
                    })
            for i in range(0, len(batch), 500):
                try:
                    supabase.table("allocations").upsert(batch[i:i+500]).execute()
                except Exception:
                    pass

        logger.info("Firebase → Supabase restore complete")
    except Exception as e:
        logger.error(f"Restore from Firebase error: {e}")


# ============================================================
# CACHE MANAGEMENT
# ============================================================
def load_known_users():
    """Load a bounded subset of user IDs into LRU (RAM-safe)."""
    try:
        res = supabase.table("users").select("user_id").limit(MAX_KNOWN_USERS).execute()
        for r in res.data:
            KNOWN_USERS.add(int(r["user_id"]))
        logger.info(f"Loaded {len(KNOWN_USERS)} known users")
    except Exception as e:
        logger.error(f"load_known_users error: {e}")


def refresh_all_caches_sync():
    global SETTINGS_CACHE, ADMINS_CACHE
    SETTINGS_CACHE.clear()
    ADMINS_CACHE.clear()

    if ADMIN_ID:
        ADMINS_CACHE.add(ADMIN_ID)

    try:
        rows = fetch_all_rows("settings", "key, value")
        for r in rows:
            SETTINGS_CACHE[str(r["key"])] = str(r["value"])
    except Exception as e:
        logger.error(f"Error loading settings: {e}")

    try:
        rows = fetch_all_rows("admins", "user_id")
        for r in rows:
            ADMINS_CACHE.add(int(r["user_id"]))
    except Exception as e:
        logger.error(f"Error loading admins: {e}")

    refresh_services_cache_sync()
    refresh_payouts_cache_sync()


def refresh_services_cache_sync():
    """
    Rebuild SERVICES_CACHE from Supabase with proper pagination.
    Called at startup + admin ops only, NOT in hot path.
    """
    global SERVICES_CACHE
    new_cache = {}
    try:
        # 1) All service/country pairs — PAGED
        srv_rows = fetch_all_rows("services", "service_name, country_name")
        for r in srv_rows:
            new_cache.setdefault(r["service_name"], {})[r["country_name"]] = 0

        # 2) All allocated numbers — PAGED (was capped at 1000)
        allocated = set()
        try:
            alloc_rows = fetch_all_rows("allocations", "number")
            allocated = {str(a["number"]) for a in alloc_rows}
        except Exception as e:
            logger.error(f"allocations read error: {e}")

        # 3) Count available per service/country — PAGED
        for srv, cnts in new_cache.items():
            for cnt in list(cnts.keys()):
                try:
                    num_rows = fetch_all_rows(
                        "numbers", "number",
                        filters={"service": srv, "country": cnt, "status": "available"},
                    )
                    new_cache[srv][cnt] = sum(
                        1 for n in num_rows if str(n["number"]) not in allocated
                    )
                except Exception as e:
                    logger.error(f"count error for {srv}/{cnt}: {e}")
    except Exception as e:
        logger.error(f"Error populating services cache: {e}")
    SERVICES_CACHE = new_cache


def get_admin_services_summary():
    return SERVICES_CACHE


def delete_service_db(service: str):
    try:
        supabase.table("services").delete().eq("service_name", service).execute()
        supabase.table("numbers").delete().eq("service", service).execute()
        supabase.table("payouts").delete().eq("service_name", service).execute()
    except Exception as e:
        logger.error(f"Supabase delete service error: {e}")
    if firebase_db_ref:
        try:
            firebase_db_ref.reference(f"services/{service}").delete()
            firebase_db_ref.reference(f"numbers/{service}").delete()
            firebase_db_ref.reference(f"payouts/{service}").delete()
        except Exception as e:
            logger.error(f"Firebase delete service error: {e}")
    refresh_services_cache_sync()
    refresh_payouts_cache_sync()


def delete_country_db(service: str, country: str):
    try:
        supabase.table("services").delete().eq("service_name", service).eq("country_name", country).execute()
        supabase.table("numbers").delete().eq("service", service).eq("country", country).execute()
        supabase.table("payouts").delete().eq("service_name", service).eq("country_name", country).execute()
    except Exception as e:
        logger.error(f"Supabase delete country error: {e}")
    if firebase_db_ref:
        try:
            firebase_db_ref.reference(f"services/{service}/{country}").delete()
            firebase_db_ref.reference(f"numbers/{service}/{country}").delete()
            firebase_db_ref.reference(f"payouts/{service}/{country}").delete()
        except Exception as e:
            logger.error(f"Firebase delete country error: {e}")
    refresh_services_cache_sync()
    refresh_payouts_cache_sync()


def get_countries_for_service(service: str) -> list:
    if service in SERVICES_CACHE:
        return list(SERVICES_CACHE[service].keys())
    return []


def save_numbers_sync(service: str, country: str, numbers: list, payout: float = None) -> int:
    cleaned_numbers = []
    seen = set()
    for num in numbers:
        clean_num = re.sub(r'\D', '', str(num))
        if clean_num and clean_num not in seen:
            seen.add(clean_num)
            cleaned_numbers.append(clean_num)

    if not cleaned_numbers:
        return 0

    inserted = 0
    to_insert = []
    try:
        supabase.table("services").upsert({
            "service_name": service, "country_name": country,
        }).execute()

        # Existing allocations — PAGED
        globally_used = set()
        try:
            alloc_rows = fetch_all_rows("allocations", "number")
            globally_used = {str(r["number"]) for r in alloc_rows}
        except Exception as e:
            logger.error(f"allocs read error: {e}")

        # Existing numbers for this service+country — PAGED
        existing = set()
        try:
            num_rows = fetch_all_rows(
                "numbers", "number",
                filters={"service": service, "country": country},
            )
            existing = {str(r["number"]) for r in num_rows}
        except Exception as e:
            logger.error(f"existing numbers read error: {e}")

        to_insert = [n for n in cleaned_numbers if n not in globally_used and n not in existing]

        for i in range(0, len(to_insert), 500):
            chunk = to_insert[i:i + 500]
            try:
                supabase.table("numbers").insert([
                    {"service": service, "country": country, "number": n,
                     "status": "available", "user_id": 0}
                    for n in chunk
                ]).execute()
                inserted += len(chunk)
            except Exception as e:
                logger.error(f"batch numbers insert error: {e}")
    except Exception as e:
        logger.error(f"save_numbers error: {e}")

    if payout is not None:
        set_payout_sync(service, country, payout)

    if firebase_db_ref and inserted > 0:
        try:
            updates = {}
            for n in to_insert[:2000]:
                updates[f"numbers/{service}/{country}/{n}"] = {
                    "number": n, "status": "available", "user_id": 0
                }
            if updates:
                firebase_db_ref.reference("/").update(updates)
            firebase_db_ref.reference(f"services/{service}/{country}").set(True)
        except Exception as e:
            logger.error(f"Firebase mirror numbers error: {e}")

    refresh_services_cache_sync()
    return inserted


def allocate_numbers_sync(service: str, country: str, user_id: int, target_qty: int, exclude: list = None) -> list:
    """Atomic via Supabase RPC — no full cache refresh in hot path."""
    exclude_list = [str(x) for x in (exclude or [])]
    target_qty = max(1, int(target_qty))
    assigned = []

    for _ in range(target_qty):
        try:
            res = supabase.rpc("allocate_number", {
                "p_service": service, "p_country": country,
                "p_user_id": user_id, "p_exclude": exclude_list + assigned,
            }).execute()
            num = res.data
            if isinstance(num, list):
                num = num[0] if num else None
            if not num:
                break
            assigned.append(str(num))
        except Exception as e:
            logger.error(f"allocate RPC error: {e}")
            break

    # In-place cache decrement
    if service in SERVICES_CACHE and country in SERVICES_CACHE[service]:
        SERVICES_CACHE[service][country] = max(0, SERVICES_CACHE[service][country] - len(assigned))

    # Firebase mirror
    if firebase_db_ref and assigned:
        try:
            updates = {}
            for n in assigned:
                updates[f"allocations/{n}"] = {
                    "user_id": user_id, "service": service, "country": country,
                    "allocated_at": int(time.time()),
                }
                updates[f"numbers/{service}/{country}/{n}/status"] = "allocated"
                updates[f"numbers/{service}/{country}/{n}/user_id"] = user_id
            firebase_db_ref.reference("/").update(updates)
        except Exception as e:
            logger.error(f"Firebase mirror alloc error: {e}")

    return assigned


def get_user_allocations_sync(user_id: int, service: str, country: str) -> list:
    try:
        rows = fetch_all_rows(
            "allocations", "number",
            filters={"user_id": user_id, "service": service, "country": country},
        )
        return [str(r["number"]) for r in rows]
    except Exception as e:
        logger.error(f"Supabase get user allocations error: {e}")
        return []


def lookup_allocation_sync(num: str, clean_num: str):
    try:
        res = supabase.table("allocations").select("user_id, service, country").or_(
            f"number.eq.{num},number.eq.{clean_num}"
        ).limit(1).execute()
        if res.data:
            r = res.data[0]
            return r["user_id"], r["service"], r["country"]
    except Exception as e:
        logger.error(f"Supabase lookup allocation error: {e}")
    return None, None, None


# ============================================================
# SEEN OTPs (persistent via Supabase → no duplicate payouts on restart)
# ============================================================
def load_seen_otp_ids_sync() -> dict:
    """Load only IDs from last 6h (retention) with pagination."""
    cutoff = int(time.time()) - OTP_ID_RETENTION_SECONDS
    result = {}
    try:
        offset = 0
        while True:
            r = (supabase.table("seen_otps")
                 .select("msg_id")
                 .gte("ts", cutoff)
                 .range(offset, offset + SUPABASE_PAGE_SIZE - 1)
                 .execute())
            if not r.data:
                break
            for row in r.data:
                result[row["msg_id"]] = True
            if len(r.data) < SUPABASE_PAGE_SIZE:
                break
            offset += SUPABASE_PAGE_SIZE
    except Exception as e:
        logger.error(f"Supabase load seen otps error: {e}")
    return result


def mark_otp_seen_sync(msg_id: str):
    try:
        supabase.table("seen_otps").upsert({"msg_id": msg_id, "ts": int(time.time())}).execute()
    except Exception as e:
        logger.error(f"Supabase mark otp seen error: {e}")


def mark_otp_seen_batch_sync(msg_ids: list):
    if not msg_ids:
        return
    ts = int(time.time())
    try:
        supabase.table("seen_otps").upsert([{"msg_id": m, "ts": ts} for m in msg_ids]).execute()
    except Exception as e:
        logger.error(f"Supabase batch mark otp seen error: {e}")


def cleanup_old_otp_ids_sync():
    cutoff = int(time.time()) - OTP_ID_RETENTION_SECONDS
    try:
        supabase.table("seen_otps").delete().lt("ts", cutoff).execute()
    except Exception as e:
        logger.error(f"Supabase cleanup seen otps error: {e}")


# ============================================================
# VIEW BUILDERS
# ============================================================
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
            payout = get_payout_sync(srv, cnt)
            text += f"\n   └ {escape_md(cnt)}: `{count}` avail | payout `{fmt_num(payout)}৳`"
        buttons.append([create_button(f"⚙️ {srv}", callback_data=f"adm:srv:view:{srv}", style="primary")])

    buttons.append([create_button("➕ Add New Service / Numbers", callback_data="adm:srv:add", style="success")])
    return text, InlineKeyboardMarkup(buttons)


def build_service_manage_view(service: str):
    summary = get_admin_services_summary()
    cnts = summary.get(service, {})
    total_avail = sum(cnts.values())

    text = f"⚙️ **SERVICE DETAILS: {escape_md(service)}**\n\n"
    text += f"📊 Total Available Numbers: `{total_avail}`\n\n"
    text += "🏳️ **Countries, Quantities & Payouts:**\n"
    if cnts:
        for cnt, count in cnts.items():
            payout = get_payout_sync(service, cnt)
            text += f"• **{escape_md(cnt)}**: `{count}` avail | `{fmt_num(payout)} ৳`\n"
    else:
        text += "No countries configured.\n"

    buttons = [
        [create_button("➕ Add Country / Numbers", callback_data=f"adm:srv:add:{service}", style="success")],
    ]
    if cnts:
        buttons.append([create_button("💰 Manage Payouts", callback_data=f"adm:pay:mng:{service}", style="primary")])
    buttons.append([create_button("🗑️ Delete Service", callback_data=f"adm:srv:del:{service}", style="danger")])
    if cnts:
        buttons.append([create_button("❌ Delete Country", callback_data=f"adm:cnt:delli:{service}", style="danger")])
    buttons.append([create_button("Back to Services", callback_data="adm:srv:list", style="danger")])

    return text, InlineKeyboardMarkup(buttons)


def build_payout_manage_view(service: str):
    cnts = get_admin_services_summary().get(service, {})
    text = f"💰 **PAYOUT MANAGEMENT: {escape_md(service)}**\n\n"
    if cnts:
        for cnt in cnts.keys():
            payout = get_payout_sync(service, cnt)
            text += f"• **{escape_md(cnt)}**: `{fmt_num(payout)} ৳`\n"
        text += "\n👉 Tap a country below to edit its payout amount."
    else:
        text += "No countries configured yet."

    buttons = []
    for cnt in cnts.keys():
        buttons.append([create_button(f"✏️ {cnt}", callback_data=f"adm:pay:edit:{service}:{cnt}", style="primary")])
    buttons.append([create_button("Back", callback_data=f"adm:srv:view:{service}", style="danger")])
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
        [create_button("📢 Edit Channel", callback_data="adm:set:channel", style="primary"),
         create_button("🎧 Edit Support", callback_data="adm:set:support", style="primary")],
        [create_button("🔗 Edit Group Link", callback_data="adm:set:otplink", style="primary"),
         create_button("🆔 Edit Forward Group ID", callback_data="adm:set:otpgroupid", style="primary")],
        [create_button("👨‍💻 Edit Dev Name", callback_data="adm:set:devname", style="primary"),
         create_button("🔗 Edit Dev Link", callback_data="adm:set:devlink", style="primary")]
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
        [create_button("1", callback_data="adm:setqty:1", style="primary" if current_qty != "1" else "success"),
         create_button("2", callback_data="adm:setqty:2", style="primary" if current_qty != "2" else "success"),
         create_button("3", callback_data="adm:setqty:3", style="primary" if current_qty != "3" else "success")],
        [create_button("4", callback_data="adm:setqty:4", style="primary" if current_qty != "4" else "success"),
         create_button("5", callback_data="adm:setqty:5", style="primary" if current_qty != "5" else "success"),
         create_button("6", callback_data="adm:setqty:6", style="primary" if current_qty != "6" else "success")]
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
        [create_button("💳 Withdraw Settings", callback_data="adm:w_settings", style="primary"),
         create_button("🏆 Ranking Bonuses", callback_data="adm:r_bonus_settings", style="primary")]
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
        [create_button("➕ Add Method", callback_data="adm:w_add_m", style="success"),
         create_button("❌ Remove Method", callback_data="adm:w_del_m_list", style="danger")],
        [create_button("✏️ Edit Min Amount", callback_data="adm:w_set_min", style="primary")],
        [create_button("Back", callback_data="adm:extra_back", style="danger")]
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


# ============================================================
# FLASK HEALTH
# ============================================================
flask_app = Flask(__name__)


@flask_app.route('/')
def home():
    return f"Bot running! DB Mode: {CURRENT_DB_MODE}"


@flask_app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "db_mode": CURRENT_DB_MODE,
        "known_users": len(KNOWN_USERS),
        "services": len(SERVICES_CACHE),
        "service_counts": SERVICES_CACHE,
        "panels": len(PANEL_TASKS),
        "batch_rpc": "enabled" if _BATCH_RPC_AVAILABLE else "fallback",
        "firebase": "connected" if firebase_db_ref else "disabled",
        "otp_retention_hours": OTP_ID_RETENTION_SECONDS // 3600,
    })


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


# ============================================================
# STATES
# ============================================================
(
    ADD_SERVICE,
    ADD_COUNTRY,
    WAIT_PAYOUT_AMOUNT,
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
) = range(25)


# ---------------- AUTH DECORATOR ----------------
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        query = update.callback_query
        user = update.effective_user
        if query:
            try:
                await query.answer()
            except Exception:
                pass
        if not user or not (await run_db(is_admin_sync, user.id)):
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


# ---------------- KEYBOARDS ----------------
def get_main_keyboard(user_id: int):
    keyboard_layout = [
        [{"text": "GET NUMBER", "style": "success"}],
        [{"text": "PROFILE", "style": "primary"}, {"text": "WALLET", "style": "primary"}],
        [{"text": "RANKING", "style": "danger"}, {"text": "SUPPORT", "style": "danger"}]
    ]
    if is_admin_sync(user_id):
        keyboard_layout.append([{"text": "ADMIN PANEL", "style": "danger"}])
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


def get_admin_keyboard():
    keyboard_layout = [
        [{"text": "SERVICES", "style": "success"}, {"text": "BROADCAST", "style": "success"}],
        [{"text": "ADMIN CONTROL", "style": "primary"}, {"text": "GLOBAL SETTINGS", "style": "primary"}],
        [{"text": "MANAGE PAYOUTS", "style": "primary"}, {"text": "BACK", "style": "danger"}]
    ]
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


def get_global_settings_keyboard():
    keyboard_layout = [
        [{"text": "EDIT LINKS", "style": "success"}, {"text": "EDIT API", "style": "success"}],
        [{"text": "NUMBER QUANTITY", "style": "primary"}, {"text": "EXTRA", "style": "primary"}],
        [{"text": "BACK", "style": "danger"}]
    ]
    return ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)


# ============================================================
# BOT HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or ""
    await run_db(save_user, user_id, first_name)

    for key in ('service_name', 'country_name', 'w_method', 'w_wallet',
                'reject_req_id', 'new_api_name', 'new_api_url', 'new_api_token',
                'new_admin_id', 'pay_edit_service', 'pay_edit_country', 'pay_flow'):
        context.user_data.pop(key, None)

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
        kbd = InlineKeyboardMarkup([[create_button("Referral Link", copy_text=refer_link, style="success")]])
        await update.message.reply_text(profile_text, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper == "WALLET":
        bal = await run_db(get_user_balance_sync, user_id)
        bal_str = fmt_num(bal)
        wallet_text = (
            f"👛 **YOUR WALLET**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Balance:** `{bal_str} ৳`"
        )
        kbd = InlineKeyboardMarkup([[create_button("💸 Withdraw", callback_data="usr:withdraw", style="success")]])
        await update.message.reply_text(wallet_text, reply_markup=kbd, parse_mode="Markdown")

    elif text_upper in ["RANKING", "LEADERBOARD"]:
        leaderboard_msg = await run_db(get_ranking_leaderboard_sync, user_id)
        await update.message.reply_text(leaderboard_msg, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

    elif text_upper == "SUPPORT":
        sp_link = clean_tg_link(get_setting("support", "@your_support"))
        ch_link = clean_tg_link(get_setting("channel", "https://t.me/your_channel"))
        kbd = InlineKeyboardMarkup([
            [create_button("Support", url=sp_link, style="primary"),
             create_button("Channel", url=ch_link, style="primary")]
        ])
        await update.message.reply_text("Click below to contact support or join our channel:", reply_markup=kbd)

    elif text_upper == "ADMIN PANEL" and user_is_admin:
        context.user_data['current_menu'] = 'admin'
        total_users = len(await run_db(get_all_users))
        await update.message.reply_text(
            f"**ADMIN PANEL**\n\n"
            f"⚙️ DB Mode: **{CURRENT_DB_MODE}**\n"
            f"👥 Total Registered Users: `{total_users}`",
            reply_markup=get_admin_keyboard(), parse_mode="Markdown"
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
            reply_markup=get_global_settings_keyboard(), parse_mode="Markdown"
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
                reply_markup=get_admin_keyboard(), parse_mode="Markdown"
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
    min_w = float(get_setting("min_withdraw_amount", "50") or 50)

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
            error_msg = "❌ Invalid Account Number! Mobile banking numbers must be at least 11 digits."
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
    min_w = float(get_setting("min_withdraw_amount", "50") or 50)

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
    min_w = float(get_setting("min_withdraw_amount", "50") or 50)

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


# ---------------- ADMIN: RECEIVE PAYOUT & EDIT PAYOUT ----------------
@admin_only
async def start_edit_payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":", 4)
    if len(parts) < 5:
        await query.message.reply_text("⚠️ Invalid edit request.")
        return ConversationHandler.END
    service, country = parts[3], parts[4]
    context.user_data['pay_edit_service'] = service
    context.user_data['pay_edit_country'] = country
    context.user_data['pay_flow'] = 'edit'

    curr = get_payout_sync(service, country)
    await query.message.reply_text(
        f"💰 Editing payout for:\n"
        f"🏷️ Service: **{escape_md(service)}**\n"
        f"🏳️ Country: **{escape_md(country)}**\n\n"
        f"Current payout: `{fmt_num(curr)} ৳`\n"
        f"Send new amount (or send `default` for {fmt_num(DEFAULT_PAYOUT)} ৳):",
        parse_mode="Markdown"
    )
    return WAIT_PAYOUT_AMOUNT


async def receive_payout_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_text = update.message.text.strip().lower()
    if input_text in ("default", "d", ""):
        val = DEFAULT_PAYOUT
    else:
        try:
            val = float(input_text)
            if val < 0:
                val = 0.0
        except ValueError:
            await update.message.reply_text("❌ Invalid amount! Please enter a valid number (e.g., 0.5, 1, 2.5) or `default`.")
            return WAIT_PAYOUT_AMOUNT

    flow = context.user_data.get('pay_flow')

    if flow == 'edit':
        service = context.user_data.get('pay_edit_service')
        country = context.user_data.get('pay_edit_country')
        await run_db(set_payout_sync, service, country, val)
        await update.message.reply_text(
            f"✅ Payout for **{escape_md(service)}** → **{escape_md(country)}** set to `{fmt_num(val)} ৳`!",
            parse_mode="Markdown"
        )
        text_msg, kbd = build_payout_manage_view(service)
        await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
        for key in ('pay_flow', 'pay_edit_service', 'pay_edit_country'):
            context.user_data.pop(key, None)
        return ConversationHandler.END
    else:
        context.user_data['payout_amount'] = val
        await update.message.reply_text(
            f"✅ Payout set to `{fmt_num(val)} ৳` per OTP.\n\n"
            "Now send the numbers (as a text file or one number per line):",
            parse_mode="Markdown"
        )
        return ADD_NUMBERS


# ---------------- RANKING BONUS ----------------
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


# ---------------- DEV / ADMIN SETTINGS ----------------
@admin_only
async def set_dev_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.message.reply_text("Enter the new Developer Name/Tag (e.g., John Doe):")
    return WAIT_DEV_NAME


async def receive_dev_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    await run_db(set_setting, "dev_username", new_name)
    await update.message.reply_text(f"✅ Developer Name updated!\nCurrent Name: `{escape_md(new_name)}`", parse_mode="Markdown")
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
    await update.message.reply_text(f"✅ Developer Link updated!\nCurrent Link: {escape_md(new_link)}", parse_mode="Markdown")
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
        await update.message.reply_text(f"✅ Payment method **{escape_md(method_name)}** added!", parse_mode="Markdown")
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
            ok = await run_db(atomic_transition_withdraw_status_sync, req_id, "pending", "rejected", reason_text)
            if ok:
                await run_db(refund_user_balance_sync, req["user_id"], req["amount"])
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
                await update.message.reply_text("❌ Could not reject — request was already processed.")
        else:
            await update.message.reply_text("❌ Request was already processed or not found.")

    context.user_data.pop('reject_req_id', None)
    text_msg, kbd = build_admin_withdraw_requests_view(1)
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------- ADMIN SETUP ----------------
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
    await query.message.reply_text(
        f"Service **{escape_md(service)}** selected.\n\nEnter country name (e.g., Bangladesh, Nepal):",
        parse_mode="Markdown"
    )
    return ADD_COUNTRY


async def receive_service_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['service_name'] = update.message.text.strip()
    await update.message.reply_text("Enter country name (e.g., Bangladesh, Nepal):")
    return ADD_COUNTRY


async def receive_country_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country = update.message.text.strip()
    context.user_data['country_name'] = country
    service = context.user_data.get('service_name', '')

    existing_payout = get_payout_sync(service, country) if service else DEFAULT_PAYOUT
    hint = ""
    if service and country in PAYOUTS_CACHE.get(service, {}):
        hint = f"\n⚠️ This country already exists with payout `{fmt_num(existing_payout)} ৳`. Sending a new payout will overwrite it."

    await update.message.reply_text(
        f"💵 Enter the **payout amount (৳)** each user will receive per OTP "
        f"for **{escape_md(service)}** → **{escape_md(country)}**.\n\n"
        f"Default: `{fmt_num(DEFAULT_PAYOUT)} ৳` (send `default` to use this)."
        f"{hint}",
        parse_mode="Markdown"
    )
    return WAIT_PAYOUT_AMOUNT


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
    payout = context.user_data.get('payout_amount', DEFAULT_PAYOUT)

    if service and country and numbers:
        valid_count = await run_db(save_numbers_sync, service, country, numbers, payout)
        await update.message.reply_text(
            f"✅ Successfully added **{valid_count}** numbers!\n"
            f"💰 Payout per OTP: `{fmt_num(payout)} ৳`",
            reply_markup=get_admin_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Incomplete data provided. Please try again.", reply_markup=get_admin_keyboard())

    for key in ('service_name', 'country_name', 'payout_amount'):
        context.user_data.pop(key, None)
    context.user_data['current_menu'] = 'admin'
    return ConversationHandler.END


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
        await update.message.reply_text(f"✅ Admin **{escape_md(admin_name)}** (`{new_id}`) added!", parse_mode="Markdown")
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
    context.user_data['new_api_url'] = update.message.text.strip()
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
            f"✅ **API Panel Connected!**\n\n📌 Name: `{escape_md(p_name)}`\n⏱️ Polling: `{interval_val}s`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Incomplete panel data.")

    for key in ('new_api_name', 'new_api_url', 'new_api_token'):
        context.user_data.pop(key, None)
    text_msg, kbd = build_api_panels_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------- EDIT LINKS ----------------
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
    await update.message.reply_text(f"✅ Channel link updated!\nCurrent link: {escape_md(new_link)}", parse_mode="Markdown")
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
        await update.message.reply_text("❌ Invalid username/link!\nType /cancel to abort.")
        return WAIT_SUPPORT
    await run_db(set_setting, "support", new_link)
    await update.message.reply_text(f"✅ Support updated!\nCurrent: {escape_md(new_link)}", parse_mode="Markdown")
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
        await update.message.reply_text("❌ Invalid link!\nType /cancel to abort.")
        return WAIT_OTP_LINK
    await run_db(set_setting, "otp_group_link", new_link)
    await update.message.reply_text(f"✅ OTP Group link updated!\nCurrent link: {escape_md(new_link)}", parse_mode="Markdown")
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
    await update.message.reply_text(f"✅ OTP Forward Group ID updated!\nCurrent: `{escape_md(new_id)}`", parse_mode="Markdown")
    text_msg, kbd = build_edit_links_view()
    await update.message.reply_text(text_msg, reply_markup=kbd, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------- BROADCAST ----------------
@admin_only
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = await run_db(get_all_users)
    await update.message.reply_text(
        f"📢 **BROADCAST SYSTEM**\n\n"
        f"Target Audience: `{len(all_users)}` users\n\n"
        f"Send or forward the message (text, photo, video, document, etc.) you want to broadcast to all users.\n"
        f"Type /cancel to abort.",
        parse_mode="Markdown"
    )
    return WAIT_BROADCAST_MSG


async def receive_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = await run_db(get_all_users)
    if not all_users:
        await update.message.reply_text("No users found in database to broadcast.", reply_markup=get_admin_keyboard())
        return ConversationHandler.END

    total = len(all_users)
    status_msg = await update.message.reply_text(
        f"⏳ Broadcasting to `{total}` users (concurrent workers)...", parse_mode="Markdown"
    )

    from_chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    success_count = 0
    failed_count = 0
    counter_lock = asyncio.Lock()
    sem = asyncio.Semaphore(BROADCAST_CONCURRENCY)

    async def send_one(target_id: int):
        nonlocal success_count, failed_count
        async with sem:
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=from_chat_id,
                    message_id=msg_id
                )
                async with counter_lock:
                    success_count += 1
            except Exception as e:
                logging.error(f"Broadcast failed for {target_id}: {e}")
                async with counter_lock:
                    failed_count += 1
            await asyncio.sleep(BROADCAST_PER_MSG_DELAY)

    tasks = [asyncio.create_task(send_one(uid)) for uid in all_users]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logging.error(f"Broadcast gather error: {e}")

    report = (
        f"📢 **BROADCAST COMPLETED**\n\n"
        f"✅ **Successfully Sent:** `{success_count}`\n"
        f"❌ **Failed / Blocked:** `{failed_count}`\n"
        f"📊 **Total Target Users:** `{total}`"
    )
    try:
        await status_msg.edit_text(report, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(report, parse_mode="Markdown")
    await update.message.reply_text("Select an option from Admin Menu:", reply_markup=get_admin_keyboard())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ('service_name', 'country_name', 'payout_amount', 'new_api_name',
                'new_api_url', 'new_api_token', 'new_admin_id', 'w_method', 'w_wallet',
                'reject_req_id', 'pay_flow', 'pay_edit_service', 'pay_edit_country'):
        context.user_data.pop(key, None)

    if update.message and update.message.text:
        await handle_text_menu(update, context)
    return ConversationHandler.END


# ---------------- CALLBACK HANDLER ----------------
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
        min_w = float(get_setting("min_withdraw_amount", "50") or 50)

        if bal < min_w:
            await query.answer(
                f"❌ আপনার ব্যালেন্স পর্যাপ্ত নয়! মিনিমাম উইথড্র: {fmt_num(min_w)} ৳। আপনার ব্যালেন্স: {fmt_num(bal)} ৳।",
                show_alert=True
            )
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
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        text_msg, kbd = build_withdraw_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:r_bonus_settings":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        text_msg, kbd = build_ranking_bonus_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:extra_back":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:panel_back":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        total_users = len(await run_db(get_all_users))
        await query.message.reply_text(
            f"**ADMIN PANEL**\n\n⚙️ DB Mode: **{CURRENT_DB_MODE}**\n👥 Total Registered Users: `{total_users}`",
            reply_markup=get_admin_keyboard(), parse_mode="Markdown"
        )

    elif data == "adm:w_del_m_list":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
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
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        page = int(data.split(":", 2)[2])
        text_msg, kbd = build_admin_withdraw_requests_view(page)
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:w_view:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        req_id = int(data.split(":", 2)[2])
        text_msg, kbd = build_withdraw_detail_view(req_id)
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:w_app:"):
        if not user_is_admin:
            await query.answer()
            return
        req_id = int(data.split(":", 2)[2])
        req = await run_db(get_withdraw_request_by_id_sync, req_id)
        if not req or req["status"] != "pending":
            await query.answer("Request already processed or invalid!", show_alert=True)
            return

        ok = await run_db(atomic_transition_withdraw_status_sync, req_id, "pending", "approved", "")
        if not ok:
            await query.answer("Request already processed!", show_alert=True)
            return

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

    elif data == "adm:ctrl:list":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        text_msg, kbd = build_admin_control_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:delconf:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        target_uid = int(data.split(":", 2)[2])
        if target_uid == ADMIN_ID:
            await query.answer("❌ Main Owner cannot be removed!", show_alert=True)
            return

        admins = await run_db(get_all_admins_sync)
        target_adm = next((a for a in admins if a["user_id"] == target_uid), None)
        target_name = target_adm["name"] if target_adm else "Admin"

        text = (
            f"⚠️ **CONFIRMATION REQUIRED**\n\n"
            f"Are you sure you want to remove admin **{escape_md(target_name)}** (`{target_uid}`)?\n"
            f"This action cannot be undone."
        )
        kbd = InlineKeyboardMarkup([
            [create_button("✅ YES, REMOVE", callback_data=f"adm:del:{target_uid}", style="danger"),
             create_button("❌ CANCEL", callback_data="adm:ctrl:list", style="primary")]
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
        await query.answer("Admin removed!" if success else "Failed to remove admin.", show_alert=True)
        text_msg, kbd = build_admin_control_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:setqty:"):
        if not user_is_admin:
            await query.answer()
            return
        qty_val = data.split(":", 2)[2]
        await run_db(set_setting, "number_quantity", qty_val)
        await query.answer(f"Number quantity set to {qty_val}!", show_alert=True)
        text_msg, kbd = build_number_quantity_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data in ("adm:toggle:show_msg", "adm:toggle:country_count", "adm:toggle:show_dev",
                  "adm:toggle:withdraw", "adm:toggle:ranking_bonus"):
        if not user_is_admin:
            await query.answer()
            return
        setting_map = {
            "adm:toggle:show_msg": ("show_message", "true", "Show Message"),
            "adm:toggle:country_count": ("show_country_count", "false", "Country number count"),
            "adm:toggle:show_dev": ("show_developer", "true", "Developer Info"),
            "adm:toggle:withdraw": ("withdraw_enabled", "true", "Withdraw system"),
            "adm:toggle:ranking_bonus": ("ranking_bonus_enabled", "true", "Ranking Bonus System"),
        }
        key, default_val, label = setting_map[data]
        curr_val = get_setting(key, default_val)
        new_val = "false" if curr_val == "true" else "true"
        await run_db(set_setting, key, new_val)
        status_text = "enabled" if new_val == "true" else "disabled"
        await query.answer(f"{label} is now {status_text}!", show_alert=True)
        text_msg, kbd = build_extra_settings_view()
        await query.edit_message_text(text_msg, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:srv:list":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        text, kbd = build_admin_services_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:srv:view:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        service = data.split(":", 3)[3]
        text, kbd = build_service_manage_view(service)
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:srv:del:"):
        if not user_is_admin:
            await query.answer()
            return
        service = data.split(":", 3)[3]
        await run_db(delete_service_db, service)
        await query.answer(f"Service {service} deleted!", show_alert=True)
        text, kbd = build_admin_services_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:cnt:delli:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
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

    elif data.startswith("adm:pay:mng:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        service = data.split(":", 3)[3]
        text, kbd = build_payout_manage_view(service)
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data == "adm:api:list":
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        text, kbd = build_api_panels_view()
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:api:view:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        panel_id = data.split(":", 3)[3]
        text, kbd = build_panel_manage_view(panel_id)
        await query.edit_message_text(text, reply_markup=kbd, parse_mode="Markdown")

    elif data.startswith("adm:api:delconf:"):
        if not user_is_admin:
            await query.answer()
            return
        await query.answer()
        panel_id = data.split(":", 3)[3]
        p = await run_db(get_api_panel_sync, panel_id)
        if p:
            text = f"⚠️ **ARE YOU SURE?**\n\nDo you really want to delete the panel **'{escape_md(p['name'])}'**?"
            kbd = InlineKeyboardMarkup([
                [create_button("✅ YES, DELETE", callback_data=f"adm:api:del:{panel_id}", style="danger"),
                 create_button("❌ CANCEL", callback_data=f"adm:api:view:{panel_id}", style="primary")]
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
        target_qty = int(get_setting("number_quantity", "2") or 2)
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
        target_qty = int(get_setting("number_quantity", "2") or 2)

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


# ============================================================
# OTP POLLING & PROCESSING
# ============================================================
def _remember_processed(msg_id: str):
    PROCESSED_OTP_IDS_CACHE[msg_id] = True
    if len(PROCESSED_OTP_IDS_CACHE) > MAX_PROCESSED_IN_MEMORY:
        for _ in range(MAX_PROCESSED_IN_MEMORY // 2):
            PROCESSED_OTP_IDS_CACHE.popitem(last=False)


async def process_otp_items(items: list, application: Application, processed_ids: dict):
    """Batched: 1 write for seen_otps, 1 read for allocations, 1 RPC for all earnings."""
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

    # ---- Step 1: filter new valid items ----
    valid = []
    new_ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            if float(item.get("payout", "0")) <= 0:
                continue
        except (ValueError, TypeError):
            continue

        num = str(item.get("num", "")).strip()
        msg = item.get("message", "")
        dt = item.get("dt", "")
        if not num or not msg:
            continue

        mid = hashlib.md5(f"{num}_{dt}_{msg}".encode()).hexdigest()
        if mid in processed_ids:
            continue
        processed_ids[mid] = True
        _remember_processed(mid)
        new_ids.append(mid)
        valid.append(item)

    if not valid:
        return

    # ---- Step 2: batch insert seen IDs ----
    await run_db(mark_otp_seen_batch_sync, new_ids)

    # ---- Step 3: batch lookup allocations ----
    numbers = [str(it.get("num", "")).strip() for it in valid]
    clean_numbers = [re.sub(r'\D', '', n) for n in numbers]
    alloc_map = {}
    try:
        combined = list(set(numbers + clean_numbers))
        for i in range(0, len(combined), 200):
            chunk = combined[i:i+200]
            res = supabase.table("allocations").select("number, user_id, service, country").in_("number", chunk).execute()
            for r in res.data:
                alloc_map[r["number"]] = r
    except Exception as e:
        logger.error(f"batch alloc lookup error: {e}")

    # ---- Step 4: group per user ----
    per_user = {}
    for it in valid:
        num = str(it.get("num", "")).strip()
        clean = re.sub(r'\D', '', num)
        alloc = alloc_map.get(num) or alloc_map.get(clean)
        if not alloc:
            continue
        u = alloc["user_id"]
        payout = await run_db(get_payout_supabase_wrapper, alloc["service"], alloc["country"])
        if u not in per_user:
            per_user[u] = {"amount": 0.0, "count": 0, "items": [], "service": alloc["service"]}
        per_user[u]["amount"] += payout
        per_user[u]["count"] += 1
        per_user[u]["items"].append((it, alloc))

    # ---- Step 5: BATCH balance update (single RPC) + send messages ----
    if per_user:
        batch_updates = [
            {"user_id": u, "amount": d["amount"], "count": d["count"]}
            for u, d in per_user.items()
        ]
        profiles = await run_db(add_otp_earnings_batch_sync, batch_updates)
    else:
        profiles = {}

    for u, data in per_user.items():
        prof = profiles.get(u) or {"balance": 0.0, "total_otps": 0}
        bal_str = fmt_num(prof["balance"])

        for item, alloc in data["items"]:
            num = str(item.get("num", "")).strip()
            msg = item.get("message", "")
            cli = (item.get("cli") or "").strip()
            display_service = cli if cli else (alloc.get("service") or "Service")

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
                    [create_button("Channel", url=ch_link, style="primary"),
                     create_button("Get Number", url=bot_link, style="primary")],
                    [create_button(f"{otp_code}", copy_text=otp_code, style="success")]
                ])
                try:
                    await application.bot.send_message(
                        chat_id=target_otp_group, text=group_text, reply_markup=group_kbd,
                        parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )
                except Exception as e:
                    logging.error(f"Group Forward Error: {e}")

            payout_per_otp = data["amount"] / max(1, data["count"])
            payout_str = fmt_num(payout_per_otp)

            if show_msg_enabled:
                user_text = (
                    "— — — — — — — — — —\n"
                    f"<blockquote>📱 SERVICE: {safe_service}</blockquote>\n"
                    f"<blockquote>📞 NUMBER: {safe_num}</blockquote>\n"
                    f"<blockquote>➕ ADDED  ➜ {payout_str} TK</blockquote>\n"
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
                    f"<blockquote>➕ ADDED  ➜ {payout_str} TK</blockquote>\n"
                    f"<blockquote>💳 BALANCE ➜ {bal_str} TK</blockquote>\n"
                    "— — — — — — — — — —"
                )

            user_kbd = InlineKeyboardMarkup([[create_button(f"{otp_code}", copy_text=otp_code, style="success")]])
            try:
                await application.bot.send_message(
                    chat_id=u, text=user_text, reply_markup=user_kbd, parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"User Forward Error: {e}")


def get_payout_supabase_wrapper(service: str, country: str) -> float:
    """Thin wrapper so we can call via run_db."""
    return get_payout_sync(service, country)


async def poll_single_panel(panel_id: str, application: Application, processed_ids: dict):
    async with httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=5)) as client:
        error_streak = 0
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
                    error_streak = 0
                else:
                    error_streak += 1
            except Exception as e:
                error_streak += 1
                logging.error(f"Polling Exception for Panel {panel_id}: {e}")

            sleep_t = interval * min(8, 2 ** min(error_streak, 3)) if error_streak else interval
            await asyncio.sleep(sleep_t)


async def otp_poller_manager(application: Application):
    processed_ids = await run_db(load_seen_otp_ids_sync)
    for mid in list(processed_ids.keys())[:MAX_PROCESSED_IN_MEMORY]:
        _remember_processed(mid)

    cycle_count = 0
    last_reset_check = 0.0

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

            now = time.time()
            if now - last_reset_check >= WEEKLY_RESET_CHECK_INTERVAL:
                last_reset_check = now
                notifications = await run_db(check_and_process_weekly_reset_sync)
                for n in notifications:
                    try:
                        msg = (
                            "🎉 <b>CONGRATULATIONS! WEEKLY RANKING BONUS!</b>\n\n"
                            f"You earned a <b>{fmt_num(n['amount'])} ৳</b> bonus for ranking <b>Top {n['rank']}</b> this week! 🏆\n"
                            "Bonus added to your wallet."
                        )
                        await application.bot.send_message(chat_id=n["uid"], text=msg, parse_mode="HTML")
                    except Exception as e:
                        logging.error(f"Failed sending rank bonus notification to {n['uid']}: {e}")

        except Exception as e:
            logging.error(f"OTP Poller Manager Error: {e}")

        await asyncio.sleep(5)


# ============================================================
# MAIN FUNCTION
# ============================================================
def main():
    if not init_supabase():
        logger.error("Failed to initialize Supabase. Exiting.")
        return

    init_firebase()
    refresh_all_caches_sync()

    if not SETTINGS_CACHE:
        restore_from_firebase_to_supabase()
        refresh_all_caches_sync()

    load_known_users()

    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"Flask started on port {os.environ.get('PORT', 8080)}")

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

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
            CallbackQueryHandler(start_edit_payout, pattern="^adm:pay:edit:"),
            MessageHandler(filters.Regex("(?i)^Broadcast$"), broadcast_start),
        ],
        states={
            ADD_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_service_name)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_country_name)],
            WAIT_PAYOUT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~MENU_FILTER, receive_payout_amount)],
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
        asyncio.create_task(firebase_backup_worker())
        logger.info("Bot started: Supabase primary + Firebase backup")

    async def post_shutdown(app: Application):
        try:
            await asyncio.to_thread(push_dirty_to_firebase_sync)
            logger.info("Final Firebase backup flush complete")
        except Exception as e:
            logger.error(f"Shutdown backup error: {e}")

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    application.run_polling()


if __name__ == "__main__":
    main()