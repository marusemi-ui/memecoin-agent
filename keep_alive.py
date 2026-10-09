from flask import Flask, request
from threading import Thread
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask('')


@app.route('/')
def home():
    return "Bot is running"


@app.route('/health')
def health():
    return "OK", 200


def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.start()