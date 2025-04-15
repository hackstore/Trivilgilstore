# bot.py

from dotenv import load_dotenv
load_dotenv()

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler
)
from pymongo import MongoClient
import os

# Load environment variables
MONGO_URI = os.getenv("MONGO_URI")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# MongoDB connection
client = MongoClient(MONGO_URI)
db = client.trivigil

# Conversation states
WAITING_TXID = 1

# --- Start verification with token ---
async def start_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        token = update.message.text.split()[1]
    except IndexError:
        await update.message.reply_text("Usage: /verify <TOKEN>")
        return ConversationHandler.END

    record = db.tokens.find_one({"token": token})

    if not record:
        await update.message.reply_text("❌ Invalid token")
        return ConversationHandler.END

    db.tokens.update_one(
        {"token": token},
        {"$set": {"telegram_id": update.effective_user.id}}
    )

    await update.message.reply_text("📤 Please provide your Bitcoin transaction ID:")
    return WAITING_TXID

# --- Handle transaction ID from user ---
async def handle_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text
    user_id = update.effective_user.id

    db.tokens.update_one(
        {"telegram_id": user_id},
        {"$set": {"transaction_id": txid}}
    )

    # Notify admin
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"⚠️ New verification request:\nTXID: {txid}"
    )

    await update.message.reply_text("✅ Transaction ID received. Admin will verify shortly.")
    return ConversationHandler.END

# --- Admin verifies transaction ---
async def verify_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_CHAT_ID:
        return

    try:
        _, token, txid = update.message.text.split()
    except ValueError:
        await update.message.reply_text("Usage: /verify_transaction <token> <txid>")
        return

    db.tokens.update_one(
        {"token": token},
        {"$set": {"verified": True}}
    )
    record = db.tokens.find_one({"token": token})

    if record and record.get("telegram_id"):
        await context.bot.send_message(
            chat_id=record["telegram_id"],
            text=f"✅ Verified!\nDownload: {record['download_link']}"
        )

    await update.message.reply_text(f"Verified token {token}")

# --- Admin checks all tokens ---
async def check_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_CHAT_ID:
        return

    records = db.tokens.find()
    response = ["📦 All tokens:"]

    for doc in records:
        response.append(
            f"{doc['token']} - {'✅' if doc['verified'] else '❌'} - {doc.get('transaction_id', '-')}"
        )

    await update.message.reply_text("\n".join(response))

# --- Main bot application ---
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("verify", start_verification)],
        states={
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_txid)]
        },
        fallbacks=[]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("verify_transaction", verify_transaction))
    app.add_handler(CommandHandler("check_all", check_all))

    print("Telegram bot is running...")
    app.run_polling()
