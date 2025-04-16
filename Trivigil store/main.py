# app.py

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, request, render_template
from pymongo import MongoClient
from datetime import datetime
import os
import random
import string
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler
)
from threading import thread
import asyncio

def run_bot():
    asyncio.run(bot_app.run_polling())


# Initialize Flask app
app = Flask(__name__)

# MongoDB setup
client = MongoClient(os.getenv("MONGO_URI"))
db = client.trivigil

# --- Flask Routes ---
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

def generate_token(prefix="NAT"):
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"{prefix}-{random_part}"

@app.route('/generate-token', methods=['POST'])
def handle_generate_token():
    data = request.get_json()
    product_code = data.get('product', 'NAT')
    token = generate_token(product_code)
    
    db.tokens.insert_one({
        "product": product_code,
        "token": token,
        "verified": False,
        "created_at": datetime.now(),
        "telegram_id": None,
        "transaction_id": None,
        "download_link": "https://trivigil.com/download/secure-file"
    })
    
    return jsonify({"token": token})

# --- Telegram Bot Handlers ---
# Conversation states
WAITING_TXID = 1

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

async def handle_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text
    user_id = update.effective_user.id

    db.tokens.update_one(
        {"telegram_id": user_id},
        {"$set": {"transaction_id": txid}}
    )

    # Notify admin
    await context.bot.send_message(
        chat_id=os.getenv("ADMIN_CHAT_ID"),
        text=f"⚠️ New verification request:\nTXID: {txid}"
    )

    await update.message.reply_text("✅ Transaction ID received. Admin will verify shortly.")
    return ConversationHandler.END

async def verify_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("ADMIN_CHAT_ID"):
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

async def check_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("ADMIN_CHAT_ID"):
        return

    records = db.tokens.find()
    response = ["📦 All tokens:"]

    for doc in records:
        response.append(
            f"{doc['token']} - {'✅' if doc['verified'] else '❌'} - {doc.get('transaction_id', '-')}"
        )

    await update.message.reply_text("\n".join(response))

def run_bot():
    bot_app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("verify", start_verification)],
        states={
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_txid)]
        },
        fallbacks=[]
    )

    bot_app.add_handler(conv_handler)
    bot_app.add_handler(CommandHandler("verify_transaction", verify_transaction))
    bot_app.add_handler(CommandHandler("check_all", check_all))

    print("Telegram bot is running...")
    bot_app.run_polling()

if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)

  
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()

    port = int(os.environ.get("PORT", 5000))  # Use Render-assigned port
    app.run(host="0.0.0.0", port=port)