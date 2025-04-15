# app.py
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, jsonify, request, render_template
from pymongo import MongoClient
from datetime import datetime
import os
import random
import string

app = Flask(__name__)
client = MongoClient(os.getenv("MONGO_URI"))
db = client.trivigil

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


# Generate unique token
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

if __name__ == '__main__':
    app.run(port=5000)
