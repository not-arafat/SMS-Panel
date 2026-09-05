import os
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import requests

import firebase_admin
from firebase_admin import credentials, db

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

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Config variables (Environment Variable থেকে নেওয়া হবে)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
OTP_GROUP_ID = int(os.getenv("OTP_GROUP_ID", "0"))
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
API_URL = os.getenv("API_URL", "https://server.teleroutex.com/api/message-data-record/viewstats?apiKey=xiaJtFpkTQyQHQUg5Qnq0DpjQxOYgbiYt2d4aCwzeVA%3D")

# Firebase Init
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_DATABASE_URL
})

# Firebase References
ref_services = db.reference('services')
ref_numbers = db.reference('numbers')
ref_allocated = db.reference('allocated_numbers')
ref_processed = db.reference('processed_msg_ids')

# Conversation States
WAITING_SERVICE_NAME = 1
WAITING_COUNTRY_SERVICE = 2
WAITING_COUNTRY_NAME = 3
WAITING_NUMBER_SERVICE = 4
WAITING_NUMBER_COUNTRY = 5
WAITING_NUMBERS_INPUT = 6

# Render Health Check Server (Port Bind Requirement)
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_health_check():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# Helper Keyboards
def get_main_keyboard(user_id: int):
    keyboard = [
        ["Get Number"],
        ["Channel", "Support"]
    ]
    if user_id == ADMIN_ID:
        keyboard.append(["Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "স্বাগতম! নিচের মেনু থেকে অপশন সিলেক্ট করুন:",
        reply_markup=get_main_keyboard(user_id)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "Channel":
        await update.message.reply_text("আমাদের চ্যানেল লিংক: https://t.me/your_channel")
    elif text == "Support":
        await update.message.reply_text("সাপোর্টের জন্য যোগাযোগ করুন: @your_support")
    elif text == "Get Number":
        await show_services(update, context)
    elif text == "Admin Panel" and user_id == ADMIN_ID:
        await show_admin_panel(update, context)

# User Flow
async def show_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services_data = ref_services.get()
    if not services_data:
        await update.message.reply_text("বর্তমানে কোনো সার্ভিস এভেইলএবল নেই।")
        return

    keyboard = []
    for s_id, s_val in services_data.items():
        keyboard.append([InlineKeyboardButton(s_val['name'], callback_data=f"user_service_{s_id}")])
    
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("সার্ভিস সিলেক্ট করুন:", reply_markup=markup)

async def user_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_service_"):
        service_id = data.split("_")[2]
        services_data = ref_services.get() or {}
        countries = services_data.get(service_id, {}).get("countries", {})
        
        if not countries:
            await query.edit_message_text("এই সার্ভিসের জন্য কোনো কান্ট্রি নেই।")
            return
            
        keyboard = []
        for c_id, c_val in countries.items():
            keyboard.append([InlineKeyboardButton(c_val['name'], callback_data=f"user_country_{service_id}_{c_id}")])
        
        await query.edit_message_text("কান্ট্রি সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("user_country_"):
        _, _, service_id, country_id = data.split("_")
        user_id = query.from_user.id
        
        # Available number খোঁজা
        all_numbers = ref_numbers.get() or {}
        assigned_num = None
        assigned_key = None

        for n_key, n_val in all_numbers.items():
            if n_val.get("service_id") == service_id and n_val.get("country_id") == country_id and not n_val.get("status"):
                assigned_num = n_val.get("number")
                assigned_key = n_key
                break

        if not assigned_num:
            await query.edit_message_text("দুঃখিত, এই কান্ট্রির কোনো নাম্বার এভেইলএবল নেই।")
            return

        # Status Update in Firebase
        ref_numbers.child(assigned_key).update({"status": "used"})
        ref_allocated.child(str(assigned_num)).set({
            "user_id": user_id,
            "assigned_at": asyncio.get_event_loop().time()
        })

        await query.edit_message_text(
            f"আপনার প্রাপ্ত নাম্বার: `{assigned_num}`\n\n"
            "এই নাম্বারে OTP পাঠালে কিছুক্ষণের মধ্যে বোট আপনাকে ফরওয়ার্ড করবে।",
            parse_mode="Markdown"
        )

# Admin Panel Functions
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Add Service", callback_data="admin_add_service")],
        [InlineKeyboardButton("Add Country", callback_data="admin_add_country")],
        [InlineKeyboardButton("Add Numbers", callback_data="admin_add_numbers")]
    ]
    await update.message.reply_text("এডমিন প্যানেল:", reply_markup=InlineKeyboardMarkup(keyboard))

# Admin Conversation Handlers
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "admin_add_service":
        await query.edit_message_text("নতুন সার্ভিসের নাম লিখুন (যেমন: TikTok, Facebook):")
        return WAITING_SERVICE_NAME

    elif data == "admin_add_country":
        services_data = ref_services.get() or {}
        if not services_data:
            await query.edit_message_text("আগে সার্ভিস এড করুন।")
            return ConversationHandler.END

        keyboard = [[InlineKeyboardButton(v['name'], callback_data=f"adc_{k}")] for k, v in services_data.items()]
        await query.edit_message_text("কোন সার্ভিসে কান্ট্রি এড করতে চান?", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAITING_COUNTRY_SERVICE

    elif data == "admin_add_numbers":
        services_data = ref_services.get() or {}
        if not services_data:
            await query.edit_message_text("আগে সার্ভিস ও কান্ট্রি এড করুন।")
            return ConversationHandler.END

        keyboard = [[InlineKeyboardButton(v['name'], callback_data=f"adn_s_{k}")] for k, v in services_data.items()]
        await query.edit_message_text("সার্ভিস সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAITING_NUMBER_SERVICE

async def save_service_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    ref_services.push({'name': name})
    await update.message.reply_text(f"সার্ভিস '{name}' সফলভাবে সেভ হয়েছে!")
    return ConversationHandler.END

async def select_country_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    service_id = query.data.replace("adc_", "")
    context.user_data['target_service'] = service_id
    await query.edit_message_text("কান্ট্রির নাম লিখুন (যেমন: Bangladesh):")
    return WAITING_COUNTRY_NAME

async def save_country_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country_name = update.message.text.strip()
    service_id = context.user_data.get('target_service')
    
    if service_id:
        ref_services.child(service_id).child("countries").push({'name': country_name})
        await update.message.reply_text(f"কান্ট্রি '{country_name}' সফলভাবে সেভ হয়েছে!")
    return ConversationHandler.END

async def select_number_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    service_id = query.data.replace("adn_s_", "")
    context.user_data['num_service'] = service_id

    services_data = ref_services.get() or {}
    countries = services_data.get(service_id, {}).get("countries", {})
    
    if not countries:
        await query.edit_message_text("এই সার্ভিসে কোনো কান্ট্রি নেই। আগে কান্ট্রি এড করুন।")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(v['name'], callback_data=f"adn_c_{k}")] for k, v in countries.items()]
    await query.edit_message_text("কান্ট্রি সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_NUMBER_COUNTRY

async def select_number_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country_id = query.data.replace("adn_c_", "")
    context.user_data['num_country'] = country_id

    await query.edit_message_text(
        "এবার লাইন বাই লাইন নাম্বার লিখে অথবা একটি `.txt` ফাইল আপলোড করে পাঠান:"
    )
    return WAITING_NUMBERS_INPUT

async def save_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service_id = context.user_data.get('num_service')
    country_id = context.user_data.get('num_country')
    numbers_list = []

    if update.message.document:
        file = await update.message.document.get_file()
        content = (await file.download_as_bytearray()).decode('utf-8')
        numbers_list = [line.strip() for line in content.splitlines() if line.strip()]
    elif update.message.text:
        numbers_list = [line.strip() for line in update.message.text.splitlines() if line.strip()]

    added_count = 0
    for num in numbers_list:
        ref_numbers.push({
            'number': num,
            'service_id': service_id,
            'country_id': country_id,
            'status': None
        })
        added_count += 1

    await update.message.reply_text(f"মোট {added_count} টি নাম্বার সফলভাবে সেভ করা হয়েছে।")
    return ConversationHandler.END

async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("প্রক্রিয়া বাতিল করা হয়েছে।")
    return ConversationHandler.END

# Background Task for checking OTPs
async def check_otp_loop(app: Application):
    while True:
        try:
            response = requests.get(API_URL, timeout=10)
            if response.status_code == 200:
                json_data = response.json()
                docs = json_data.get("data", {}).get("docs", [])
                
                processed_ids = ref_processed.get() or {}

                for doc in docs:
                    msg_id = doc.get("_id")
                    number = doc.get("number")
                    message = doc.get("message")
                    cli = doc.get("cli")

                    if msg_id and msg_id not in processed_ids:
                        text_to_send = f"<b>New OTP Received!</b>\n\n<b>Sender:</b> {cli}\n<b>Number:</b> {number}\n<b>Message:</b>\n<code>{message}</code>"
                        
                        # 1. Forward to Global Channel/Group
                        if OTP_GROUP_ID != 0:
                            try:
                                await app.bot.send_message(chat_id=OTP_GROUP_ID, text=text_to_send, parse_mode="HTML")
                            except Exception as e:
                                logger.error(f"Group Forward Error: {e}")

                        # 2. Check allocated user & Forward to User
                        allocated_data = ref_allocated.child(str(number)).get()
                        if allocated_data:
                            user_id = allocated_data.get("user_id")
                            try:
                                await app.bot.send_message(chat_id=user_id, text=f"আপনার ওটিপি চলে এসেছে!\n\n{text_to_send}", parse_mode="HTML")
                            except Exception as e:
                                logger.error(f"User Forward Error: {e}")

                        # Save msg_id in DB to avoid duplicates
                        ref_processed.child(msg_id).set(True)

        except Exception as e:
            logger.error(f"Error in OTP Polling Loop: {e}")

        await asyncio.sleep(5)

# Main Function
def main():
    # Start Health Check Server in a separate thread for Render
    Thread(target=run_health_check, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    # Admin Conversation Handler
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback_handler, pattern="^admin_")],
        states={
            WAITING_SERVICE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_service_name)],
            WAITING_COUNTRY_SERVICE: [CallbackQueryHandler(select_country_service, pattern="^adc_")],
            WAITING_COUNTRY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_country_name)],
            WAITING_NUMBER_SERVICE: [CallbackQueryHandler(select_number_service, pattern="^adn_s_")],
            WAITING_NUMBER_COUNTRY: [CallbackQueryHandler(select_number_country, pattern="^adn_c_")],
            WAITING_NUMBERS_INPUT: [
                MessageHandler(filters.TEXT | filters.Document.MimeType("text/plain"), save_numbers)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_admin)]
    )

    # Handlers Setup
    application.add_handler(CommandHandler("start", start))
    application.add_handler(admin_conv)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(user_callback_handler, pattern="^user_"))

    # Background Loop Attachment
    async def post_init(app: Application):
        asyncio.create_task(check_otp_loop(app))

    application.post_init = post_init

    # Run Bot
    application.run_polling()

if __name__ == "__main__":
    main()
