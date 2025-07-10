import os
import logging
import sqlite3
import asyncio
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from functools import wraps

import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ContextTypes, CallbackQueryHandler, ConversationHandler
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from flask import Flask, render_template_string

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
ADMIN_USER_IDS = [int(id) for id in os.getenv('ADMIN_USER_IDS', '').split(',') if id.strip()]
DB_PATH = 'vigilai_bot.db'

# Conversation states
BROADCAST_MESSAGE = 1

# Flask HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>VigilAI Status</title>
    <style>
        body { font-family: sans-serif; background-color: #f7f7f7; text-align: center; padding-top: 100px; }
        h1 { color: #333; }
        .stats { margin-top: 30px; font-size: 18px; color: #666; }
    </style>
</head>
<body>
    <h1>🤖 VigilAI Bot is Running</h1>
    <div class="stats">
        <p>Total Users: {{ total_users }}</p>
        <p>Active Today: {{ active_today }}</p>
    </div>
</body>
</html>
"""

class VigilAIBot:
    def __init__(self):
        self.db_path = DB_PATH
        self.init_database()
        self.setup_genai()
        self.user_sessions = {}
        self.rate_limits = {}
        self.running = False

    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_premium BOOLEAN DEFAULT FALSE,
                registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_count INTEGER DEFAULT 0,
                is_banned BOOLEAN DEFAULT FALSE
            )
        ''')

        # Chat sessions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER,
                chat_history TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        # Usage statistics table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                command TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_used INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        # Feedback table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                rating INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        conn.commit()
        conn.close()

    def setup_genai(self):
        """Configure Google Generative AI"""
        if not GOOGLE_API_KEY:
            logger.error("GOOGLE_API_KEY is not set!")
            return
        
        genai.configure(api_key=GOOGLE_API_KEY)
        self.model = genai.GenerativeModel('gemini-pro')

    def register_user(self, user_id: int, username: str = None, 
                     first_name: str = None, last_name: str = None):
        """Register or update user in database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Insert new user or ignore if exists
        cursor.execute('''
            INSERT OR IGNORE INTO users 
            (user_id, username, first_name, last_name, registration_date)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, username, first_name, last_name))

        # Update existing user
        cursor.execute('''
            UPDATE users 
            SET username = ?,
                first_name = ?,
                last_name = ?,
                last_activity = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (username, first_name, last_name, user_id))

        conn.commit()
        conn.close()

    def update_user_activity(self, user_id: int):
        """Update user's last activity and increment message count"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE users 
            SET last_activity = CURRENT_TIMESTAMP, message_count = message_count + 1
            WHERE user_id = ?
        ''', (user_id,))

        conn.commit()
        conn.close()

    def log_usage(self, user_id: int, command: str, tokens_used: int = 0):
        """Log usage statistics"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO usage_stats (user_id, command, tokens_used)
            VALUES (?, ?, ?)
        ''', (user_id, command, tokens_used))

        conn.commit()
        conn.close()

    def get_user_stats(self, user_id: int) -> Dict:
        """Get user statistics"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT registration_date, message_count, is_premium
            FROM users WHERE user_id = ?
        ''', (user_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'registration_date': result[0],
                'message_count': result[1],
                'is_premium': bool(result[2])
            }
        return None

    def is_rate_limited(self, user_id: int) -> bool:
        """Check if user is rate limited"""
        now = datetime.now()
        if user_id not in self.rate_limits:
            self.rate_limits[user_id] = []

        # Remove old entries (older than 1 hour)
        self.rate_limits[user_id] = [
            timestamp for timestamp in self.rate_limits[user_id]
            if now - timestamp < timedelta(hours=1)
        ]

        # Check if user has exceeded rate limit (20 messages per hour)
        if len(self.rate_limits[user_id]) >= 20:
            return True

        self.rate_limits[user_id].append(now)
        return False

    @staticmethod
    def admin_only(func):
        """Decorator to restrict commands to admin users only"""
        @wraps(func)
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            if user_id not in ADMIN_USER_IDS:
                await update.message.reply_text("⚠️ This command is restricted to administrators only.")
                return
            return await func(self, update, context)
        return wrapper

    async def generate_ai_response(self, prompt: str, user_id: int) -> str:
        """Generate AI response using Google Generative AI"""
        try:
            if not hasattr(self, 'model'):
                return "⚠️ AI service is not configured properly. Please contact the administrator."
            
            # Add context and personality to the prompt
            enhanced_prompt = f"""
            You are VigilAI, a helpful and knowledgeable AI assistant. 
            Respond to the following message in a friendly, informative, and concise manner:
            
            User message: {prompt}
            
            Guidelines:
            - Be helpful and accurate
            - Keep responses concise but informative
            - Use appropriate emojis when relevant
            - If asked about your capabilities, mention you're VigilAI
            """

            response = self.model.generate_content(enhanced_prompt)

            # Add footer
            ai_response = response.text + "\n\n🤖 _Generated by VigilAI_"

            # Log tokens used (approximate)
            tokens_used = len(prompt.split()) + len(response.text.split())
            self.log_usage(user_id, 'ai_chat', tokens_used)

            return ai_response

        except Exception as e:
            logger.error(f"Error generating AI response: {e}")
            return "❌ Sorry, I encountered an error while processing your request. Please try again later.\n\n🤖 _Generated by VigilAI_"

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        self.register_user(user.id, user.username, user.first_name, user.last_name)
        self.update_user_activity(user.id)  # Track user activity

        welcome_message = f"""
🚀 Welcome to VigilAI, {user.first_name}! 

I'm your intelligent AI assistant ready to help you with:
• 💬 Natural conversations
• 📚 Knowledge questions
• 🔍 Information research
• 💡 Creative tasks
• 🧮 Problem solving

**How to use me:**
• Send me any message for a chat
• Use /askai <your question> in groups
• Try /help for all commands

Let's get started! What would you like to know? 🤔

🤖 _Powered by VigilAI_
        """

        keyboard = [
            [InlineKeyboardButton("📊 My Stats", callback_data="stats")],
            [InlineKeyboardButton("❓ Help", callback_data="help"),
             InlineKeyboardButton("💬 Feedback", callback_data="feedback")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        self.log_usage(user.id, 'start')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
🔧 **VigilAI Commands:**

**For Everyone:**
• `/start` - Get started with VigilAI
• `/help` - Show this help message
• `/askai <question>` - Ask AI in groups
• `/stats` - View your usage statistics
• `/feedback <message>` - Send feedback
• `/clear` - Clear your chat history

**Features:**
• 🧠 Smart AI conversations
• 📊 Usage tracking
• 🎯 Rate limiting protection
• 💾 Persistent chat history
• 🔄 Context-aware responses

**Admin Commands:**
• `/broadcast <message>` - Send message to all users
• `/users` - Get user statistics
• `/ban <user_id>` - Ban a user
• `/unban <user_id>` - Unban a user

**Tips:**
• I work in private chats and groups
• Use natural language - I understand context
• Rate limit: 20 messages per hour
• Your privacy is protected

🤖 _Powered by VigilAI_
        """

        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
        self.log_usage(update.effective_user.id, 'help')

    async def ask_ai_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /askai command"""
        user_id = update.effective_user.id

        # Check rate limiting
        if self.is_rate_limited(user_id):
            await update.message.reply_text("⏰ Rate limit exceeded. Please wait before sending another message.")
            return

        # Register user
        user = update.effective_user
        self.register_user(user.id, user.username, user.first_name, user.last_name)
        self.update_user_activity(user.id)

        # Get the question
        if not context.args:
            await update.message.reply_text("❓ Please provide a question after /askai\n\nExample: `/askai What is artificial intelligence?`", parse_mode=ParseMode.MARKDOWN)
            return

        question = ' '.join(context.args)

        # Show typing indicator
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Generate response
        response = await self.generate_ai_response(question, user_id)

        # Send response
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle regular messages"""
        # Only respond in private chats
        if update.effective_chat.type != ChatType.PRIVATE:
            return

        user_id = update.effective_user.id

        # Check rate limiting
        if self.is_rate_limited(user_id):
            await update.message.reply_text("⏰ Rate limit exceeded. Please wait before sending another message.")
            return

        # Register user
        user = update.effective_user
        self.register_user(user.id, user.username, user.first_name, user.last_name)
        self.update_user_activity(user.id)

        # Show typing indicator
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Generate response
        response = await self.generate_ai_response(update.message.text, user_id)

        # Send response
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        user_id = update.effective_user.id
        stats = self.get_user_stats(user_id)

        if not stats:
            await update.message.reply_text("❌ User not found. Please use /start first.")
            return

        registration_date = datetime.strptime(stats['registration_date'], "%Y-%m-%d %H:%M:%S").strftime('%Y-%m-%d')
        premium_status = "✅ Premium" if stats['is_premium'] else "🆓 Free"

        stats_text = f"""
📊 **Your VigilAI Stats:**

👤 **User ID:** `{user_id}`
📅 **Member Since:** {registration_date}
💬 **Messages Sent:** {stats['message_count']}
🎯 **Status:** {premium_status}
⏱️ **Rate Limit:** 20 messages/hour

🤖 _Generated by VigilAI_
        """

        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
        self.log_usage(user_id, 'stats')

    async def feedback_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /feedback command"""
        user_id = update.effective_user.id

        if not context.args:
            await update.message.reply_text("📝 Please provide your feedback after /feedback\n\nExample: `/feedback Great bot, very helpful!`", parse_mode=ParseMode.MARKDOWN)
            return

        feedback_text = ' '.join(context.args)

        # Store feedback
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO feedback (user_id, message)
            VALUES (?, ?)
        ''', (user_id, feedback_text))
        conn.commit()
        conn.close()

        await update.message.reply_text("✅ Thank you for your feedback! It helps us improve VigilAI.\n\n🤖 _Generated by VigilAI_", parse_mode=ParseMode.MARKDOWN)
        self.log_usage(user_id, 'feedback')

    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clear command"""
        user_id = update.effective_user.id

        # Clear user's chat history
        if user_id in self.user_sessions:
            del self.user_sessions[user_id]

        await update.message.reply_text("🗑️ Your chat history has been cleared!\n\n🤖 _Generated by VigilAI_", parse_mode=ParseMode.MARKDOWN)
        self.log_usage(user_id, 'clear')

    @admin_only
    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /broadcast command - Admin only"""
        await update.message.reply_text("📢 Please send the message you want to broadcast to all users:")
        return BROADCAST_MESSAGE

    async def handle_broadcast_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle broadcast message input"""
        message = update.message.text

        # Get all users
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE is_banned = FALSE')
        users = cursor.fetchall()
        conn.close()

        # Send broadcast message
        success_count = 0
        fail_count = 0

        broadcast_text = f"📢 **Broadcast Message:**\n\n{message}\n\n🤖 _From VigilAI Team_"

        for user_tuple in users:
            user_id = user_tuple[0]
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=broadcast_text,
                    parse_mode=ParseMode.MARKDOWN
                )
                success_count += 1
                await asyncio.sleep(0.05)  # Rate limiting
            except Exception as e:
                fail_count += 1
                logger.error(f"Failed to send broadcast to {user_id}: {e}")

        await update.message.reply_text(
            f"✅ Broadcast complete!\n\n"
            f"📤 Sent: {success_count}\n"
            f"❌ Failed: {fail_count}\n\n"
            f"🤖 _Generated by VigilAI_",
            parse_mode=ParseMode.MARKDOWN
        )

        return ConversationHandler.END

    @admin_only
    async def users_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /users command - Admin only"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get user statistics
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(last_activity) = DATE("now")')
        active_today = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM users WHERE is_banned = TRUE')
        banned_users = cursor.fetchone()[0]

        cursor.execute('SELECT SUM(message_count) FROM users')
        total_messages = cursor.fetchone()[0] or 0

        conn.close()

        stats_text = f"""
📊 **Bot Statistics:**

👥 **Total Users:** {total_users}
🟢 **Active Today:** {active_today}
🚫 **Banned Users:** {banned_users}
💬 **Total Messages:** {total_messages}

🤖 _Generated by VigilAI_
        """

        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks"""
        query = update.callback_query
        await query.answer()

        if query.data == "stats":
            # Create a fake update for stats command
            fake_update = Update(update.update_id, message=query.message)
            await self.stats_command(fake_update, context)
        elif query.data == "help":
            fake_update = Update(update.update_id, message=query.message)
            await self.help_command(fake_update, context)
        elif query.data == "feedback":
            await query.edit_message_text("📝 Use /feedback <your message> to send feedback!")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        logger.error(f"Exception while handling an update: {context.error}")

        try:
            if update.message:
                await update.message.reply_text(
                    "❌ An unexpected error occurred. Please try again later.\n\n🤖 _Generated by VigilAI_",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif update.callback_query:
                await update.callback_query.message.reply_text(
                    "❌ Operation failed due to an error",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.error(f"Error in error handler: {e}")

    async def set_bot_commands(self, application):
        """Set bot commands for the menu"""
        commands = [
            BotCommand("start", "Get started with VigilAI"),
            BotCommand("help", "Show help message"),
            BotCommand("askai", "Ask AI a question"),
            BotCommand("stats", "View your statistics"),
            BotCommand("feedback", "Send feedback"),
            BotCommand("clear", "Clear chat history"),
        ]

        await application.bot.set_my_commands(commands)

    def start_bot(self):
        """Start the bot in a background thread"""
        if not BOT_TOKEN:
            logger.error("Please set TELEGRAM_BOT_TOKEN environment variable")
            return
        if not GOOGLE_API_KEY:
            logger.error("Please set GOOGLE_API_KEY environment variable")
            return

        # Create application with job queue enabled
        application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("askai", self.ask_ai_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("feedback", self.feedback_command))
        application.add_handler(CommandHandler("clear", self.clear_command))
        application.add_handler(CommandHandler("users", self.users_command))

        # Broadcast conversation handler
        broadcast_handler = ConversationHandler(
            entry_points=[CommandHandler("broadcast", self.broadcast_command)],
            states={
                BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_broadcast_message)]
            },
            fallbacks=[]
        )
        application.add_handler(broadcast_handler)

        # Button handler
        application.add_handler(CallbackQueryHandler(self.button_handler))

        # Message handler
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Error handler
        application.add_error_handler(self.error_handler)

        # Set bot commands
        async def set_commands_job(context: ContextTypes.DEFAULT_TYPE):
            await self.set_bot_commands(application)

        application.job_queue.run_once(set_commands_job, when=1)

        # Start the bot
        logger.info("Starting VigilAI Bot...")
        self.running = True
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    def run(self):
        """Run the bot in a separate thread"""
        self.bot_thread = threading.Thread(target=self.start_bot, daemon=True)
        self.bot_thread.start()
        logger.info("Bot thread started")

def run_flask():
    """Run Flask web server"""
    app = Flask(__name__)

    @app.route("/")
    def index():
        # Access database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(last_activity) = DATE("now")')
        active_today = cursor.fetchone()[0]
        conn.close()

        return render_template_string(HTML_TEMPLATE, total_users=total_users, active_today=active_today)

    app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    # Initialize and start the bot
    bot = VigilAIBot()
    bot.run()

    # Run Flask server in parallel
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server started")

    # Keep the main thread alive
    logger.info("Main thread running")
    try:
        while True:
            time.sleep(3600)  # Sleep for 1 hour
    except KeyboardInterrupt:
        logger.info("Shutting down...")