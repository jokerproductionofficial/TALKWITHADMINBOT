import os
import time
import asyncio
import logging
from functools import wraps
from typing import Optional, List, Set, Dict, Tuple
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import (
    Update, User, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# Load environment variables
load_dotenv()

# ==================== CONFIGURATION ====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("", "telegram_bot")

RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "5"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_REPLY = 1
WAITING_FOR_BROADCAST = 2
CONFIRM_BROADCAST = 3

# ==================== DATA MODELS ====================

@dataclass
class UserModel:
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_blocked: bool = False
    created_at: Optional[str] = None
    last_active: Optional[str] = None

@dataclass
class MessageModel:
    user_id: int
    message_text: Optional[str] = None
    direction: str = 'user_to_admin'
    admin_id: Optional[int] = None
    message_type: str = 'text'
    created_at: Optional[str] = None

# ==================== ADMIN MANAGER ====================

class AdminManager:
    _admins: Set[int] = set(ADMIN_IDS)
    
    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        return user_id in cls._admins
    
    @classmethod
    def add_admin(cls, user_id: int) -> bool:
        if user_id not in cls._admins:
            cls._admins.add(user_id)
            return True
        return False
    
    @classmethod
    def remove_admin(cls, user_id: int) -> bool:
        if user_id in cls._admins and len(cls._admins) > 1:
            cls._admins.discard(user_id)
            return True
        return False
    
    @classmethod
    def get_admins(cls) -> Set[int]:
        return cls._admins.copy()
    
    @classmethod
    def get_admin_count(cls) -> int:
        return len(cls._admins)

# ==================== RATE LIMITER ====================

class RateLimiter:
    _user_messages: Dict[int, list] = defaultdict(list)
    
    @classmethod
    def check_rate_limit(cls, user_id: int) -> Tuple[bool, int]:
        current_time = time.time()
        window_start = current_time - RATE_LIMIT_WINDOW
        
        cls._user_messages[user_id] = [
            t for t in cls._user_messages[user_id] if t > window_start
        ]
        
        if len(cls._user_messages[user_id]) >= RATE_LIMIT_MESSAGES:
            oldest = min(cls._user_messages[user_id])
            wait_time = int(oldest + RATE_LIMIT_WINDOW - current_time) + 1
            return False, max(0, wait_time)
        
        cls._user_messages[user_id].append(current_time)
        return True, 0

# ==================== DATABASE ====================

class Database:
    _instance = None
    _client = None
    _db = None
    
    @classmethod
    async def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._init_db()
        return cls._instance
    
    async def _init_db(self):
        self._client = AsyncIOMotorClient(MONGO_URL)
        self._db = self._client[DB_NAME]
        await self._db.users.create_index("user_id", unique=True)
        await self._db.messages.create_index("user_id")
        logger.info("Database initialized")
    
    @property
    def db(self):
        return self._db

# User Repository
class UserRepository:
    @staticmethod
    async def create_or_update(user_id: int, username: str = None,
                               first_name: str = None, last_name: str = None) -> UserModel:
        db_instance = await Database.get_instance()
        db = db_instance.db
        now = datetime.now(timezone.utc).isoformat()
        
        result = await db.users.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {"username": username, "first_name": first_name,
                         "last_name": last_name, "last_active": now},
                "$setOnInsert": {"user_id": user_id, "is_blocked": False, "created_at": now}
            },
            upsert=True, return_document=True
        )
        
        return UserModel(
            user_id=result["user_id"], username=result.get("username"),
            first_name=result.get("first_name"), last_name=result.get("last_name"),
            is_blocked=result.get("is_blocked", False)
        )
    
    @staticmethod
    async def get_by_id(user_id: int) -> Optional[UserModel]:
        db_instance = await Database.get_instance()
        result = await db_instance.db.users.find_one({"user_id": user_id}, {"_id": 0})
        if result:
            return UserModel(
                user_id=result["user_id"], username=result.get("username"),
                first_name=result.get("first_name"), last_name=result.get("last_name"),
                is_blocked=result.get("is_blocked", False)
            )
        return None
    
    @staticmethod
    async def get_all_active() -> List[UserModel]:
        db_instance = await Database.get_instance()
        users = []
        cursor = db_instance.db.users.find({"is_blocked": False}, {"_id": 0}).limit(1000)
        async for doc in cursor:
            users.append(UserModel(
                user_id=doc["user_id"], username=doc.get("username"),
                first_name=doc.get("first_name"), last_name=doc.get("last_name"),
                is_blocked=doc.get("is_blocked", False)
            ))
        return users
    
    @staticmethod
    async def get_user_count() -> int:
        db_instance = await Database.get_instance()
        return await db_instance.db.users.count_documents({})
    
    @staticmethod
    async def block_user(user_id: int) -> bool:
        db_instance = await Database.get_instance()
        await db_instance.db.users.update_one({"user_id": user_id}, {"$set": {"is_blocked": True}})
        return True
    
    @staticmethod
    async def unblock_user(user_id: int) -> bool:
        db_instance = await Database.get_instance()
        await db_instance.db.users.update_one({"user_id": user_id}, {"$set": {"is_blocked": False}})
        return True
    
    @staticmethod
    async def is_blocked(user_id: int) -> bool:
        user = await UserRepository.get_by_id(user_id)
        return user.is_blocked if user else False

# Message Repository
class MessageRepository:
    @staticmethod
    async def save_message(user_id: int, message_text: str, direction: str,
                          admin_id: int = None) -> None:
        db_instance = await Database.get_instance()
        now = datetime.now(timezone.utc).isoformat()
        await db_instance.db.messages.insert_one({
            "user_id": user_id, "admin_id": admin_id, "message_text": message_text,
            "direction": direction, "created_at": now
        })
    
    @staticmethod
    async def get_user_messages(user_id: int, limit: int = 10) -> List[MessageModel]:
        db_instance = await Database.get_instance()
        messages = []
        cursor = db_instance.db.messages.find(
            {"user_id": user_id}, {"_id": 0}
        ).sort("created_at", -1).limit(limit)
        async for doc in cursor:
            messages.append(MessageModel(
                user_id=doc["user_id"], message_text=doc.get("message_text"),
                direction=doc.get("direction"), admin_id=doc.get("admin_id")
            ))
        return messages
    
    @staticmethod
    async def get_message_count() -> int:
        db_instance = await Database.get_instance()
        return await db_instance.db.messages.count_documents({})

# ==================== KEYBOARDS ====================

def get_user_menu():
    keyboard = [
        [KeyboardButton("Send Message to Admin")],
        [KeyboardButton("Help"), KeyboardButton("About")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_menu():
    keyboard = [
        [KeyboardButton("Dashboard"), KeyboardButton("Users")],
        [KeyboardButton("Broadcast"), KeyboardButton("Logs")],
        [KeyboardButton("Add Admin"), KeyboardButton("Settings")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_reply_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("Reply", callback_data=f"reply_{user_id}")],
        [InlineKeyboardButton("Block", callback_data=f"block_{user_id}"),
         InlineKeyboardButton("History", callback_data=f"history_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirm_broadcast_keyboard():
    keyboard = [[
        InlineKeyboardButton("Confirm", callback_data="broadcast_confirm"),
        InlineKeyboardButton("Cancel", callback_data="broadcast_cancel")
    ]]
    return InlineKeyboardMarkup(keyboard)

# ==================== PERMISSION DECORATOR ====================

def check_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not AdminManager.is_admin(update.effective_user.id):
            await update.message.reply_text("You don't have permission to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ==================== FORMATTERS ====================

def format_user_info(user: User) -> str:
    parts = [f"User ID: {user.id}"]
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    if name:
        parts.append(f"Name: {name}")
    if user.username:
        parts.append(f"Username: @{user.username}")
    return "\n".join(parts)

def format_message_for_admin(user: User, message_text: str) -> str:
    return f"New message from user:\n{format_user_info(user)}\n\nMessage:\n{message_text}"

# ==================== USER HANDLERS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await UserRepository.create_or_update(user.id, user.username, user.first_name, user.last_name)
    
    if AdminManager.is_admin(user.id):
        await update.message.reply_text(
            f"Welcome back, Admin {user.first_name}!\nUse the menu below to manage the bot.",
            reply_markup=get_admin_menu()
        )
    else:
        await update.message.reply_text(
            f"Hello {user.first_name}!\n\nWelcome! You can send messages to our admin team here.\n"
            "Just type your message and we'll get back to you soon!",
            reply_markup=get_user_menu()
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How to use this bot:\n\n1. Simply type your message to contact admin\n"
        "2. Wait for admin to reply\n3. You'll receive responses directly here\n\n"
        "Commands:\n/start - Start the bot\n/help - Show this help message"
    )

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Talk to Admin Bot\n\nThis bot allows you to communicate directly with our admin team.\n"
        "Version: 1.0.0"
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Send Message to Admin":
        await update.message.reply_text("Just type your message and I'll forward it to the admin team!")
    elif text == "Help":
        await help_command(update, context)
    elif text == "About":
        await about_command(update, context)

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    
    if AdminManager.is_admin(user.id):
        return
    
    if await UserRepository.is_blocked(user.id):
        await message.reply_text("You have been blocked from using this bot.")
        return
    
    allowed, wait_time = RateLimiter.check_rate_limit(user.id)
    if not allowed:
        await message.reply_text(f"Please slow down! Wait {wait_time} seconds before sending another message.")
        return
    
    await UserRepository.create_or_update(user.id, user.username, user.first_name, user.last_name)
    
    message_text = message.text or "[Media message]"
    await MessageRepository.save_message(user.id, message_text, 'user_to_admin')
    
    admin_message = format_message_for_admin(user, message_text)
    
    sent_count = 0
    for admin_id in AdminManager.get_admins():
        try:
            await context.bot.send_message(admin_id, admin_message, reply_markup=get_reply_keyboard(user.id))
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send to admin {admin_id}: {e}")
    
    if sent_count > 0:
        await message.reply_text("Your message has been sent to admin. Please wait for a response.")
    else:
        await message.reply_text("Sorry, couldn't deliver your message. Please try again later.")

# ==================== ADMIN HANDLERS ====================

@check_admin
async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_count = await UserRepository.get_user_count()
    message_count = await MessageRepository.get_message_count()
    
    await update.message.reply_text(
        f"Admin Dashboard\n\nTotal Users: {user_count}\nTotal Messages: {message_count}\n"
        f"Active Admins: {AdminManager.get_admin_count()}",
        reply_markup=get_admin_menu()
    )

@check_admin
async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Dashboard":
        await dashboard_handler(update, context)
    elif text == "Users":
        await users_handler(update, context)
    elif text == "Broadcast":
        await broadcast_handler(update, context)
    elif text == "Logs":
        await update.message.reply_text("Message logging active. All conversations are stored.")
    elif text == "Add Admin":
        await update.message.reply_text("To add admin: /addadmin <user_id>\nTo remove: /removeadmin <user_id>")
    elif text == "Settings":
        await update.message.reply_text(f"Rate Limit: {RATE_LIMIT_MESSAGES} msgs/{RATE_LIMIT_WINDOW}s\n"
                                        f"Admins: {AdminManager.get_admin_count()}")

@check_admin
async def users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await UserRepository.get_all_active()
    if not users:
        await update.message.reply_text("No users found.")
        return
    
    text = "Recent Users:\n\n"
    for user in users[:10]:
        name = user.first_name or "Unknown"
        username = f"@{user.username}" if user.username else "No username"
        text += f"• {name} ({username}) - ID: {user.user_id}\n"
    text += f"\nTotal: {len(users)}"
    await update.message.reply_text(text)

@check_admin
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        new_admin_id = int(context.args[0])
        if AdminManager.add_admin(new_admin_id):
            await update.message.reply_text(f"User {new_admin_id} added as admin.")
        else:
            await update.message.reply_text("User is already an admin.")
    except ValueError:
        await update.message.reply_text("Invalid user ID.")

@check_admin
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    try:
        admin_id = int(context.args[0])
        if AdminManager.remove_admin(admin_id):
            await update.message.reply_text(f"Admin {admin_id} removed.")
        else:
            await update.message.reply_text("Cannot remove. Either not an admin or last admin.")
    except ValueError:
        await update.message.reply_text("Invalid user ID.")

# Reply handlers
async def reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not AdminManager.is_admin(query.from_user.id):
        return ConversationHandler.END
    
    user_id = int(query.data.split('_')[1])
    context.user_data['reply_to_user'] = user_id
    
    user = await UserRepository.get_by_id(user_id)
    name = user.first_name if user else f"User {user_id}"
    
    await query.message.reply_text(f"Replying to {name} (ID: {user_id})\nType your reply or /cancel:")
    return WAITING_FOR_REPLY

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'reply_to_user' not in context.user_data:
        return ConversationHandler.END
    
    user_id = context.user_data['reply_to_user']
    reply_text = update.message.text
    
    try:
        await context.bot.send_message(user_id, f"Reply from Admin:\n\n{reply_text}")
        await MessageRepository.save_message(user_id, reply_text, 'admin_to_user', update.effective_user.id)
        await update.message.reply_text(f"Reply sent to user {user_id}!")
    except Exception as e:
        await update.message.reply_text(f"Failed to send reply: {e}")
    
    del context.user_data['reply_to_user']
    return ConversationHandler.END

async def cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('reply_to_user', None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# Block/Unblock handlers
async def block_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not AdminManager.is_admin(query.from_user.id):
        return
    user_id = int(query.data.split('_')[1])
    await UserRepository.block_user(user_id)
    await query.message.reply_text(f"User {user_id} blocked.")

async def unblock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not AdminManager.is_admin(query.from_user.id):
        return
    user_id = int(query.data.split('_')[1])
    await UserRepository.unblock_user(user_id)
    await query.message.reply_text(f"User {user_id} unblocked.")

async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not AdminManager.is_admin(query.from_user.id):
        return
    
    user_id = int(query.data.split('_')[1])
    messages = await MessageRepository.get_user_messages(user_id, limit=10)
    
    if not messages:
        await query.message.reply_text(f"No history for user {user_id}.")
        return
    
    text = f"Message History (User {user_id}):\n\n"
    for msg in reversed(messages):
        prefix = "" if msg.direction == 'user_to_admin' else "[Admin] "
        text += f"{prefix}{msg.message_text[:50]}...\n" if len(msg.message_text or '') > 50 else f"{prefix}{msg.message_text}\n"
    await query.message.reply_text(text)

# Broadcast handlers
@check_admin
async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_count = await UserRepository.get_user_count()
    await update.message.reply_text(f"Broadcast to {user_count} users.\nType your message or /cancel:")
    return WAITING_FOR_BROADCAST

async def receive_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AdminManager.is_admin(update.effective_user.id):
        return ConversationHandler.END
    
    context.user_data['broadcast_message'] = update.message.text
    user_count = await UserRepository.get_user_count()
    
    await update.message.reply_text(
        f"Preview:\n\n{update.message.text}\n\nSend to {user_count} users?",
        reply_markup=get_confirm_broadcast_keyboard()
    )
    return CONFIRM_BROADCAST

async def broadcast_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "broadcast_cancel":
        context.user_data.pop('broadcast_message', None)
        await query.message.edit_text("Broadcast cancelled.")
        return ConversationHandler.END
    
    broadcast_text = context.user_data.get('broadcast_message')
    if not broadcast_text:
        await query.message.edit_text("No message found. Start again.")
        return ConversationHandler.END
    
    users = await UserRepository.get_all_active()
    await query.message.edit_text(f"Sending to {len(users)} users...")
    
    sent, failed = 0, 0
    for user in users:
        try:
            await context.bot.send_message(user.user_id, f"Announcement:\n\n{broadcast_text}")
            sent += 1
        except:
            failed += 1
    
    await query.message.reply_text(f"Broadcast Complete!\nSent: {sent}\nFailed: {failed}")
    context.user_data.pop('broadcast_message', None)
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('broadcast_message', None)
    await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END

# ==================== MAIN ====================

async def post_init(application):
    await Database.get_instance()

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return
    
    logger.info(f"Starting bot with {AdminManager.get_admin_count()} admin(s)")
    
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Reply conversation
    reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(reply_callback, pattern=r'^reply_\d+$')],
        states={WAITING_FOR_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_reply)]},
        fallbacks=[CommandHandler('cancel', cancel_reply)],
        per_user=True, per_chat=True
    )
    
    # Broadcast conversation
    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^Broadcast$') & filters.ChatType.PRIVATE, broadcast_handler)],
        states={
            WAITING_FOR_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast_message)],
            CONFIRM_BROADCAST: [CallbackQueryHandler(broadcast_confirm_callback, pattern=r'^broadcast_(confirm|cancel)$')]
        },
        fallbacks=[CommandHandler('cancel', cancel_broadcast)],
        per_user=True, per_chat=True
    )
    
    # Register handlers
    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('addadmin', add_admin_command))
    application.add_handler(CommandHandler('removeadmin', remove_admin_command))
    
    application.add_handler(reply_conv)
    application.add_handler(broadcast_conv)
    
    application.add_handler(CallbackQueryHandler(block_callback, pattern=r'^block_\d+$'))
    application.add_handler(CallbackQueryHandler(unblock_callback, pattern=r'^unblock_\d+$'))
    application.add_handler(CallbackQueryHandler(history_callback, pattern=r'^history_\d+$'))
    
    application.add_handler(MessageHandler(
        filters.Regex('^(Dashboard|Users|Logs|Add Admin|Settings)$') & filters.ChatType.PRIVATE,
        admin_menu_handler
    ))
    application.add_handler(MessageHandler(
        filters.Regex('^(Send Message to Admin|Help|About)$') & filters.ChatType.PRIVATE,
        menu_handler
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_user_message
    ))
    
    logger.info("Bot starting...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
